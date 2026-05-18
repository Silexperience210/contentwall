"""
ContentWall CRUD layer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from lnbits.db import Database
from lnbits.helpers import urlsafe_short_hash

from .models import CreateItem, Item, ItemFile, ItemStats, Payment, UpdateItem

db = Database("ext_contentwall")

FILES_DIR = os.path.join("data", "contentwall", "files")


def _ensure_files_dir():
    os.makedirs(FILES_DIR, exist_ok=True)


def _hash_content(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _new_signing_key() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


async def create_item(wallet_id: str, data: CreateItem) -> Item:
    item_id = urlsafe_short_hash()
    now = datetime.now(timezone.utc).isoformat()
    memo = data.memo or f"ContentWall: {data.title}"

    item = Item(
        id=item_id,
        wallet=wallet_id,
        title=data.title,
        description=data.description,
        content_type=data.content_type,
        amount=data.amount,
        currency=data.currency,
        memo=memo,
        remembers=1 if data.remembers else 0,
        release_delay_seconds=data.release_delay_seconds,
        scheduled_at=data.scheduled_at,
        onion_hostname=data.onion_hostname,
        created_at=now,
        teaser_text=data.teaser_text,
        teaser_blur=1 if data.teaser_blur else 0,
        access_duration_seconds=data.access_duration_seconds,
        access_signing_key=_new_signing_key(),
        webhook_url=data.webhook_url,
        max_views=data.max_views,
    )
    await db.insert("contentwall.items", item)
    return item


async def get_item(item_id: str) -> Optional[Item]:
    return await db.fetchone(
        "SELECT * FROM contentwall.items WHERE id = :id",
        {"id": item_id},
        Item,
    )


async def get_items(
    wallet_ids: list[str], include_archived: bool = False
) -> list[Item]:
    if not wallet_ids:
        return []
    q = ",".join([f"'{w}'" for w in wallet_ids])
    where = f"wallet IN ({q})"
    if not include_archived:
        where += " AND archived_at IS NULL"
    return await db.fetchall(
        f"SELECT * FROM contentwall.items WHERE {where} ORDER BY created_at DESC",
        model=Item,
    )


async def update_item(item_id: str, data: UpdateItem) -> Optional[Item]:
    """Patch-style update. Only fields explicitly set are written."""
    fields = data.dict(exclude_unset=True)
    if not fields:
        return await get_item(item_id)

    # Translate the friendly 'archived' bool -> archived_at timestamp.
    if "archived" in fields:
        if fields.pop("archived"):
            fields["archived_at"] = datetime.now(timezone.utc).isoformat()
        else:
            fields["archived_at"] = None

    # Bool to int for sqlite compatibility on the fields we store as INTEGER.
    for bool_field in ("remembers", "teaser_blur"):
        if bool_field in fields and isinstance(fields[bool_field], bool):
            fields[bool_field] = 1 if fields[bool_field] else 0

    set_clause = ", ".join(f"{k} = :{k}" for k in fields.keys())
    params = {**fields, "id": item_id}
    await db.execute(
        f"UPDATE contentwall.items SET {set_clause} WHERE id = :id", params
    )
    return await get_item(item_id)


async def archive_item(item_id: str) -> None:
    """Soft-delete: hide from admin list but keep payments+files for buyers."""
    await db.execute(
        "UPDATE contentwall.items SET archived_at = :now WHERE id = :id",
        {"now": datetime.now(timezone.utc).isoformat(), "id": item_id},
    )


async def delete_item(item_id: str) -> None:
    """Hard delete: wipes payments, files on disk and DB rows."""
    for ext in [".article", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bin"]:
        fp = os.path.join(FILES_DIR, f"{item_id}{ext}")
        if os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass

    # Bundle files
    files = await get_item_files(item_id)
    for f in files:
        fp = os.path.join(FILES_DIR, "bundles", item_id, f.id)
        if os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass
    bundle_dir = os.path.join(FILES_DIR, "bundles", item_id)
    if os.path.isdir(bundle_dir):
        try:
            os.rmdir(bundle_dir)
        except OSError:
            pass

    await db.execute(
        "DELETE FROM contentwall.item_files WHERE item_id = :id", {"id": item_id}
    )
    await db.execute(
        "DELETE FROM contentwall.payments WHERE item_id = :id", {"id": item_id}
    )
    await db.execute(
        "DELETE FROM contentwall.items WHERE id = :id", {"id": item_id}
    )


# ---------------------------------------------------------------------------
# Content storage
# ---------------------------------------------------------------------------


async def store_article_content(item_id: str, content: str) -> None:
    _ensure_files_dir()
    file_path = os.path.join(FILES_DIR, f"{item_id}.article")
    content_bytes = content.encode("utf-8")
    with open(file_path, "wb") as f:
        f.write(content_bytes)
    content_hash = _hash_content(content_bytes)
    await db.execute(
        "UPDATE contentwall.items SET content_hash = :hash WHERE id = :id",
        {"hash": content_hash, "id": item_id},
    )


async def store_image_file(item_id: str, upload_file) -> dict:
    _ensure_files_dir()
    file_ext = os.path.splitext(upload_file.filename or "")[1].lower()
    if file_ext not in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
        ct = (upload_file.content_type or "").lower()
        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        file_ext = ext_map.get(ct, ".bin")
    file_path = os.path.join(FILES_DIR, f"{item_id}{file_ext}")
    content = await upload_file.read()
    content_hash = _hash_content(content)
    with open(file_path, "wb") as f:
        f.write(content)
    await db.execute(
        "UPDATE contentwall.items SET content_hash = :hash WHERE id = :id",
        {"hash": content_hash, "id": item_id},
    )
    return {
        "file_path": file_path,
        "content_hash": content_hash,
        "size": len(content),
        "content_type": upload_file.content_type or "application/octet-stream",
    }


async def get_article_content(item_id: str) -> Optional[str]:
    fp = os.path.join(FILES_DIR, f"{item_id}.article")
    if not os.path.exists(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        return f.read()


async def get_image_file_info(item_id: str) -> Optional[dict]:
    for ext, ct in [
        (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
        (".png", "image/png"), (".gif", "image/gif"), (".webp", "image/webp"),
    ]:
        fp = os.path.join(FILES_DIR, f"{item_id}{ext}")
        if os.path.exists(fp):
            return {"file_path": fp, "content_type": ct, "size": os.path.getsize(fp)}
    fp = os.path.join(FILES_DIR, f"{item_id}.bin")
    if os.path.exists(fp):
        return {"file_path": fp, "content_type": "application/octet-stream", "size": os.path.getsize(fp)}
    return None


async def get_image_base64(item_id: str) -> Optional[dict]:
    info = await get_image_file_info(item_id)
    if not info:
        return None
    with open(info["file_path"], "rb") as f:
        data = f.read()
    return {
        "data": base64.b64encode(data).decode("ascii"),
        "content_type": info["content_type"],
    }


# ---------------------------------------------------------------------------
# Bundle files (multi-file items)
# ---------------------------------------------------------------------------


async def add_bundle_file(item_id: str, upload_file) -> ItemFile:
    """Store an extra file as part of a bundle item."""
    bundle_dir = os.path.join(FILES_DIR, "bundles", item_id)
    os.makedirs(bundle_dir, exist_ok=True)

    content = await upload_file.read()
    content_hash = _hash_content(content)
    file_id = urlsafe_short_hash()
    file_path = os.path.join(bundle_dir, file_id)
    with open(file_path, "wb") as f:
        f.write(content)

    # Determine next position
    existing = await get_item_files(item_id)
    next_position = (max((f.position for f in existing), default=-1)) + 1

    bf = ItemFile(
        id=file_id,
        item_id=item_id,
        filename=upload_file.filename or file_id,
        content_type=upload_file.content_type or "application/octet-stream",
        size=len(content),
        content_hash=content_hash,
        position=next_position,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    await db.insert("contentwall.item_files", bf)
    return bf


async def get_item_files(item_id: str) -> list[ItemFile]:
    return await db.fetchall(
        "SELECT * FROM contentwall.item_files WHERE item_id = :id ORDER BY position",
        {"id": item_id},
        model=ItemFile,
    )


async def get_item_file(file_id: str) -> Optional[ItemFile]:
    return await db.fetchone(
        "SELECT * FROM contentwall.item_files WHERE id = :id",
        {"id": file_id},
        ItemFile,
    )


async def delete_bundle_file(file_id: str) -> None:
    bf = await get_item_file(file_id)
    if not bf:
        return
    fp = os.path.join(FILES_DIR, "bundles", bf.item_id, bf.id)
    if os.path.exists(fp):
        try:
            os.remove(fp)
        except OSError:
            pass
    await db.execute(
        "DELETE FROM contentwall.item_files WHERE id = :id", {"id": file_id}
    )


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


async def record_payment(
    item_id: str,
    payment_hash: str,
    amount_paid: int,
    access_duration_seconds: int = 0,
) -> Payment:
    now = datetime.now(timezone.utc)
    expires_at = None
    if access_duration_seconds and access_duration_seconds > 0:
        expires_at = (now + timedelta(seconds=access_duration_seconds)).isoformat()
    payment = Payment(
        id=urlsafe_short_hash(),
        item_id=item_id,
        payment_hash=payment_hash,
        amount_paid=amount_paid,
        paid_at=now.isoformat(),
        created_at=now.isoformat(),
        expires_at=expires_at,
        views_count=0,
    )
    await db.insert("contentwall.payments", payment)
    return payment


async def get_payment(item_id: str, payment_hash: str) -> Optional[Payment]:
    return await db.fetchone(
        """
        SELECT * FROM contentwall.payments
        WHERE item_id = :item_id AND payment_hash = :payment_hash
        """,
        {"item_id": item_id, "payment_hash": payment_hash},
        Payment,
    )


async def get_payment_timestamp(item_id: str, payment_hash: str) -> Optional[str]:
    row = await db.fetchone(
        """
        SELECT paid_at FROM contentwall.payments
        WHERE item_id = :item_id AND payment_hash = :payment_hash
        """,
        {"item_id": item_id, "payment_hash": payment_hash},
    )
    if not row:
        return None
    paid_at = row["paid_at"]
    if paid_at is None:
        return None
    return paid_at if isinstance(paid_at, str) else paid_at.isoformat()


async def has_paid(item_id: str, payment_hash: str) -> bool:
    row = await db.fetchone(
        """
        SELECT 1 FROM contentwall.payments
        WHERE item_id = :item_id AND payment_hash = :payment_hash
        """,
        {"item_id": item_id, "payment_hash": payment_hash},
    )
    return row is not None


async def get_payment_amount(item_id: str, payment_hash: str) -> int:
    row = await db.fetchone(
        """
        SELECT amount_paid FROM contentwall.payments
        WHERE item_id = :item_id AND payment_hash = :payment_hash
        """,
        {"item_id": item_id, "payment_hash": payment_hash},
    )
    if not row:
        return 0
    return int(row["amount_paid"]) if row["amount_paid"] is not None else 0


async def increment_view_count(item_id: str, payment_hash: str) -> int:
    """Increment views_count and return the new value."""
    await db.execute(
        """
        UPDATE contentwall.payments
        SET views_count = COALESCE(views_count, 0) + 1
        WHERE item_id = :item_id AND payment_hash = :payment_hash
        """,
        {"item_id": item_id, "payment_hash": payment_hash},
    )
    row = await db.fetchone(
        """
        SELECT views_count FROM contentwall.payments
        WHERE item_id = :item_id AND payment_hash = :payment_hash
        """,
        {"item_id": item_id, "payment_hash": payment_hash},
    )
    if not row or row["views_count"] is None:
        return 0
    return int(row["views_count"])


async def is_payment_expired(item_id: str, payment_hash: str) -> bool:
    payment = await get_payment(item_id, payment_hash)
    if not payment or not payment.expires_at:
        return False
    expires = datetime.fromisoformat(payment.expires_at.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) > expires


async def list_payments_for_item(item_id: str) -> list[Payment]:
    return await db.fetchall(
        "SELECT * FROM contentwall.payments WHERE item_id = :id ORDER BY paid_at DESC",
        {"id": item_id},
        model=Payment,
    )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


async def get_item_stats(item_id: str) -> ItemStats:
    """Aggregated stats for a single item."""
    row = await db.fetchone(
        """
        SELECT
            COUNT(*) AS payment_count,
            COALESCE(SUM(amount_paid), 0) AS total_sats,
            COUNT(DISTINCT payment_hash) AS unique_payers,
            MAX(paid_at) AS last_payment_at
        FROM contentwall.payments
        WHERE item_id = :id
        """,
        {"id": item_id},
    )
    if not row:
        return ItemStats(item_id=item_id)
    last = row["last_payment_at"]
    if last is not None and not isinstance(last, str):
        last = last.isoformat()
    return ItemStats(
        item_id=item_id,
        payment_count=int(row["payment_count"] or 0),
        total_sats=int(row["total_sats"] or 0),
        unique_payers=int(row["unique_payers"] or 0),
        last_payment_at=last,
    )


async def get_stats_for_wallets(wallet_ids: list[str]) -> dict[str, ItemStats]:
    """Bulk stats keyed by item_id, for use in the admin table."""
    if not wallet_ids:
        return {}
    q = ",".join([f"'{w}'" for w in wallet_ids])
    rows = await db.fetchall(
        f"""
        SELECT
            i.id AS item_id,
            COUNT(p.id) AS payment_count,
            COALESCE(SUM(p.amount_paid), 0) AS total_sats,
            COUNT(DISTINCT p.payment_hash) AS unique_payers,
            MAX(p.paid_at) AS last_payment_at
        FROM contentwall.items i
        LEFT JOIN contentwall.payments p ON p.item_id = i.id
        WHERE i.wallet IN ({q})
        GROUP BY i.id
        """
    )
    out: dict[str, ItemStats] = {}
    for r in rows:
        last = r["last_payment_at"]
        if last is not None and not isinstance(last, str):
            last = last.isoformat()
        out[r["item_id"]] = ItemStats(
            item_id=r["item_id"],
            payment_count=int(r["payment_count"] or 0),
            total_sats=int(r["total_sats"] or 0),
            unique_payers=int(r["unique_payers"] or 0),
            last_payment_at=last,
        )
    return out


async def get_payments_by_day(wallet_ids: list[str], days: int = 30) -> list[dict]:
    """Daily aggregated payments for the wallet's items, for chart display."""
    if not wallet_ids:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = ",".join([f"'{w}'" for w in wallet_ids])
    rows = await db.fetchall(
        f"""
        SELECT
            SUBSTR(p.paid_at, 1, 10) AS day,
            COUNT(*) AS count,
            COALESCE(SUM(p.amount_paid), 0) AS total_sats
        FROM contentwall.payments p
        JOIN contentwall.items i ON i.id = p.item_id
        WHERE i.wallet IN ({q}) AND p.paid_at >= :cutoff
        GROUP BY day
        ORDER BY day ASC
        """,
        {"cutoff": cutoff},
    )
    return [
        {
            "day": r["day"],
            "count": int(r["count"] or 0),
            "total_sats": int(r["total_sats"] or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Signed access (HMAC)
# ---------------------------------------------------------------------------


def make_access_token(signing_key: str, item_id: str, payment_hash: str) -> str:
    """
    Compact HMAC-SHA256 token bound to (item_id, payment_hash).

    Format: <hex_digest_first_16_bytes>
    Verified on every protected request -> URL tampering is detected.
    """
    if not signing_key:
        return ""
    mac = hmac.new(
        signing_key.encode("utf-8"),
        f"{item_id}:{payment_hash}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return mac.hex()[:32]


def verify_access_token(
    signing_key: str, item_id: str, payment_hash: str, token: str
) -> bool:
    if not signing_key or not token:
        return False
    expected = make_access_token(signing_key, item_id, payment_hash)
    return hmac.compare_digest(expected, token)


# ---------------------------------------------------------------------------
# Audio / Video file storage (single-file, like image but bigger)
# ---------------------------------------------------------------------------


async def store_media_file(item_id: str, upload_file) -> dict:
    """Store an audio/video file using the same flat-file scheme as images."""
    _ensure_files_dir()
    file_ext = os.path.splitext(upload_file.filename or "")[1].lower()
    # Tolerate a broad set; the content_type is what we serve back.
    if file_ext not in (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav",
                        ".mp4", ".webm", ".mkv", ".mov"):
        ct = (upload_file.content_type or "").lower()
        ext_map = {
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/aac": ".aac",
            "audio/ogg": ".ogg",
            "audio/opus": ".opus",
            "audio/wav": ".wav",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/x-matroska": ".mkv",
            "video/quicktime": ".mov",
        }
        file_ext = ext_map.get(ct, ".bin")
    file_path = os.path.join(FILES_DIR, f"{item_id}{file_ext}")

    content = await upload_file.read()
    content_hash = _hash_content(content)
    with open(file_path, "wb") as f:
        f.write(content)

    await db.execute(
        "UPDATE contentwall.items SET content_hash = :hash WHERE id = :id",
        {"hash": content_hash, "id": item_id},
    )
    return {
        "file_path": file_path,
        "content_hash": content_hash,
        "size": len(content),
        "content_type": upload_file.content_type or "application/octet-stream",
    }


async def get_media_file_info(item_id: str) -> Optional[dict]:
    """Return path + content_type for an audio/video item, if found on disk."""
    candidates = [
        (".mp3", "audio/mpeg"), (".m4a", "audio/mp4"),
        (".aac", "audio/aac"), (".ogg", "audio/ogg"),
        (".opus", "audio/ogg"), (".wav", "audio/wav"),
        (".mp4", "video/mp4"), (".webm", "video/webm"),
        (".mkv", "video/x-matroska"), (".mov", "video/quicktime"),
    ]
    for ext, ct in candidates:
        fp = os.path.join(FILES_DIR, f"{item_id}{ext}")
        if os.path.exists(fp):
            return {"file_path": fp, "content_type": ct, "size": os.path.getsize(fp)}
    fp = os.path.join(FILES_DIR, f"{item_id}.bin")
    if os.path.exists(fp):
        return {"file_path": fp, "content_type": "application/octet-stream", "size": os.path.getsize(fp)}
    return None


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------


from .models import Coupon, CreateCoupon, Tip  # noqa: E402  (kept after schema)


async def create_coupon(item_id: str, data: CreateCoupon) -> Coupon:
    coupon = Coupon(
        id=urlsafe_short_hash(),
        item_id=item_id,
        code=data.code.upper(),
        discount_percent=data.discount_percent,
        discount_fixed_sats=data.discount_fixed_sats,
        uses_remaining=data.uses_remaining,
        uses_count=0,
        expires_at=data.expires_at,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    await db.insert("contentwall.coupons", coupon)
    return coupon


async def get_coupon(item_id: str, code: str) -> Optional[Coupon]:
    return await db.fetchone(
        """
        SELECT * FROM contentwall.coupons
        WHERE item_id = :id AND UPPER(code) = :code
        """,
        {"id": item_id, "code": code.upper()},
        Coupon,
    )


async def list_coupons(item_id: str) -> list[Coupon]:
    return await db.fetchall(
        "SELECT * FROM contentwall.coupons WHERE item_id = :id ORDER BY created_at DESC",
        {"id": item_id},
        model=Coupon,
    )


async def delete_coupon(coupon_id: str) -> None:
    await db.execute(
        "DELETE FROM contentwall.coupons WHERE id = :id", {"id": coupon_id}
    )


async def consume_coupon(coupon: Coupon) -> bool:
    """
    Atomic-ish consumption: decrement uses_remaining if non-unlimited,
    bump uses_count. Returns True if successful, False if exhausted/expired.
    """
    # Expiry check
    if coupon.expires_at:
        exp = datetime.fromisoformat(coupon.expires_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > exp:
            return False
    # Uses check
    if coupon.uses_remaining == 0:
        return False
    new_remaining = (
        coupon.uses_remaining - 1 if coupon.uses_remaining > 0 else -1
    )
    await db.execute(
        """
        UPDATE contentwall.coupons
        SET uses_count = COALESCE(uses_count, 0) + 1,
            uses_remaining = :remaining
        WHERE id = :id
        """,
        {"remaining": new_remaining, "id": coupon.id},
    )
    return True


def apply_coupon_to_amount(amount: int, coupon: Coupon) -> int:
    """
    Return the discounted amount. Percent and fixed are summed:
    e.g. 30% off + 10 sat off on 100 sat = 60 sat.
    Floor at 1 sat (never zero — paywalls require an invoice).
    """
    out = amount
    if coupon.discount_percent and coupon.discount_percent > 0:
        out = out - (out * coupon.discount_percent // 100)
    if coupon.discount_fixed_sats and coupon.discount_fixed_sats > 0:
        out = out - coupon.discount_fixed_sats
    return max(1, out)


# ---------------------------------------------------------------------------
# Tips
# ---------------------------------------------------------------------------


async def record_tip(
    item_id: str,
    tip_payment_hash: str,
    amount_sats: int,
    paywall_payment_hash: Optional[str] = None,
) -> Tip:
    tip = Tip(
        id=urlsafe_short_hash(),
        item_id=item_id,
        paywall_payment_hash=paywall_payment_hash,
        tip_payment_hash=tip_payment_hash,
        amount_sats=amount_sats,
        paid_at=datetime.now(timezone.utc).isoformat(),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    await db.insert("contentwall.tips", tip)
    return tip


async def list_tips_for_item(item_id: str) -> list[Tip]:
    return await db.fetchall(
        "SELECT * FROM contentwall.tips WHERE item_id = :id ORDER BY paid_at DESC",
        {"id": item_id},
        model=Tip,
    )


async def get_tips_total(wallet_ids: list[str]) -> int:
    """Sum of all tips across a user's items, for the dashboard."""
    if not wallet_ids:
        return 0
    q = ",".join([f"'{w}'" for w in wallet_ids])
    row = await db.fetchone(
        f"""
        SELECT COALESCE(SUM(t.amount_sats), 0) AS total
        FROM contentwall.tips t
        JOIN contentwall.items i ON i.id = t.item_id
        WHERE i.wallet IN ({q})
        """
    )
    if not row:
        return 0
    return int(row["total"] or 0)


# ---------------------------------------------------------------------------
# "My purchases" lookup (anonymous)
# ---------------------------------------------------------------------------


async def get_purchases_by_hashes(payment_hashes: list[str]) -> list[dict]:
    """
    Given a list of payment_hashes (which the client tracks in localStorage),
    return the matching items + token + expiry. We don't expose anything that
    isn't already tied to a successful payment, so this remains safe to expose
    without auth.
    """
    if not payment_hashes:
        return []
    # SQLite doesn't support tuple binding well; do it ourselves but safely.
    safe = [h for h in payment_hashes if all(c in "0123456789abcdefABCDEF" for c in h)]
    if not safe:
        return []
    placeholders = ",".join(f"'{h}'" for h in safe)
    rows = await db.fetchall(
        f"""
        SELECT
            p.item_id, p.payment_hash, p.amount_paid, p.paid_at,
            p.expires_at, p.views_count,
            i.title, i.description, i.content_type,
            i.access_signing_key, i.max_views, i.archived_at
        FROM contentwall.payments p
        JOIN contentwall.items i ON i.id = p.item_id
        WHERE p.payment_hash IN ({placeholders})
        ORDER BY p.paid_at DESC
        """
    )
    out = []
    for r in rows:
        token = make_access_token(
            r["access_signing_key"] or "", r["item_id"], r["payment_hash"]
        )
        out.append({
            "item_id": r["item_id"],
            "title": r["title"],
            "description": r["description"],
            "content_type": r["content_type"],
            "payment_hash": r["payment_hash"],
            "amount_paid": int(r["amount_paid"] or 0),
            "paid_at": r["paid_at"],
            "expires_at": r["expires_at"],
            "views_count": int(r["views_count"] or 0),
            "max_views": int(r["max_views"] or 0),
            "archived": r["archived_at"] is not None,
            "token": token,
        })
    return out

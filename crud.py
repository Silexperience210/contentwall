"""
ContentWall CRUD - following the exact paywall extension pattern.
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timezone

from lnbits.db import Database
from lnbits.helpers import urlsafe_short_hash

from .models import CreateItem, Item, Payment

db = Database("ext_contentwall")

FILES_DIR = os.path.join("data", "contentwall", "files")


def _ensure_files_dir():
    os.makedirs(FILES_DIR, exist_ok=True)


def _hash_content(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


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
    )
    await db.insert("contentwall.items", item)
    return item


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
        file_ext = ".bin"
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


async def get_item(item_id: str) -> Item | None:
    row = await db.fetchone(
        "SELECT * FROM contentwall.items WHERE id = :id",
        {"id": item_id},
        Item,
    )
    return row


async def get_items(wallet_ids: list[str]) -> list[Item]:
    if not wallet_ids:
        return []
    q = ",".join([f"'{w}'" for w in wallet_ids])
    rows = await db.fetchall(
        f"SELECT * FROM contentwall.items WHERE wallet IN ({q})",
        model=Item,
    )
    return rows


async def delete_item(item_id: str) -> None:
    for ext in [".article", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bin"]:
        fp = os.path.join(FILES_DIR, f"{item_id}{ext}")
        if os.path.exists(fp):
            os.remove(fp)
    await db.execute(
        "DELETE FROM contentwall.payments WHERE item_id = :id", {"id": item_id}
    )
    await db.execute(
        "DELETE FROM contentwall.items WHERE id = :id", {"id": item_id}
    )


async def record_payment(item_id: str, payment_hash: str, amount_paid: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payment_id = urlsafe_short_hash()
    payment = Payment(
        id=payment_id,
        item_id=item_id,
        payment_hash=payment_hash,
        amount_paid=amount_paid,
        paid_at=now,
        created_at=now,
    )
    await db.insert("contentwall.payments", payment)


async def get_payment_timestamp(item_id: str, payment_hash: str) -> str | None:
    row = await db.fetchone(
        "SELECT paid_at FROM contentwall.payments WHERE item_id = :item_id AND payment_hash = :payment_hash",
        {"item_id": item_id, "payment_hash": payment_hash},
    )
    if not row:
        return None
    # fetchone without a model returns a RowMapping (dict-like)
    paid_at = row["paid_at"]
    if paid_at is None:
        return None
    return paid_at if isinstance(paid_at, str) else paid_at.isoformat()


async def has_paid(item_id: str, payment_hash: str) -> bool:
    row = await db.fetchone(
        "SELECT 1 FROM contentwall.payments WHERE item_id = :item_id AND payment_hash = :payment_hash",
        {"item_id": item_id, "payment_hash": payment_hash},
    )
    return row is not None


async def get_payment_amount(item_id: str, payment_hash: str) -> int:
    row = await db.fetchone(
        "SELECT amount_paid FROM contentwall.payments WHERE item_id = :item_id AND payment_hash = :payment_hash",
        {"item_id": item_id, "payment_hash": payment_hash},
    )
    if not row:
        return 0
    # fetchone without a model returns a RowMapping (dict-like)
    return int(row["amount_paid"]) if row["amount_paid"] is not None else 0


async def get_article_content(item_id: str) -> str | None:
    fp = os.path.join(FILES_DIR, f"{item_id}.article")
    if not os.path.exists(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        return f.read()


async def get_image_file_info(item_id: str) -> dict | None:
    for ext, ct in [
        (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
        (".png", "image/png"), (".gif", "image/gif"), (".webp", "image/webp"),
    ]:
        fp = os.path.join(FILES_DIR, f"{item_id}{ext}")
        if os.path.exists(fp):
            return {"file_path": fp, "content_type": ct, "size": os.path.getsize(fp)}
    return None


async def get_image_base64(item_id: str) -> dict | None:
    info = await get_image_file_info(item_id)
    if not info:
        return None
    with open(info["file_path"], "rb") as f:
        data = f.read()
    return {"data": base64.b64encode(data).decode("ascii"), "content_type": info["content_type"]}

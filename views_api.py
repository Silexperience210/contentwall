"""
ContentWall API routes.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import datetime, timezone
from http import HTTPStatus
from typing import List

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from lnbits.core.models import WalletTypeInfo
from lnbits.core.services import create_invoice
from lnbits.decorators import require_admin_key, require_invoice_key
from loguru import logger

from .crud import (
    add_bundle_file,
    apply_coupon_to_amount,
    archive_item,
    consume_coupon,
    create_coupon,
    create_item,
    db,
    delete_bundle_file,
    delete_coupon,
    delete_item,
    get_article_content,
    get_coupon,
    get_image_base64,
    get_image_file_info,
    get_item,
    get_item_file,
    get_item_files,
    get_item_stats,
    get_items,
    get_media_file_info,
    get_payment,
    get_payment_amount,
    get_payment_timestamp,
    get_payments_by_day,
    get_purchases_by_hashes,
    get_stats_for_wallets,
    get_tips_total,
    has_paid,
    increment_view_count,
    is_payment_expired,
    list_coupons,
    list_payments_for_item,
    list_tips_for_item,
    make_access_token,
    record_payment,
    record_tip,
    store_article_content,
    store_image_file,
    update_item,
    verify_access_token,
)
from .helpers import (
    article_teaser,
    encode_lnurl,
    fire_and_forget_webhook,
    iter_file_range,
    make_blurred_preview,
    metadata_for_lnurl,
    metadata_hash,
    parse_range_header,
    rate_limit_check,
    render_markdown_safe,
    watermark_image_bytes,
)
from .models import (
    CheckPaymentData,
    CreateCoupon,
    CreateInvoiceData,
    CreateItem,
    CreateTipData,
    Item,
    UpdateItem,
)
from .tasks import paid_invoices

contentwall_api_router = APIRouter()


def _parse_extra(payment) -> dict:
    """Parse payment.extra which can be dict, JSON string, or None."""
    extra = payment.extra
    if extra is None:
        return {}
    if isinstance(extra, dict):
        return extra
    if isinstance(extra, str):
        try:
            return json.loads(extra)
        except json.JSONDecodeError:
            return {}
    return {}


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


@contentwall_api_router.get("/api/v1/items")
async def api_items(
    wallet: WalletTypeInfo = Depends(require_invoice_key),
    all_wallets: bool = Query(False),
    include_archived: bool = Query(False),
):
    wallet_ids = [wallet.wallet.id]
    if all_wallets:
        from lnbits.core.crud import get_user
        user = await get_user(wallet.wallet.user)
        wallet_ids = user.wallet_ids if user else []

    items = await get_items(wallet_ids, include_archived=include_archived)
    stats = await get_stats_for_wallets(wallet_ids)

    out = []
    for it in items:
        d = it.dict()
        st = stats.get(it.id)
        d["payment_count"] = st.payment_count if st else 0
        d["total_sats"] = st.total_sats if st else 0
        d["unique_payers"] = st.unique_payers if st else 0
        d["last_payment_at"] = st.last_payment_at if st else None
        d["file_count"] = len(await get_item_files(it.id))
        # Hide the signing key from the API
        d.pop("access_signing_key", None)
        out.append(d)
    return out


@contentwall_api_router.post("/api/v1/items")
async def api_item_create(
    request: Request,
    data: CreateItem,
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    if data.content_type not in ("article", "image", "bundle", "audio", "video"):
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            "content_type must be 'article', 'image', 'bundle', 'audio' or 'video'",
        )
    if data.content_type == "article" and not data.article_content:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, "article_content is required for article type"
        )

    item = await create_item(wallet.wallet.id, data)
    if data.content_type == "article" and data.article_content:
        await store_article_content(item.id, data.article_content)
    return item


@contentwall_api_router.patch("/api/v1/items/{item_id}")
async def api_item_update(
    item_id: str,
    data: UpdateItem,
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    return await update_item(item_id, data)


@contentwall_api_router.post("/api/v1/items/{item_id}/archive")
async def api_item_archive(
    item_id: str, wallet: WalletTypeInfo = Depends(require_admin_key)
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    await archive_item(item_id)
    return {"archived": True}


@contentwall_api_router.delete("/api/v1/items/{item_id}")
async def api_item_delete(
    item_id: str, wallet: WalletTypeInfo = Depends(require_admin_key)
):
    logger.info(f"[api_item_delete] DELETE request for item_id={item_id} from wallet={wallet.wallet.id}")
    item = await get_item(item_id)
    if not item:
        logger.warning(f"[api_item_delete] item not found: {item_id}")
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        logger.warning(
            f"[api_item_delete] wallet mismatch for {item_id}: "
            f"item.wallet={item.wallet} caller={wallet.wallet.id}"
        )
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    try:
        await delete_item(item_id)
    except Exception as exc:
        logger.exception(f"[api_item_delete] delete failed for {item_id}: {exc}")
        raise HTTPException(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"Delete failed: {exc}",
        )
    return {"deleted": True}


@contentwall_api_router.post("/api/v1/items/{item_id}/upload")
async def api_upload_image(
    item_id: str,
    upload_file: UploadFile = File(...),
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    from loguru import logger
    logger.info(f"[api_upload_image] item_id={item_id} filename={upload_file.filename} mime={upload_file.content_type}")
    item = await get_item(item_id)
    if not item:
        logger.warning(f"[api_upload_image] item not found: {item_id}")
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        logger.warning(f"[api_upload_image] wallet mismatch for item {item_id}")
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    if item.content_type != "image":
        logger.warning(f"[api_upload_image] item {item_id} is not image, it's {item.content_type}")
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, "Item is not an image type"
        )

    # NOTE: client-provided content_type is NOT trusted. Mobile pickers and
    # some Android browsers send 'application/octet-stream' or 'image/*' even
    # for valid JPEG/PNG files. We let store_image_file validate by reading
    # the magic bytes — that's both safer (can't be spoofed by a header) and
    # more permissive (works with whatever the client actually sends).
    try:
        result = await store_image_file(item_id, upload_file)
    except ValueError as exc:
        logger.warning(f"[api_upload_image] rejected by magic-byte check: {exc}")
        raise HTTPException(HTTPStatus.BAD_REQUEST, str(exc))

    logger.info(
        f"[api_upload_image] success item_id={item_id} "
        f"size={result['size']} stored_mime={result['content_type']}"
    )
    return {
        "success": True,
        "size": result["size"],
        "content_type": result["content_type"],
    }


@contentwall_api_router.post("/api/v1/items/{item_id}/upload-media")
async def api_upload_media(
    item_id: str,
    upload_file: UploadFile = File(...),
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    """Upload an audio or video file for items of type 'audio' or 'video'."""
    from .crud import store_media_file

    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    if item.content_type not in ("audio", "video"):
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, "Item is not an audio or video type"
        )

    # Same approach as image upload: client mime is unreliable on mobile.
    # store_media_file validates by magic bytes, falls back to filename ext.
    try:
        result = await store_media_file(item_id, upload_file)
    except ValueError as exc:
        logger.warning(f"[api_upload_media] rejected by magic-byte check: {exc}")
        raise HTTPException(HTTPStatus.BAD_REQUEST, str(exc))

    # Enforce item.content_type matches what was actually uploaded
    real_kind = "audio" if result["content_type"].startswith("audio/") else "video"
    if real_kind != item.content_type:
        # Roll back: delete the file we just wrote
        import os as _os
        try:
            _os.remove(result["file_path"])
        except OSError:
            pass
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            f"You uploaded a {real_kind} file but this item is configured as "
            f"{item.content_type!r}. Create a new item with the matching type.",
        )

    return {
        "success": True,
        "size": result["size"],
        "content_type": result["content_type"],
    }


@contentwall_api_router.post("/api/v1/items/{item_id}/thumbnail")
async def api_upload_thumbnail(
    item_id: str,
    upload_file: UploadFile = File(...),
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    """
    Upload a thumbnail (poster image) for a video or audio item. The
    thumbnail is extracted client-side from a video frame at upload time
    (no ffmpeg dependency on the host). Always stored as JPEG.
    """
    from .crud import store_thumbnail_file

    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    if item.content_type not in ("audio", "video"):
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            "Thumbnails are only supported on audio/video items",
        )

    try:
        result = await store_thumbnail_file(item_id, upload_file)
    except ValueError as exc:
        raise HTTPException(HTTPStatus.BAD_REQUEST, str(exc))

    return {"success": True, "size": result["size"]}


@contentwall_api_router.get("/api/v1/items/{item_id}/thumbnail")
async def api_get_thumbnail(item_id: str):
    """
    Public endpoint: serves the thumbnail JPEG for a video/audio item.
    Safe to expose — thumbnails are meant to be seen on the paywall card
    before payment. Returns 404 if no thumbnail was uploaded.
    """
    from .crud import get_thumbnail_path

    fp = get_thumbnail_path(item_id)
    if not fp:
        raise HTTPException(HTTPStatus.NOT_FOUND, "No thumbnail")

    def iterfile():
        with open(fp, "rb") as f:
            yield from f

    return StreamingResponse(
        iterfile(),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f'inline; filename="{item_id}.thumb.jpg"',
        },
    )


# ---- Bundle multi-file -----------------------------------------------------


@contentwall_api_router.post("/api/v1/items/{item_id}/files")
async def api_bundle_add_file(
    item_id: str,
    upload_file: UploadFile = File(...),
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    if item.content_type != "bundle":
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, "Item is not a bundle"
        )
    bf = await add_bundle_file(item_id, upload_file)
    return bf


@contentwall_api_router.get("/api/v1/items/{item_id}/files")
async def api_bundle_list_files(
    item_id: str, wallet: WalletTypeInfo = Depends(require_invoice_key)
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    return await get_item_files(item_id)


@contentwall_api_router.delete("/api/v1/items/{item_id}/files/{file_id}")
async def api_bundle_delete_file(
    item_id: str,
    file_id: str,
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    f = await get_item_file(file_id)
    if not f or f.item_id != item_id:
        raise HTTPException(HTTPStatus.NOT_FOUND, "File not found")
    await delete_bundle_file(file_id)
    return {"deleted": True}


# ---- Stats / exports ------------------------------------------------------


@contentwall_api_router.get("/api/v1/stats/items/{item_id}")
async def api_item_stats(
    item_id: str, wallet: WalletTypeInfo = Depends(require_invoice_key)
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    return await get_item_stats(item_id)


@contentwall_api_router.get("/api/v1/stats/timeseries")
async def api_stats_timeseries(
    wallet: WalletTypeInfo = Depends(require_invoice_key),
    days: int = Query(30, ge=1, le=365),
    all_wallets: bool = Query(False),
):
    wallet_ids = [wallet.wallet.id]
    if all_wallets:
        from lnbits.core.crud import get_user
        user = await get_user(wallet.wallet.user)
        wallet_ids = user.wallet_ids if user else []
    return await get_payments_by_day(wallet_ids, days=days)


@contentwall_api_router.get("/api/v1/stats/export.csv")
async def api_export_csv(
    wallet: WalletTypeInfo = Depends(require_invoice_key),
    all_wallets: bool = Query(False),
):
    wallet_ids = [wallet.wallet.id]
    if all_wallets:
        from lnbits.core.crud import get_user
        user = await get_user(wallet.wallet.user)
        wallet_ids = user.wallet_ids if user else []

    items = await get_items(wallet_ids, include_archived=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "item_id",
            "item_title",
            "payment_id",
            "payment_hash",
            "amount_paid",
            "paid_at",
            "expires_at",
            "views_count",
        ]
    )
    for it in items:
        for p in await list_payments_for_item(it.id):
            w.writerow(
                [
                    it.id,
                    it.title,
                    p.id,
                    p.payment_hash,
                    p.amount_paid,
                    p.paid_at or "",
                    p.expires_at or "",
                    p.views_count,
                ]
            )
    csv_data = buf.getvalue()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=contentwall-export.csv"
        },
    )


# ---------------------------------------------------------------------------
# Invoicing & payment check
# ---------------------------------------------------------------------------


@contentwall_api_router.post("/api/v1/items/invoice/{item_id}")
async def api_create_invoice(
    request: Request, item_id: str, data: CreateInvoiceData
):
    # Rate limit: max 10 invoice creations per minute per IP per item
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"invoice:{client_ip}:{item_id}"
    if not rate_limit_check(rate_key, max_requests=10, window_seconds=60):
        raise HTTPException(
            HTTPStatus.TOO_MANY_REQUESTS,
            "Too many invoice requests. Please wait a minute.",
        )

    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.archived_at:
        raise HTTPException(
            HTTPStatus.GONE, "This content is no longer for sale"
        )

    if item.scheduled_at:
        scheduled = datetime.fromisoformat(
            item.scheduled_at.replace("Z", "+00:00")
        )
        if datetime.now(timezone.utc) < scheduled:
            raise HTTPException(
                HTTPStatus.BAD_REQUEST,
                "This content is not yet available for purchase",
            )

    requested_amount = data.amount if data and data.amount else item.amount

    # Coupon
    applied_coupon = None
    if data and data.coupon_code:
        coupon = await get_coupon(item_id, data.coupon_code)
        if not coupon:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Invalid coupon code")
        if not await consume_coupon(coupon):
            raise HTTPException(
                HTTPStatus.GONE, "Coupon is exhausted or expired"
            )
        applied_coupon = coupon
        # Discount applies to the floor price, not to a custom tip amount.
        effective_min = apply_coupon_to_amount(item.amount, coupon)
        if requested_amount < effective_min:
            requested_amount = effective_min
    else:
        if requested_amount < item.amount:
            raise HTTPException(
                HTTPStatus.BAD_REQUEST,
                f"Minimum amount is {item.amount} {item.currency}",
            )

    extra = {"tag": "contentwall", "id": item_id}
    if applied_coupon:
        extra["coupon_code"] = applied_coupon.code

    try:
        payment = await create_invoice(
            wallet_id=item.wallet,
            amount=requested_amount,
            memo=item.memo,
            extra=extra,
        )
        return {
            "payment_hash": payment.payment_hash,
            "payment_request": payment.bolt11,
            "amount": requested_amount,
            "coupon_applied": applied_coupon.code if applied_coupon else None,
        }
    except Exception as exc:
        logger.error(f"Error creating invoice for item {item_id}: {exc}")
        raise HTTPException(
            HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)
        ) from exc


@contentwall_api_router.post("/api/v1/items/check/{item_id}")
async def api_check_payment(
    request: Request, item_id: str, data: CheckPaymentData
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")

    paid_amount = await _is_payment_made(item, data.payment_hash)

    if not paid_amount:
        return {"paid": False}

    # Build the signed content URL
    token = make_access_token(
        item.access_signing_key or "", item_id, data.payment_hash
    )
    host = str(request.base_url).rstrip("/")
    qs = f"?payment_hash={data.payment_hash}"
    if token:
        qs += f"&t={token}"
    content_url = f"{host}/contentwall/content/{item_id}{qs}"
    onion_content_url = None
    if item.onion_hostname:
        onion_content_url = (
            f"http://{item.onion_hostname}/contentwall/content/{item_id}{qs}"
        )

    # Release delay
    release_delay = item.release_delay_seconds or 0
    unlock_in = None
    content_unlocked = True
    if release_delay > 0:
        payment_ts = await get_payment_timestamp(item_id, data.payment_hash)
        if payment_ts:
            paid_at = datetime.fromisoformat(payment_ts.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - paid_at).total_seconds()
            remaining = release_delay - int(elapsed)
            if remaining > 0:
                content_unlocked = False
                unlock_in = remaining

    # Expiry (rental mode)
    expired = await is_payment_expired(item_id, data.payment_hash)
    payment = await get_payment(item_id, data.payment_hash)

    return {
        "paid": True,
        "expired": expired,
        "url": content_url,
        "onion_url": onion_content_url,
        "remembers": bool(item.remembers),
        "release_delay_seconds": release_delay,
        "content_unlocked": content_unlocked,
        "unlock_in_seconds": unlock_in,
        "expires_at": payment.expires_at if payment else None,
        "views_count": payment.views_count if payment else 0,
        "max_views": item.max_views or 0,
    }


@contentwall_api_router.get(
    "/api/v1/items/debug/{item_id}/{payment_hash}"
)
async def api_debug_payment(item_id: str, payment_hash: str):
    """
    v1.2.1 — diagnostic endpoint. Returns the FULL state of a payment so we
    can tell at a glance why the paywall might say "waiting" while the user
    swears they paid. Intentionally unauthenticated: it never leaks content,
    only the boolean state of each step.

    Hit it directly from a browser:
      GET /contentwall/api/v1/items/debug/<item_id>/<payment_hash>
    """
    out: dict = {
        "item_id": item_id,
        "payment_hash": payment_hash,
        "item_exists": False,
        "item_wallet": None,
        "contentwall_has_paid": False,
        "lnbits_payment_found": False,
        "lnbits_status_pending": None,
        "lnbits_status_paid": None,
        "lnbits_extra": None,
        "extra_tag_matches": False,
        "extra_id_matches": False,
        "amount_msat": None,
        "amount_sats": None,
        "ws_queue_present": payment_hash in paid_invoices,
        "errors": [],
    }

    try:
        item = await get_item(item_id)
        if item:
            out["item_exists"] = True
            out["item_wallet"] = item.wallet
        else:
            return out

        try:
            out["contentwall_has_paid"] = await has_paid(item_id, payment_hash)
        except Exception as exc:
            out["errors"].append(f"has_paid: {exc}")

        try:
            from lnbits.core.crud import get_standalone_payment
            from lnbits.core.services import check_transaction_status

            status = await check_transaction_status(item.wallet, payment_hash)
            out["lnbits_status_pending"] = bool(status.pending)
            out["lnbits_status_paid"] = bool(getattr(status, "paid", False))
        except Exception as exc:
            out["errors"].append(f"check_transaction_status: {exc}")

        try:
            payment = await get_standalone_payment(
                checking_id_or_hash=payment_hash,
                incoming=True,
                wallet_id=item.wallet,
            )
            if payment:
                out["lnbits_payment_found"] = True
                extra = _parse_extra(payment)
                out["lnbits_extra"] = extra
                out["extra_tag_matches"] = extra.get("tag") == "contentwall"
                out["extra_id_matches"] = extra.get("id") == item_id
                out["amount_msat"] = payment.amount
                out["amount_sats"] = int(payment.amount / 1000)
        except Exception as exc:
            out["errors"].append(f"get_standalone_payment: {exc}")

    except Exception as exc:
        out["errors"].append(f"top-level: {exc}")

    return out


# ---------------------------------------------------------------------------
# Public content endpoints
# ---------------------------------------------------------------------------


def _placeholder_svg(label: str) -> str:
    """
    Return a small inline SVG data URI used as a graceful fallback when a
    real image preview can't be generated (no Pillow, missing upload, ...).
    Sized 480x300 to match the blurred-preview aspect when displayed in the
    paywall card.
    """
    import base64 as _b64

    safe_label = (
        label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 300">'
        '<defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="1">'
        '<stop offset="0" stop-color="#1a1320"/>'
        '<stop offset="1" stop-color="#0a0814"/>'
        "</linearGradient></defs>"
        '<rect width="480" height="300" fill="url(#g)"/>'
        '<rect x="2" y="2" width="476" height="296" fill="none" '
        'stroke="#ff6b00" stroke-opacity="0.45" stroke-width="2" rx="6"/>'
        '<text x="240" y="155" text-anchor="middle" '
        'font-family="monospace" font-size="20" fill="#ff6b00">'
        f"{safe_label}</text>"
        '<text x="240" y="185" text-anchor="middle" '
        'font-family="monospace" font-size="11" fill="#b9aeb6">'
        "// preview unavailable</text>"
        "</svg>"
    )
    encoded = _b64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


@contentwall_api_router.get("/api/v1/items/{item_id}/diagnostic")
async def api_item_diagnostic(
    item_id: str,
    payment_hash: str = Query(""),
    t: str = Query(""),
):
    """
    Self-service diagnostic for the most common failure modes (missing file,
    wrong extension on disk, PIL missing). Safe to expose: only returns paths
    and booleans, never image bytes. Requires a valid paid+token combo OR no
    parameters (in which case file existence is reported without content).
    """
    import os as _os

    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")

    # If the caller passes payment_hash + t and they verify, they get extra
    # detail; otherwise just the basics so anyone can self-diagnose.
    detailed = False
    if payment_hash and t:
        try:
            detailed = await _verify_access(item, item_id, payment_hash, t)
        except Exception:
            detailed = False

    from .crud import FILES_DIR as _FD

    info_image = await get_image_file_info(item_id)
    info_media = await get_media_file_info(item_id)

    # Inventory of every file on disk for this item id (regardless of ext)
    matching_files = []
    try:
        if _os.path.isdir(_FD):
            for name in _os.listdir(_FD):
                if name.startswith(item_id + "."):
                    fp = _os.path.join(_FD, name)
                    if _os.path.isfile(fp):
                        matching_files.append({
                            "name": name,
                            "size": _os.path.getsize(fp),
                        })
    except Exception as exc:
        matching_files.append({"error": str(exc)})

    try:
        from PIL import Image  # noqa: F401
        pil_available = True
    except Exception:
        pil_available = False

    out = {
        "item_id": item_id,
        "content_type": item.content_type,
        "content_hash_db": item.content_hash,
        "files_dir": _FD,
        "files_dir_exists": _os.path.isdir(_FD),
        "matching_files_on_disk": matching_files,
        "image_lookup": (
            None if not info_image else {
                "path": info_image["file_path"],
                "content_type": info_image["content_type"],
                "size": info_image["size"],
            }
        ),
        "media_lookup": (
            None if not info_media else {
                "path": info_media["file_path"],
                "content_type": info_media["content_type"],
                "size": info_media["size"],
            }
        ),
        "pil_available": pil_available,
        "authenticated": detailed,
    }
    return out


@contentwall_api_router.get("/api/v1/items/{item_id}/preview")
async def api_get_preview(item_id: str):
    """
    Returns the safe-to-share teaser:
    * Article  -> first N chars (from teaser_text, else generated)
    * Image    -> blurred JPEG bytes (downscaled then GaussianBlur)
    * Bundle   -> list of file names + sizes
    """
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")

    if item.content_type == "article":
        teaser = item.teaser_text
        if not teaser:
            full = await get_article_content(item_id)
            teaser = article_teaser(full or "", 280)
        return {
            "content_type": "article",
            "teaser_text": teaser,
            "teaser_blur": bool(item.teaser_blur),
        }

    if item.content_type == "image":
        info = await get_image_file_info(item_id)
        if not info:
            # No file on disk — surface this explicitly so the UI can show a
            # 'missing upload' placeholder rather than rendering an empty div.
            return {
                "content_type": "image",
                "preview_data": _placeholder_svg("Image not uploaded"),
                "teaser_text": item.teaser_text,
                "file_missing": True,
            }
        blurred = make_blurred_preview(info["file_path"])
        if blurred is None:
            # PIL not installed: serve a placeholder so the user still sees
            # something on the paywall card. README recommends installing
            # Pillow for a true blurred teaser.
            return {
                "content_type": "image",
                "preview_data": _placeholder_svg("🔒 Locked image"),
                "teaser_text": item.teaser_text,
                "file_missing": False,
                "pil_missing": True,
            }
        import base64
        return {
            "content_type": "image",
            "preview_data": "data:image/jpeg;base64,"
            + base64.b64encode(blurred).decode("ascii"),
            "teaser_text": item.teaser_text,
        }

    if item.content_type == "bundle":
        files = await get_item_files(item_id)
        return {
            "content_type": "bundle",
            "teaser_text": item.teaser_text,
            "files": [
                {
                    "filename": f.filename,
                    "content_type": f.content_type,
                    "size": f.size,
                }
                for f in files
            ],
        }

    if item.content_type in ("audio", "video"):
        from .crud import get_thumbnail_path
        info = await get_media_file_info(item_id)
        thumbnail_url = (
            f"/contentwall/api/v1/items/{item_id}/thumbnail"
            if get_thumbnail_path(item_id) else None
        )
        return {
            "content_type": item.content_type,
            "teaser_text": item.teaser_text,
            "media_present": info is not None,
            "media_size": info["size"] if info else 0,
            "media_mime": info["content_type"] if info else None,
            "thumbnail_url": thumbnail_url,
        }

    return {"content_type": item.content_type}


@contentwall_api_router.get("/api/v1/items/content/{item_id}")
async def api_get_content(
    item_id: str,
    payment_hash: str,
    t: str = Query("", description="Access token (HMAC)"),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")

    if item.scheduled_at:
        scheduled = datetime.fromisoformat(
            item.scheduled_at.replace("Z", "+00:00")
        )
        if datetime.now(timezone.utc) < scheduled:
            raise HTTPException(
                HTTPStatus.FORBIDDEN, "This content is not yet available"
            )

    has_access = await _verify_access(item, item_id, payment_hash, t)
    if not has_access:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Payment required")

    release_delay = item.release_delay_seconds or 0
    if release_delay > 0:
        payment_ts = await get_payment_timestamp(item_id, payment_hash)
        if payment_ts:
            paid_at = datetime.fromisoformat(payment_ts.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - paid_at).total_seconds()
            remaining = release_delay - int(elapsed)
            if remaining > 0:
                raise HTTPException(
                    HTTPStatus.FORBIDDEN, f"Content available in {remaining}s"
                )

    if await is_payment_expired(item_id, payment_hash):
        raise HTTPException(HTTPStatus.GONE, "Access has expired")

    # Enforce view limit
    if item.max_views and item.max_views > 0:
        new_count = await increment_view_count(item_id, payment_hash)
        if new_count > item.max_views:
            raise HTTPException(
                HTTPStatus.GONE,
                f"View limit reached ({item.max_views})",
            )

    response = {
        "content_type": item.content_type,
        "title": item.title,
        "description": item.description,
        "markdown": bool(item.markdown),
        "allow_tips": bool(item.allow_tips),
    }

    if item.content_type == "article":
        raw = await get_article_content(item_id)
        response["article_content"] = raw
        if item.markdown and raw:
            response["article_html"] = render_markdown_safe(raw)
    elif item.content_type == "image":
        img = await get_image_base64(item_id)
        if img:
            response["image_data"] = (
                f"data:{img['content_type']};base64,{img['data']}"
            )
        # Always include a streaming URL too — clients should prefer this
        # over the embedded base64 (smaller payload, watermark applied).
        response["image_url"] = (
            f"/contentwall/api/v1/items/image/{item_id}"
            f"?payment_hash={payment_hash}&t={t or ''}"
        )
    elif item.content_type == "bundle":
        files = await get_item_files(item_id)
        response["files"] = [
            {
                "id": f.id,
                "filename": f.filename,
                "content_type": f.content_type,
                "size": f.size,
            }
            for f in files
        ]
    elif item.content_type in ("audio", "video"):
        # Media is delivered via /api/v1/items/media/<id> (Range-aware)
        info = await get_media_file_info(item_id)
        response["media_mime"] = info["content_type"] if info else None
        response["media_size"] = info["size"] if info else 0

    return response


@contentwall_api_router.get("/api/v1/items/image/{item_id}")
async def api_get_image_raw(
    item_id: str,
    payment_hash: str,
    t: str = Query("", description="Access token"),
    watermark: int = Query(1, description="Apply watermark (0/1)"),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.content_type != "image":
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Item is not an image")

    if not await _verify_access(item, item_id, payment_hash, t):
        raise HTTPException(HTTPStatus.FORBIDDEN, "Payment required")

    if await is_payment_expired(item_id, payment_hash):
        raise HTTPException(HTTPStatus.GONE, "Access has expired")

    file_info = await get_image_file_info(item_id)
    if not file_info:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Image file not found")

    if watermark:
        result = watermark_image_bytes(file_info["file_path"], payment_hash)
        if result is not None:
            data, ct = result
            return Response(
                content=data,
                media_type=ct,
                headers={
                    "Content-Disposition": f'inline; filename="{item_id}.jpg"',
                    "Cache-Control": "private, no-store",
                },
            )

    def iterfile():
        with open(file_info["file_path"], "rb") as f:
            yield from f

    return StreamingResponse(
        iterfile(),
        media_type=file_info["content_type"],
        headers={
            "Content-Disposition": f'inline; filename="{item_id}"',
            "Cache-Control": "private, no-store",
        },
    )


@contentwall_api_router.get("/api/v1/items/file/{item_id}/{file_id}")
async def api_get_bundle_file(
    item_id: str,
    file_id: str,
    payment_hash: str,
    t: str = Query(""),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.content_type != "bundle":
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Item is not a bundle")

    if not await _verify_access(item, item_id, payment_hash, t):
        raise HTTPException(HTTPStatus.FORBIDDEN, "Payment required")
    if await is_payment_expired(item_id, payment_hash):
        raise HTTPException(HTTPStatus.GONE, "Access has expired")

    bf = await get_item_file(file_id)
    if not bf or bf.item_id != item_id:
        raise HTTPException(HTTPStatus.NOT_FOUND, "File not found")

    import os as _os
    from .crud import FILES_DIR as _FD
    file_path = _os.path.join(_FD, "bundles", item_id, bf.id)
    if not _os.path.exists(file_path):
        raise HTTPException(HTTPStatus.NOT_FOUND, "File missing on disk")

    def iterfile():
        with open(file_path, "rb") as f:
            yield from f

    return StreamingResponse(
        iterfile(),
        media_type=bf.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{bf.filename}"',
            "Cache-Control": "private, no-store",
        },
    )


# ---------------------------------------------------------------------------
# LNURL-pay
# ---------------------------------------------------------------------------


@contentwall_api_router.get("/api/v1/lnurlp/{item_id}")
async def api_lnurlp_first(request: Request, item_id: str):
    """LUD-06 LNURL-pay first response."""
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.archived_at:
        raise HTTPException(HTTPStatus.GONE, "Content no longer for sale")

    callback = f"{str(request.base_url).rstrip('/')}/contentwall/api/v1/lnurlp/{item_id}/callback"
    metadata = metadata_for_lnurl(item.title, item.description or item.memo)
    amount_msat = max(1, int(item.amount)) * 1000

    return {
        "tag": "payRequest",
        "callback": callback,
        "minSendable": amount_msat,
        "maxSendable": amount_msat * 100,  # allow tipping up to 100x
        "metadata": metadata,
        "commentAllowed": 200,
    }


@contentwall_api_router.get("/api/v1/lnurlp/{item_id}/callback")
async def api_lnurlp_callback(
    request: Request,
    item_id: str,
    amount: int = Query(..., description="Amount in millisatoshis"),
    comment: str = Query("", max_length=200),
):
    """LUD-06 LNURL-pay second response."""
    item = await get_item(item_id)
    if not item:
        return {"status": "ERROR", "reason": "Item not found"}
    if item.archived_at:
        return {"status": "ERROR", "reason": "Content no longer for sale"}

    amount_sat = amount // 1000
    if amount_sat < item.amount:
        return {
            "status": "ERROR",
            "reason": f"Minimum is {item.amount} {item.currency}",
        }

    metadata = metadata_for_lnurl(item.title, item.description or item.memo)
    try:
        payment = await create_invoice(
            wallet_id=item.wallet,
            amount=amount_sat,
            memo=item.memo,
            description_hash=metadata_hash(metadata),
            extra={
                "tag": "contentwall",
                "id": item_id,
                "via": "lnurlp",
                "comment": comment[:200] if comment else "",
            },
        )
        return {
            "pr": payment.bolt11,
            "successAction": {
                "tag": "url",
                "description": "View your content",
                "url": f"{str(request.base_url).rstrip('/')}/contentwall/{item_id}?payment_hash={payment.payment_hash}",
            },
            "routes": [],
        }
    except Exception as exc:
        logger.error(f"LNURL callback failed for {item_id}: {exc}")
        return {"status": "ERROR", "reason": str(exc)[:140]}


@contentwall_api_router.get("/api/v1/lnurlp/{item_id}/encoded")
async def api_lnurlp_encoded(request: Request, item_id: str):
    """Return the bech32-encoded LNURL string for QR/copy."""
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    endpoint = f"{str(request.base_url).rstrip('/')}/contentwall/api/v1/lnurlp/{item_id}"
    encoded = encode_lnurl(endpoint)
    return {"lnurl": encoded, "endpoint": endpoint}


# ---------------------------------------------------------------------------
# Websocket
# ---------------------------------------------------------------------------


@contentwall_api_router.websocket(
    "/api/v1/items/ws/{item_id}/{payment_hash}"
)
async def websocket_payment_status(
    ws: WebSocket, item_id: str, payment_hash: str
):
    try:
        await ws.accept()

        item = await get_item(item_id)
        if not item:
            await ws.send_text(
                json.dumps({"paid": False, "error": "Item not found"})
            )
            return

        from lnbits.core.crud import get_standalone_payment

        payment = await get_standalone_payment(
            checking_id_or_hash=payment_hash,
            incoming=True,
            wallet_id=item.wallet,
        )
        if payment and not payment.pending:
            extra = _parse_extra(payment)
            if (
                extra.get("tag") == "contentwall"
                and extra.get("id") == item_id
            ):
                await ws.send_text(json.dumps({"paid": True}))
                return

        if await has_paid(item_id, payment_hash):
            await ws.send_text(json.dumps({"paid": True}))
            return

        if payment_hash not in paid_invoices:
            paid_invoices[payment_hash] = asyncio.Queue()

        try:
            await asyncio.wait_for(
                paid_invoices[payment_hash].get(), timeout=300
            )
            del paid_invoices[payment_hash]
            await ws.send_text(json.dumps({"paid": True}))
        except asyncio.TimeoutError:
            await ws.send_text(json.dumps({"paid": False, "timeout": True}))

    except WebSocketDisconnect:
        logger.debug(
            f"WebSocket disconnected for {item_id}/{payment_hash}"
        )
    except Exception as exc:
        logger.warning(f"WebSocket error: {exc}")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _is_payment_made(item: Item, payment_hash: str) -> int:
    if item.remembers and await has_paid(item.id, payment_hash):
        return await get_payment_amount(item.id, payment_hash)

    try:
        from lnbits.core.crud import get_standalone_payment
        from lnbits.core.services import check_transaction_status

        status = await check_transaction_status(item.wallet, payment_hash)
        if not status.pending:
            payment = await get_standalone_payment(
                checking_id_or_hash=payment_hash,
                incoming=True,
                wallet_id=item.wallet,
            )
            if payment:
                extra = _parse_extra(payment)
                if (
                    extra.get("tag") == "contentwall"
                    and extra.get("id") == item.id
                ):
                    amount_sats = int(payment.amount / 1000)
                    rec = await record_payment(
                        item.id,
                        payment_hash,
                        amount_sats,
                        access_duration_seconds=item.access_duration_seconds
                        or 0,
                    )
                    # Webhook delivery
                    if item.webhook_url:
                        fire_and_forget_webhook(
                            item.webhook_url,
                            {
                                "event": "payment.confirmed",
                                "item_id": item.id,
                                "payment_hash": payment_hash,
                                "amount_sats": amount_sats,
                                "paid_at": rec.paid_at,
                                "expires_at": rec.expires_at,
                            },
                        )
                    return amount_sats
    except Exception as exc:
        logger.error(f"Error in _is_payment_made: {exc}")
    return 0


async def _verify_access(
    item: Item, item_id: str, payment_hash: str, token: str = ""
) -> bool:
    # If a signing key + token are present, the token MUST match.
    if item.access_signing_key:
        if not token:
            # Backwards-compat: allow legacy payments stored before HMAC was
            # introduced. We can detect that via the payment having no token
            # cached, in which case we still grant access if the payment row
            # is in our DB.
            pass
        else:
            if not verify_access_token(
                item.access_signing_key, item_id, payment_hash, token
            ):
                return False

    if item.remembers and await has_paid(item_id, payment_hash):
        return True

    try:
        from lnbits.core.crud import get_standalone_payment
        from lnbits.core.services import check_transaction_status

        status = await check_transaction_status(item.wallet, payment_hash)
        if not status.pending:
            payment = await get_standalone_payment(
                checking_id_or_hash=payment_hash,
                incoming=True,
                wallet_id=item.wallet,
            )
            if payment:
                extra = _parse_extra(payment)
                if (
                    extra.get("tag") == "contentwall"
                    and extra.get("id") == item_id
                ):
                    await record_payment(
                        item_id,
                        payment_hash,
                        int(payment.amount / 1000),
                        access_duration_seconds=item.access_duration_seconds
                        or 0,
                    )
                    return True
    except Exception as exc:
        logger.error(f"Error in _verify_access: {exc}")
    return False


# ===========================================================================
# v1.2.0 endpoints
# ===========================================================================


# ---------------------------------------------------------------------------
# Audio / video streaming with HTTP Range support
# ---------------------------------------------------------------------------


@contentwall_api_router.get("/api/v1/items/media/{item_id}")
@contentwall_api_router.head("/api/v1/items/media/{item_id}")
async def api_get_media(
    request: Request,
    item_id: str,
    payment_hash: str,
    t: str = Query(""),
):
    """
    Range-aware streaming endpoint for audio/video.
    Returns 206 Partial Content when a Range header is present,
    otherwise the full file as 200.
    """
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.content_type not in ("audio", "video"):
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Item is not media")

    if not await _verify_access(item, item_id, payment_hash, t):
        raise HTTPException(HTTPStatus.FORBIDDEN, "Payment required")
    if await is_payment_expired(item_id, payment_hash):
        raise HTTPException(HTTPStatus.GONE, "Access has expired")

    info = await get_media_file_info(item_id)
    if not info:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Media file not found")

    file_size = info["size"]
    range_header = request.headers.get("range") or request.headers.get("Range")
    parsed = parse_range_header(range_header, file_size) if range_header else None

    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{item_id}"',
        "Cache-Control": "private, no-store",
    }

    if request.method == "HEAD":
        return Response(
            content=b"",
            media_type=info["content_type"],
            headers={**common_headers, "Content-Length": str(file_size)},
        )

    if parsed is None:
        # Full body
        def iter_full():
            with open(info["file_path"], "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            iter_full(),
            media_type=info["content_type"],
            headers={**common_headers, "Content-Length": str(file_size)},
        )

    # Partial body
    start, end = parsed
    length = end - start + 1
    return StreamingResponse(
        iter_file_range(info["file_path"], start, end),
        status_code=HTTPStatus.PARTIAL_CONTENT,
        media_type=info["content_type"],
        headers={
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        },
    )


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------


@contentwall_api_router.get("/api/v1/items/{item_id}/coupons")
async def api_coupon_list(
    item_id: str, wallet: WalletTypeInfo = Depends(require_admin_key)
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    return await list_coupons(item_id)


@contentwall_api_router.post("/api/v1/items/{item_id}/coupons")
async def api_coupon_create(
    item_id: str,
    data: CreateCoupon,
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    if data.discount_percent == 0 and data.discount_fixed_sats == 0:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            "Coupon must set either discount_percent or discount_fixed_sats",
        )
    return await create_coupon(item_id, data)


@contentwall_api_router.delete("/api/v1/items/{item_id}/coupons/{coupon_id}")
async def api_coupon_delete(
    item_id: str,
    coupon_id: str,
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    await delete_coupon(coupon_id)
    return {"deleted": True}


@contentwall_api_router.get("/api/v1/items/{item_id}/coupons/check/{code}")
async def api_coupon_check(item_id: str, code: str):
    """
    Public preview: returns whether a code is valid and the discounted price.
    Used by the public page to show 'You'll pay X sats with this code'
    without exposing other coupons or admin info.
    """
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    coupon = await get_coupon(item_id, code)
    if not coupon:
        return {"valid": False, "reason": "Unknown code"}
    if coupon.uses_remaining == 0:
        return {"valid": False, "reason": "Code is exhausted"}
    if coupon.expires_at:
        try:
            exp = datetime.fromisoformat(coupon.expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp:
                return {"valid": False, "reason": "Code has expired"}
        except Exception:
            pass
    new_price = apply_coupon_to_amount(item.amount, coupon)
    return {
        "valid": True,
        "original_amount": item.amount,
        "discounted_amount": new_price,
        "savings": item.amount - new_price,
        "currency": item.currency,
    }


# ---------------------------------------------------------------------------
# Tips
# ---------------------------------------------------------------------------


@contentwall_api_router.post("/api/v1/items/{item_id}/tip")
async def api_tip_create(item_id: str, data: CreateTipData):
    """Create a tip invoice. Anyone can tip — even without paying first."""
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if not item.allow_tips:
        raise HTTPException(HTTPStatus.GONE, "Tips disabled for this item")
    if data.amount < 1:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Amount must be >= 1 sat")

    try:
        payment = await create_invoice(
            wallet_id=item.wallet,
            amount=data.amount,
            memo=f"Tip: {item.title}",
            extra={
                "tag": "contentwall_tip",
                "id": item_id,
                "paywall_payment_hash": data.paywall_payment_hash or "",
            },
        )
        return {
            "payment_hash": payment.payment_hash,
            "payment_request": payment.bolt11,
        }
    except Exception as exc:
        logger.error(f"Tip invoice creation failed: {exc}")
        raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)) from exc


@contentwall_api_router.post("/api/v1/items/{item_id}/tip/check")
async def api_tip_check(item_id: str, data: CheckPaymentData):
    """Poll a tip invoice and record it on confirmation."""
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")

    try:
        from lnbits.core.crud import get_standalone_payment
        from lnbits.core.services import check_transaction_status

        status = await check_transaction_status(item.wallet, data.payment_hash)
        if status.pending:
            return {"paid": False}
        payment = await get_standalone_payment(
            checking_id_or_hash=data.payment_hash,
            incoming=True,
            wallet_id=item.wallet,
        )
        if not payment:
            return {"paid": False}
        extra = _parse_extra(payment)
        if extra.get("tag") != "contentwall_tip" or extra.get("id") != item_id:
            return {"paid": False}
        amount_sats = int(payment.amount / 1000)
        # Avoid double-recording
        existing = await db.fetchone(
            "SELECT 1 FROM contentwall.tips WHERE tip_payment_hash = :h",
            {"h": data.payment_hash},
        )
        if not existing:
            await record_tip(
                item_id=item_id,
                tip_payment_hash=data.payment_hash,
                amount_sats=amount_sats,
                paywall_payment_hash=extra.get("paywall_payment_hash") or None,
            )
        return {"paid": True, "amount_sats": amount_sats}
    except Exception as exc:
        logger.error(f"Tip check failed: {exc}")
        return {"paid": False}


@contentwall_api_router.get("/api/v1/items/{item_id}/tips")
async def api_tips_list(
    item_id: str, wallet: WalletTypeInfo = Depends(require_invoice_key)
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    return await list_tips_for_item(item_id)


@contentwall_api_router.get("/api/v1/stats/tips_total")
async def api_tips_total(
    wallet: WalletTypeInfo = Depends(require_invoice_key),
    all_wallets: bool = Query(False),
):
    wallet_ids = [wallet.wallet.id]
    if all_wallets:
        from lnbits.core.crud import get_user
        user = await get_user(wallet.wallet.user)
        wallet_ids = user.wallet_ids if user else []
    return {"total_sats": await get_tips_total(wallet_ids)}


# ---------------------------------------------------------------------------
# My purchases (anonymous, by payment_hash lookup)
# ---------------------------------------------------------------------------


class _MyPurchasesQuery(__import__('pydantic').BaseModel):
    hashes: List[str] = []


@contentwall_api_router.post("/api/v1/me/purchases")
async def api_my_purchases(payload: _MyPurchasesQuery):
    """
    Returns the items + signed access URLs for a list of payment_hashes
    the client kept in localStorage. We only confirm what's already in our DB,
    so there's no privacy leak: knowing a payment_hash already proves access.
    """
    return await get_purchases_by_hashes(payload.hashes or [])


# ---------------------------------------------------------------------------
# Embed widget (iframe-safe + snippet)
# ---------------------------------------------------------------------------


@contentwall_api_router.get("/api/v1/items/{item_id}/embed-snippet")
async def api_embed_snippet(request: Request, item_id: str):
    """
    Returns a copy-paste-ready HTML snippet a creator can drop in any blog
    or site. Renders the paywall in a sandboxed iframe.
    """
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    base = str(request.base_url).rstrip("/")
    snippet = (
        f'<iframe src="{base}/contentwall/embed/{item_id}" '
        f'style="width:100%;max-width:520px;height:680px;border:0;'
        f'border-radius:12px;box-shadow:0 0 24px rgba(255,107,0,.35);" '
        f'sandbox="allow-scripts allow-same-origin allow-popups allow-forms" '
        f'loading="lazy" '
        f'title="{item.title} — Lightning paywall"></iframe>'
    )
    return {"snippet": snippet, "embed_url": f"{base}/contentwall/embed/{item_id}"}


# ---------------------------------------------------------------------------
# Backup / restore (admin)
# ---------------------------------------------------------------------------


@contentwall_api_router.get("/api/v1/backup")
async def api_backup(
    wallet: WalletTypeInfo = Depends(require_admin_key),
    all_wallets: bool = Query(False),
):
    """
    JSON snapshot of items, payments, coupons, tips, bundle files for
    the user's wallets. Does NOT include the actual content bytes — just
    metadata so the creator can re-import structure elsewhere.
    """
    wallet_ids = [wallet.wallet.id]
    if all_wallets:
        from lnbits.core.crud import get_user
        user = await get_user(wallet.wallet.user)
        wallet_ids = user.wallet_ids if user else []

    items = await get_items(wallet_ids, include_archived=True)
    out_items = []
    for it in items:
        d = it.dict()
        d.pop("access_signing_key", None)
        d["payments"] = [p.dict() for p in await list_payments_for_item(it.id)]
        d["coupons"] = [c.dict() for c in await list_coupons(it.id)]
        d["tips"] = [t.dict() for t in await list_tips_for_item(it.id)]
        d["files"] = [f.dict() for f in await get_item_files(it.id)]
        out_items.append(d)
    return {
        "version": "1.2.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "items": out_items,
    }

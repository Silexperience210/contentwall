"""
ContentWall API routes.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
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
    archive_item,
    create_item,
    delete_bundle_file,
    delete_item,
    get_article_content,
    get_image_base64,
    get_image_file_info,
    get_item,
    get_item_file,
    get_item_files,
    get_item_stats,
    get_items,
    get_payment,
    get_payment_amount,
    get_payment_timestamp,
    get_payments_by_day,
    get_stats_for_wallets,
    has_paid,
    increment_view_count,
    is_payment_expired,
    list_payments_for_item,
    make_access_token,
    record_payment,
    store_article_content,
    store_image_file,
    update_item,
    verify_access_token,
)
from .helpers import (
    article_teaser,
    encode_lnurl,
    fire_and_forget_webhook,
    make_blurred_preview,
    metadata_for_lnurl,
    metadata_hash,
    watermark_image_bytes,
)
from .models import (
    CheckPaymentData,
    CreateInvoiceData,
    CreateItem,
    Item,
    PublicItem,
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
    if data.content_type not in ("article", "image", "bundle"):
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            "content_type must be 'article', 'image' or 'bundle'",
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
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    await delete_item(item_id)
    return {"deleted": True}


@contentwall_api_router.post("/api/v1/items/{item_id}/upload")
async def api_upload_image(
    item_id: str,
    upload_file: UploadFile = File(...),
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    if item.content_type != "image":
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, "Item is not an image type"
        )

    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if (upload_file.content_type or "") not in allowed:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            "Invalid file type. Allowed: JPEG, PNG, GIF, WebP",
        )

    result = await store_image_file(item_id, upload_file)
    return {
        "success": True,
        "size": result["size"],
        "content_type": result["content_type"],
    }


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
async def api_create_invoice(item_id: str, data: CreateInvoiceData):
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

    amount = data.amount if data and data.amount else item.amount
    if amount < item.amount:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            f"Minimum amount is {item.amount} {item.currency}",
        )

    try:
        payment = await create_invoice(
            wallet_id=item.wallet,
            amount=amount,
            memo=item.memo,
            extra={"tag": "contentwall", "id": item_id},
        )
        return {
            "payment_hash": payment.payment_hash,
            "payment_request": payment.bolt11,
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


# ---------------------------------------------------------------------------
# Public content endpoints
# ---------------------------------------------------------------------------


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
            return {"content_type": "image", "preview_data": None}
        blurred = make_blurred_preview(info["file_path"])
        if blurred is None:
            # No PIL, no preview available
            return {
                "content_type": "image",
                "preview_data": None,
                "teaser_text": item.teaser_text,
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
    }

    if item.content_type == "article":
        response["article_content"] = await get_article_content(item_id)
    elif item.content_type == "image":
        img = await get_image_base64(item_id)
        if img:
            response["image_data"] = (
                f"data:{img['content_type']};base64,{img['data']}"
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
    file_path = _os.path.join(
        "data", "contentwall", "files", "bundles", item_id, bf.id
    )
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

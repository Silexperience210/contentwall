"""
ContentWall API routes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from http import HTTPStatus

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
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
    create_item,
    delete_item,
    get_article_content,
    get_image_base64,
    get_image_file_info,
    get_item,
    get_items,
    get_payment_timestamp,
    has_paid,
    record_payment,
    store_article_content,
    store_image_file,
)
from .models import CheckPaymentData, CreateInvoiceData, CreateItem, Item
from .tasks import paid_invoices

contentwall_api_router = APIRouter()


@contentwall_api_router.get("/api/v1/items")
async def api_items(
    wallet: WalletTypeInfo = Depends(require_invoice_key),
    all_wallets: bool = Query(False),
):
    wallet_ids = [wallet.wallet.id]
    if all_wallets:
        from lnbits.core.crud import get_user
        user = await get_user(wallet.wallet.user)
        wallet_ids = user.wallet_ids if user else []
    return await get_items(wallet_ids)


@contentwall_api_router.post("/api/v1/items")
async def api_item_create(
    request: Request,
    data: CreateItem,
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    if data.content_type not in ("article", "image"):
        raise HTTPException(HTTPStatus.BAD_REQUEST, "content_type must be 'article' or 'image'")
    if data.content_type == "article" and not data.article_content:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "article_content is required for article type")

    item = await create_item(wallet.wallet.id, data)
    if data.content_type == "article" and data.article_content:
        await store_article_content(item.id, data.article_content)
    return item


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
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Item is not an image type")

    allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if (upload_file.content_type or "") not in allowed_types:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Invalid file type. Allowed: JPEG, PNG, GIF, WebP")

    result = await store_image_file(item_id, upload_file)
    return {"success": True, "size": result["size"], "content_type": result["content_type"]}


@contentwall_api_router.delete("/api/v1/items/{item_id}")
async def api_item_delete(
    item_id: str,
    wallet: WalletTypeInfo = Depends(require_admin_key),
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.wallet != wallet.wallet.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Not your item")
    await delete_item(item_id)


@contentwall_api_router.post("/api/v1/items/invoice/{item_id}")
async def api_create_invoice(
    item_id: str,
    data: CreateInvoiceData,
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")

    if item.scheduled_at:
        scheduled = datetime.fromisoformat(item.scheduled_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) < scheduled:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "This content is not yet available for purchase")

    amount = data.amount if data and data.amount else item.amount
    if amount < item.amount:
        raise HTTPException(HTTPStatus.BAD_REQUEST, f"Minimum amount is {item.amount} {item.currency}")

    try:
        payment_hash, payment_request = await create_invoice(
            wallet_id=item.wallet,
            amount=amount,
            memo=item.memo,
            extra={"tag": "contentwall", "id": item_id},
        )
        return {
            "payment_hash": payment_hash,
            "payment_request": payment_request,
        }
    except Exception as exc:
        logger.error(f"Error creating invoice for item {item_id}: {exc}")
        raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)) from exc


@contentwall_api_router.post("/api/v1/items/check/{item_id}")
async def api_check_payment(
    request: Request,
    item_id: str,
    data: CheckPaymentData,
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")

    paid_amount = await _is_payment_made(item, data.payment_hash)

    if paid_amount:
        host = str(request.base_url).rstrip("/")
        content_url = f"{host}/contentwall/content/{item_id}?payment_hash={data.payment_hash}"
        onion_content_url = None
        if item.onion_hostname:
            onion_content_url = f"http://{item.onion_hostname}/contentwall/content/{item_id}?payment_hash={data.payment_hash}"

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

        return {
            "paid": True,
            "url": content_url,
            "onion_url": onion_content_url,
            "remembers": bool(item.remembers),
            "release_delay_seconds": release_delay,
            "content_unlocked": content_unlocked,
            "unlock_in_seconds": unlock_in,
        }

    return {"paid": False}


@contentwall_api_router.get("/api/v1/items/content/{item_id}")
async def api_get_content(
    item_id: str,
    payment_hash: str,
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")

    if item.scheduled_at:
        scheduled = datetime.fromisoformat(item.scheduled_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) < scheduled:
            raise HTTPException(HTTPStatus.FORBIDDEN, "This content is not yet available")

    has_access = await _verify_access(item, item_id, payment_hash)
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
                raise HTTPException(HTTPStatus.FORBIDDEN, f"Content available in {remaining}s")

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
            response["image_data"] = f"data:{img['content_type']};base64,{img['data']}"

    return response


@contentwall_api_router.get("/api/v1/items/image/{item_id}")
async def api_get_image_raw(
    item_id: str,
    payment_hash: str,
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Item not found")
    if item.content_type != "image":
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Item is not an image")

    has_access = await _verify_access(item, item_id, payment_hash)
    if not has_access:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Payment required")

    file_info = await get_image_file_info(item_id)
    if not file_info:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Image file not found")

    def iterfile():
        with open(file_info["file_path"], "rb") as f:
            yield from f

    return StreamingResponse(
        iterfile(),
        media_type=file_info["content_type"],
        headers={
            "Content-Disposition": f'inline; filename="{item_id}.jpg"',
            "Cache-Control": "private, no-store",
        },
    )


@contentwall_api_router.websocket("/api/v1/items/ws/{item_id}/{payment_hash}")
async def websocket_payment_status(
    ws: WebSocket,
    item_id: str,
    payment_hash: str,
):
    try:
        await ws.accept()

        item = await get_item(item_id)
        if not item:
            await ws.send_text(json.dumps({"paid": False, "error": "Item not found"}))
            return

        from lnbits.core.crud import get_standalone_payment
        payment = await get_standalone_payment(
            checking_id_or_hash=payment_hash,
            incoming=True,
            wallet_id=item.wallet,
        )
        if payment and not payment.pending:
            extra = _parse_extra(payment)
            if extra.get("tag") == "contentwall" and extra.get("id") == item_id:
                await ws.send_text(json.dumps({"paid": True}))
                return

        if await has_paid(item_id, payment_hash):
            await ws.send_text(json.dumps({"paid": True}))
            return

        if payment_hash not in paid_invoices:
            paid_invoices[payment_hash] = asyncio.Queue()

        try:
            paid_payment = await asyncio.wait_for(paid_invoices[payment_hash].get(), timeout=300)
            del paid_invoices[payment_hash]
            await ws.send_text(json.dumps({"paid": True}))
        except asyncio.TimeoutError:
            await ws.send_text(json.dumps({"paid": False, "timeout": True}))

    except WebSocketDisconnect:
        logger.debug(f"WebSocket disconnected for {item_id}/{payment_hash}")
    except Exception as exc:
        logger.warning(f"WebSocket error: {exc}")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


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


async def _is_payment_made(item: Item, payment_hash: str) -> int:
    if item.remembers and await has_paid(item.id, payment_hash):
        from .crud import get_payment_amount
        return await get_payment_amount(item.id, payment_hash)

    try:
        from lnbits.core.services import check_transaction_status
        from lnbits.core.crud import get_standalone_payment
        status = await check_transaction_status(item.wallet, payment_hash)
        if not status.pending:
            payment = await get_standalone_payment(
                checking_id_or_hash=payment_hash,
                incoming=True,
                wallet_id=item.wallet,
            )
            if payment:
                extra = _parse_extra(payment)
                if extra.get("tag") == "contentwall" and extra.get("id") == item.id:
                    amount_sats = int(payment.amount / 1000)
                    await record_payment(item.id, payment_hash, amount_sats)
                    return amount_sats
    except Exception as exc:
        logger.error(f"Error in _is_payment_made: {exc}")

    return 0


async def _verify_access(item: Item, item_id: str, payment_hash: str) -> bool:
    if item.remembers and await has_paid(item_id, payment_hash):
        return True

    try:
        from lnbits.core.services import check_transaction_status
        from lnbits.core.crud import get_standalone_payment
        status = await check_transaction_status(item.wallet, payment_hash)
        if not status.pending:
            payment = await get_standalone_payment(
                checking_id_or_hash=payment_hash,
                incoming=True,
                wallet_id=item.wallet,
            )
            if payment:
                extra = _parse_extra(payment)
                if extra.get("tag") == "contentwall" and extra.get("id") == item_id:
                    await record_payment(item_id, payment_hash, int(payment.amount / 1000))
                    return True
    except Exception as exc:
        logger.error(f"Error in _verify_access: {exc}")

    return False

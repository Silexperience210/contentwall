"""
ContentWall frontend routes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.exceptions import HTTPException
from fastapi.requests import Request
from lnbits.core.models import User
from lnbits.decorators import check_user_exists
from lnbits.helpers import template_renderer

from .crud import get_article_content, get_image_base64, get_item, has_paid, record_payment
from .models import Item, PublicItem

contentwall_generic_router = APIRouter()


def contentwall_renderer():
    return template_renderer(["contentwall/templates"])


@contentwall_generic_router.get("/")
async def index(request: Request, user: User = Depends(check_user_exists)):
    return contentwall_renderer().TemplateResponse(
        "contentwall/index.html", {"request": request, "user": user.json()}
    )


@contentwall_generic_router.get("/content/{item_id}")
async def view_content(
    request: Request, item_id: str, payment_hash: str | None = None
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Content not found")

    if item.scheduled_at:
        scheduled = datetime.fromisoformat(item.scheduled_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) < scheduled:
            raise HTTPException(status_code=403, detail="This content is not yet available")

    has_access = False
    payment_ts = None

    if payment_hash:
        from lnbits.core.crud import get_standalone_payment
        from lnbits.core.services import check_transaction_status

        try:
            status = await check_transaction_status(item.wallet, payment_hash)
            if not status.pending:
                payment = await get_standalone_payment(
                    checking_id_or_hash=payment_hash, incoming=True
                )
                if payment and payment.extra.get("tag") == "contentwall" and payment.extra.get("id") == item_id:
                    has_access = True
                    # payment.amount is in millisats; convert to sats for storage
                    await record_payment(item_id, payment_hash, int(payment.amount / 1000))
        except Exception:
            pass

        if not has_access:
            has_access = await has_paid(item_id, payment_hash)
            if has_access:
                from .crud import get_payment_timestamp
                payment_ts = await get_payment_timestamp(item_id, payment_hash)

    if not has_access:
        raise HTTPException(status_code=403, detail="Payment required")

    release_delay = item.release_delay_seconds or 0
    if release_delay > 0 and payment_ts:
        paid_at = datetime.fromisoformat(payment_ts.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - paid_at).total_seconds()
        remaining = release_delay - int(elapsed)
        if remaining > 0:
            raise HTTPException(status_code=403, detail=f"Content available in {remaining}s")

    article_content = None
    image_data = None
    if item.content_type == "article":
        article_content = await get_article_content(item_id)
    elif item.content_type == "image":
        img = await get_image_base64(item_id)
        if img:
            image_data = f"data:{img['content_type']};base64,{img['data']}"

    return contentwall_renderer().TemplateResponse(
        "contentwall/content.html",
        {
            "request": request,
            "item_id": item_id,
            "title": item.title,
            "description": item.description,
            "content_type": item.content_type,
            "article_content": article_content,
            "image_data": image_data,
            "payment_hash": payment_hash,
        },
    )


@contentwall_generic_router.get("/{item_id}")
async def display(request: Request, item_id: str):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Content not found")

    public_item = PublicItem(
        id=item.id,
        title=item.title,
        description=item.description,
        content_type=item.content_type,
        amount=item.amount,
        currency=item.currency,
        memo=item.memo,
        release_delay_seconds=item.release_delay_seconds or 0,
        scheduled_at=item.scheduled_at,
    )

    return contentwall_renderer().TemplateResponse(
        "contentwall/display.html",
        {
            "request": request,
            "item": public_item.json(),
            "item_id": item_id,
            "public_url": f"{request.base_url}contentwall/{item_id}".rstrip("/"),
            "onion_url": f"http://{item.onion_hostname}/contentwall/{item_id}" if item.onion_hostname else None,
        },
    )

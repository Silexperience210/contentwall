"""
ContentWall frontend routes (HTML pages).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.exceptions import HTTPException
from fastapi.requests import Request
from lnbits.core.models import User
from lnbits.decorators import check_user_exists
from lnbits.helpers import template_renderer

from .crud import (
    get_article_content,
    get_image_base64,
    get_item,
    get_item_files,
    has_paid,
    increment_view_count,
    is_payment_expired,
    record_payment,
    verify_access_token,
)
from .models import PublicItem

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
    request: Request,
    item_id: str,
    payment_hash: str | None = None,
    t: str | None = None,
):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Content not found")

    if item.scheduled_at:
        scheduled = datetime.fromisoformat(
            item.scheduled_at.replace("Z", "+00:00")
        )
        if datetime.now(timezone.utc) < scheduled:
            raise HTTPException(status_code=403, detail="Not yet available")

    has_access = False
    payment_ts = None

    if payment_hash:
        # HMAC check (when key+token present)
        if item.access_signing_key and t:
            if not verify_access_token(
                item.access_signing_key, item_id, payment_hash, t
            ):
                raise HTTPException(status_code=403, detail="Invalid token")

        from lnbits.core.crud import get_standalone_payment
        from lnbits.core.services import check_transaction_status

        try:
            status = await check_transaction_status(item.wallet, payment_hash)
            if not status.pending:
                payment = await get_standalone_payment(
                    checking_id_or_hash=payment_hash, incoming=True
                )
                if (
                    payment
                    and payment.extra.get("tag") == "contentwall"
                    and payment.extra.get("id") == item_id
                ):
                    has_access = True
                    await record_payment(
                        item_id,
                        payment_hash,
                        int(payment.amount / 1000),
                        access_duration_seconds=item.access_duration_seconds
                        or 0,
                    )
        except Exception:
            pass

        if not has_access:
            has_access = await has_paid(item_id, payment_hash)
            if has_access:
                from .crud import get_payment_timestamp
                payment_ts = await get_payment_timestamp(
                    item_id, payment_hash
                )

    if not has_access:
        raise HTTPException(status_code=403, detail="Payment required")

    release_delay = item.release_delay_seconds or 0
    if release_delay > 0 and payment_ts:
        paid_at = datetime.fromisoformat(payment_ts.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - paid_at).total_seconds()
        remaining = release_delay - int(elapsed)
        if remaining > 0:
            raise HTTPException(
                status_code=403,
                detail=f"Content available in {remaining}s",
            )

    if await is_payment_expired(item_id, payment_hash):
        raise HTTPException(status_code=410, detail="Access has expired")

    # Enforce view limit
    if item.max_views and item.max_views > 0:
        new_count = await increment_view_count(item_id, payment_hash)
        if new_count > item.max_views:
            raise HTTPException(
                status_code=410,
                detail=f"View limit reached ({item.max_views})",
            )

    article_content = None
    image_data = None
    bundle_files = []
    if item.content_type == "article":
        article_content = await get_article_content(item_id)
    elif item.content_type == "image":
        img = await get_image_base64(item_id)
        if img:
            image_data = (
                f"data:{img['content_type']};base64,{img['data']}"
            )
    elif item.content_type == "bundle":
        bundle_files = [
            {
                "id": f.id,
                "filename": f.filename,
                "content_type": f.content_type,
                "size": f.size,
            }
            for f in await get_item_files(item_id)
        ]

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
            "bundle_files": bundle_files,
            "payment_hash": payment_hash,
            "access_token": t or "",
        },
    )


@contentwall_generic_router.get("/{item_id}")
async def display(request: Request, item_id: str):
    item = await get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Content not found")
    if item.archived_at:
        raise HTTPException(
            status_code=410, detail="This content is no longer available"
        )

    files = await get_item_files(item_id) if item.content_type == "bundle" else []

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
        teaser_text=item.teaser_text,
        teaser_blur=bool(item.teaser_blur),
        access_duration_seconds=item.access_duration_seconds or 0,
        max_views=item.max_views or 0,
        file_count=len(files),
    )

    return contentwall_renderer().TemplateResponse(
        "contentwall/display.html",
        {
            "request": request,
            "item": public_item.json(),
            "item_id": item_id,
            "public_url": f"{request.base_url}contentwall/{item_id}".rstrip("/"),
            "onion_url": (
                f"http://{item.onion_hostname}/contentwall/{item_id}"
                if item.onion_hostname
                else None
            ),
        },
    )

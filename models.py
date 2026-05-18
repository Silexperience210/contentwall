"""
ContentWall models - used for both DB and API.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Item(BaseModel):
    id: str
    wallet: str
    title: str
    description: Optional[str] = ""
    content_type: str
    content_hash: Optional[str] = None
    amount: int
    currency: str = "sat"
    memo: str
    remembers: int = 1
    release_delay_seconds: int = 0
    scheduled_at: Optional[str] = None
    onion_hostname: Optional[str] = None
    created_at: Optional[str] = None

    # v1.1.0 fields
    teaser_text: Optional[str] = None
    teaser_blur: int = 1
    archived_at: Optional[str] = None
    access_duration_seconds: int = 0  # 0 = lifetime access
    access_signing_key: Optional[str] = None
    webhook_url: Optional[str] = None
    max_views: int = 0  # 0 = unlimited views per payment

    # v1.2.0 fields
    markdown: int = 0       # render article body as markdown
    allow_tips: int = 1     # show "tip the creator" form after unlock


class ItemFile(BaseModel):
    id: str
    item_id: str
    filename: str
    content_type: str
    size: int = 0
    content_hash: Optional[str] = None
    position: int = 0
    created_at: Optional[str] = None


class CreateItem(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    content_type: str = Field(..., pattern="^(article|image|bundle|audio|video)$")
    article_content: Optional[str] = Field(default=None, max_length=200000)
    amount: int = Field(..., ge=1)
    currency: str = Field(default="sat")
    memo: Optional[str] = Field(default=None, max_length=200)
    remembers: bool = Field(default=True)
    release_delay_seconds: int = Field(default=0, ge=0)
    scheduled_at: Optional[str] = Field(default=None)
    onion_hostname: Optional[str] = Field(default=None, max_length=500)

    # v1.1.0 fields
    teaser_text: Optional[str] = Field(default=None, max_length=1000)
    teaser_blur: bool = Field(default=True)
    access_duration_seconds: int = Field(default=0, ge=0)
    webhook_url: Optional[str] = Field(default=None, max_length=500)
    max_views: int = Field(default=0, ge=0)

    # v1.2.0 fields
    markdown: bool = Field(default=False)
    allow_tips: bool = Field(default=True)


class UpdateItem(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    amount: Optional[int] = Field(default=None, ge=1)
    currency: Optional[str] = None
    memo: Optional[str] = Field(default=None, max_length=200)
    remembers: Optional[bool] = None
    release_delay_seconds: Optional[int] = Field(default=None, ge=0)
    scheduled_at: Optional[str] = None
    onion_hostname: Optional[str] = Field(default=None, max_length=500)
    teaser_text: Optional[str] = Field(default=None, max_length=1000)
    teaser_blur: Optional[bool] = None
    access_duration_seconds: Optional[int] = Field(default=None, ge=0)
    webhook_url: Optional[str] = Field(default=None, max_length=500)
    max_views: Optional[int] = Field(default=None, ge=0)
    archived: Optional[bool] = None
    markdown: Optional[bool] = None
    allow_tips: Optional[bool] = None


class Payment(BaseModel):
    id: str
    item_id: str
    payment_hash: str
    amount_paid: int
    paid_at: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    views_count: int = 0


class CreateInvoiceData(BaseModel):
    amount: Optional[int] = None
    coupon_code: Optional[str] = Field(default=None, max_length=64)


class CreateTipData(BaseModel):
    amount: int = Field(..., ge=1)
    paywall_payment_hash: Optional[str] = None  # to link the tip to a purchase


class CheckPaymentData(BaseModel):
    payment_hash: str


class PublicItem(BaseModel):
    """The slice of Item that is safe to expose to unauthenticated visitors."""
    id: str
    title: str
    description: str
    content_type: str
    amount: int
    currency: str
    memo: str
    release_delay_seconds: int
    scheduled_at: Optional[str]
    teaser_text: Optional[str] = None
    teaser_blur: bool = True
    access_duration_seconds: int = 0
    max_views: int = 0
    file_count: int = 0
    markdown: bool = False
    allow_tips: bool = True


class Coupon(BaseModel):
    id: str
    item_id: str
    code: str
    discount_percent: int = 0
    discount_fixed_sats: int = 0
    uses_remaining: int = -1  # -1 = unlimited
    uses_count: int = 0
    expires_at: Optional[str] = None
    created_at: Optional[str] = None


class CreateCoupon(BaseModel):
    code: str = Field(..., min_length=2, max_length=64)
    discount_percent: int = Field(default=0, ge=0, le=100)
    discount_fixed_sats: int = Field(default=0, ge=0)
    uses_remaining: int = Field(default=-1, ge=-1)
    expires_at: Optional[str] = None


class Tip(BaseModel):
    id: str
    item_id: str
    paywall_payment_hash: Optional[str] = None
    tip_payment_hash: str
    amount_sats: int
    paid_at: Optional[str] = None
    created_at: Optional[str] = None


class ItemStats(BaseModel):
    """Aggregated payment stats per item, for the admin dashboard."""
    item_id: str
    payment_count: int = 0
    total_sats: int = 0
    unique_payers: int = 0
    last_payment_at: Optional[str] = None

"""
ContentWall models - used for both DB and API.
"""

from __future__ import annotations

from typing import Optional

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


class CreateItem(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    content_type: str = Field(..., pattern="^(article|image)$")
    article_content: Optional[str] = Field(default=None, max_length=50000)
    amount: int = Field(..., ge=1)
    currency: str = Field(default="sat")
    memo: Optional[str] = Field(default=None, max_length=200)
    remembers: bool = Field(default=True)
    release_delay_seconds: int = Field(default=0, ge=0)
    scheduled_at: Optional[str] = Field(default=None)
    onion_hostname: Optional[str] = Field(default=None, max_length=500)


class Payment(BaseModel):
    id: str
    item_id: str
    payment_hash: str
    amount_paid: int
    paid_at: Optional[str] = None
    created_at: Optional[str] = None


class CreateInvoiceData(BaseModel):
    amount: Optional[int] = None


class CheckPaymentData(BaseModel):
    payment_hash: str


class PublicItem(BaseModel):
    id: str
    title: str
    description: str
    content_type: str
    amount: int
    currency: str
    memo: str
    release_delay_seconds: int
    scheduled_at: Optional[str]

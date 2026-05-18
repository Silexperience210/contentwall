"""
ContentWall invoice listener.

v1.2.1: the listener now RECORDS the payment in our own DB the moment LNbits
fires the invoice-paid event, rather than relying on the websocket round-trip.
This was the root cause of "waiting for payment forever even though I paid":
if the websocket missed the notification for ANY reason (proxy timeout, mobile
backgrounding, LNURL flow with no open WS, restart between create+pay), the
payment was lost. Now `has_paid()` returns True the moment LNbits confirms,
regardless of what the frontend is doing.
"""

from __future__ import annotations

import asyncio

from lnbits.core.models import Payment
from lnbits.tasks import register_invoice_listener
from loguru import logger

# In-memory queue used purely as a fast-path notification to any open
# WebSocket. The persistent state lives in the contentwall.payments table.
paid_invoices: dict[str, asyncio.Queue] = {}


def _parse_extra(payment) -> dict:
    """Parse payment.extra which can be dict, JSON string, or None."""
    extra = payment.extra
    if extra is None:
        return {}
    if isinstance(extra, dict):
        return extra
    if isinstance(extra, str):
        import json
        try:
            return json.loads(extra)
        except json.JSONDecodeError:
            return {}
    return {}


async def wait_for_paid_invoices():
    invoice_queue: asyncio.Queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_contentwall")

    while True:
        try:
            payment = await invoice_queue.get()
            await on_invoice_paid(payment)
        except Exception as exc:
            logger.error(f"contentwall: error processing invoice: {exc}")


async def on_invoice_paid(payment: Payment) -> None:
    """
    Handle an invoice that LNbits has just marked as settled.

    For ContentWall-tagged invoices we:
      1. Record the payment in `contentwall.payments` (idempotent).
      2. Fire the item's outbound webhook if configured.
      3. Notify any open WebSocket for instant UI update.

    Steps 1+2 happen BEFORE step 3 so that if the WebSocket fires the client
    back with `paid:true`, the very next /check call finds the row.
    """
    try:
        extra = _parse_extra(payment)
        if extra.get("tag") != "contentwall":
            return

        item_id = extra.get("id")
        payment_hash = payment.payment_hash
        if not item_id or not payment_hash:
            logger.warning(
                f"contentwall: paid invoice missing item id / hash: "
                f"extra={extra!r} hash={payment_hash!r}"
            )
            return

        logger.info(
            f"contentwall: payment received for item={item_id} hash={payment_hash}"
        )

        # --- 1. Persist in our own table (idempotent) -----------------------
        # Imported inside the function to avoid an import cycle at module load
        # (crud -> models -> ... -> tasks).
        from .crud import (
            get_item,
            has_paid,
            record_payment,
        )

        rec = None
        if not await has_paid(item_id, payment_hash):
            item = await get_item(item_id)
            if not item:
                logger.warning(
                    f"contentwall: paid invoice references unknown item {item_id}"
                )
                return

            amount_sats = int(payment.amount / 1000)
            try:
                rec = await record_payment(
                    item_id,
                    payment_hash,
                    amount_sats,
                    access_duration_seconds=item.access_duration_seconds or 0,
                )
                logger.success(
                    f"contentwall: recorded payment item={item_id} "
                    f"hash={payment_hash} amount={amount_sats} sat"
                )
            except Exception as exc:
                # Most likely a UNIQUE-index race against a concurrent /check
                # call that already inserted the row. That's fine.
                logger.debug(
                    f"contentwall: record_payment race for {payment_hash}: {exc}"
                )

            # --- 2. Webhook delivery ---------------------------------------
            if item.webhook_url and rec is not None:
                from .helpers import fire_and_forget_webhook

                fire_and_forget_webhook(
                    item.webhook_url,
                    {
                        "event": "payment.confirmed",
                        "item_id": item_id,
                        "payment_hash": payment_hash,
                        "amount_sats": amount_sats,
                        "paid_at": rec.paid_at,
                        "expires_at": rec.expires_at,
                        "coupon_code": extra.get("coupon_code"),
                    },
                )

        # --- 3. Notify open WebSocket for instant UI update ----------------
        if payment_hash in paid_invoices:
            try:
                await paid_invoices[payment_hash].put(payment)
                logger.debug(
                    f"contentwall: notified websocket for {payment_hash}"
                )
            except Exception as exc:
                logger.warning(
                    f"contentwall: failed to notify websocket: {exc}"
                )

    except Exception as exc:
        logger.error(f"contentwall: on_invoice_paid crashed: {exc}")

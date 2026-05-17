from __future__ import annotations

import asyncio

from lnbits.core.models import Payment
from lnbits.tasks import register_invoice_listener
from loguru import logger

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
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_contentwall")

    while True:
        try:
            payment = await invoice_queue.get()
            await on_invoice_paid(payment)
        except Exception as exc:
            logger.error(f"Error processing invoice: {exc}")


async def on_invoice_paid(payment: Payment) -> None:
    try:
        extra = _parse_extra(payment)
        if extra.get("tag") != "contentwall":
            return

        payment_hash = payment.payment_hash
        logger.info(f"ContentWall payment received: {payment_hash}")

        if payment_hash in paid_invoices:
            try:
                await paid_invoices[payment_hash].put(payment)
                logger.debug(f"Notified websocket for {payment_hash}")
            except Exception as exc:
                logger.warning(f"Failed to notify websocket: {exc}")
    except Exception as exc:
        logger.error(f"Error in on_invoice_paid: {exc}")

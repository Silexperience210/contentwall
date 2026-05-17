"""
ContentWall - A real paywall extension for LNbits with server-side content storage.
"""

import asyncio

from fastapi import APIRouter
from lnbits.tasks import create_permanent_unique_task
from loguru import logger

from .crud import db
from .tasks import wait_for_paid_invoices
from .views import contentwall_generic_router
from .views_api import contentwall_api_router

contentwall_ext: APIRouter = APIRouter(prefix="/contentwall", tags=["ContentWall"])
contentwall_ext.include_router(contentwall_generic_router)
contentwall_ext.include_router(contentwall_api_router)

contentwall_static_files = [
    {
        "path": "/contentwall/static",
        "name": "contentwall_static",
    }
]

scheduled_tasks: list[asyncio.Task] = []


def contentwall_stop():
    for task in scheduled_tasks:
        try:
            task.cancel()
        except Exception as ex:
            logger.warning(ex)


def contentwall_start():
    task = create_permanent_unique_task("ext_contentwall", wait_for_paid_invoices)
    scheduled_tasks.append(task)


__all__ = [
    "db",
    "contentwall_ext",
    "contentwall_start",
    "contentwall_static_files",
    "contentwall_stop",
]

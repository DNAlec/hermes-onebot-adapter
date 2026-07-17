"""Shared async helpers used across the adapter service.

These small utilities are factored out to avoid duplication between
``relay/hermes_ws.py``, ``onebot/ws_reverse.py``, ``onebot/ws_forward.py``,
and ``config.py``.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def log_task_exception(task: asyncio.Task) -> None:
    """Done-callback: log unhandled exceptions from fire-and-forget tasks.

    Attached to ``asyncio.create_task`` results so that exceptions don't
    surface as "Task exception was never retrieved" warnings.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background task crashed: %r", exc, exc_info=exc)


def bearer_token(header: str) -> str:
    """Extract a bearer token from an ``Authorization`` header value.

    Returns ``""`` when the header is absent or doesn't use the Bearer scheme.
    """
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""

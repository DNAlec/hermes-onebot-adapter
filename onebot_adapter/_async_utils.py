"""Shared async helpers used across the adapter service.

These small utilities are factored out to avoid duplication between
``relay/hermes_ws.py``, ``onebot/ws_reverse.py``, ``onebot/ws_forward.py``,
and ``config.py``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Sequence

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


def ws_presented_token(request, *, query_keys: Sequence[str] = ("token",)) -> str:
    """Return the WS token: Authorization Bearer if present, else the first query key."""
    header = bearer_token(request.headers.get("Authorization", ""))
    if header:
        return header
    for key in query_keys:
        value = request.query.get(key)
        if value:
            return value
    return ""


def token_matches(presented: str, expected: str) -> bool:
    """Constant-time compare. Empty presented or expected never matches."""
    if not presented or not expected:
        return False
    left = hashlib.sha256(presented.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)

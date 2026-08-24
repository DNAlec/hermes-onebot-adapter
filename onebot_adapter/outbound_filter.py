"""Filter Hermes-originated outbound messages against configured regexes.

Used by the relay send / ``send_msg`` path. Adapter-originated messages
(``send_direct_message`` / reject replies) and the HTTP automation API are
intentionally not filtered.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_OUTBOUND_FILTER_PATTERNS = 50
MAX_OUTBOUND_FILTER_PATTERN_LENGTH = 256

SEND_ACTIONS_WITH_TEXT = frozenset({
    "send_text",
    "send_image",
    "send_voice",
    "send_video",
    "send_document",
})
API_SEND_ACTIONS = frozenset({"send_msg", "send_group_msg", "send_private_msg"})


def validate_outbound_filter_patterns(patterns: Any, label: str, errors: list[str]) -> None:
    """Append validation errors for a list of regex pattern strings."""
    if not isinstance(patterns, list):
        errors.append(f"{label} must be a list of regex strings")
        return
    if len(patterns) > MAX_OUTBOUND_FILTER_PATTERNS:
        errors.append(f"{label} must contain at most {MAX_OUTBOUND_FILTER_PATTERNS} patterns")
    for i, item in enumerate(patterns):
        if not isinstance(item, str) or not item:
            errors.append(f"{label}[{i}] must be a non-empty string")
            continue
        if len(item) > MAX_OUTBOUND_FILTER_PATTERN_LENGTH:
            errors.append(
                f"{label}[{i}] must be at most {MAX_OUTBOUND_FILTER_PATTERN_LENGTH} characters"
            )
            continue
        try:
            re.compile(item)
        except re.error as exc:
            errors.append(f"{label}[{i}] is not a valid regex: {exc}")


def extract_send_frame_text(action: str, data: dict[str, Any]) -> str:
    """Visible text Hermes is trying to send via a ``send`` frame."""
    if action == "send_text":
        return str(data.get("content") or "")
    if action in {"send_image", "send_voice", "send_video", "send_document"}:
        return str(data.get("caption") or "")
    return ""


def extract_api_message_text(params: dict[str, Any]) -> str:
    """Visible text from a OneBot ``send_msg`` / ``send_*_msg`` params payload."""
    message = params.get("message")
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for seg in message:
        if not isinstance(seg, dict) or seg.get("type") != "text":
            continue
        data = seg.get("data") or {}
        if isinstance(data, dict):
            parts.append(str(data.get("text") or ""))
    return "".join(parts)


def matching_pattern(text: str, patterns: list[str]) -> str | None:
    """Return the first pattern that ``re.search``-matches *text*, else ``None``.

    Empty text is never filtered, so media-only sends (no caption) pass through
    even when a pattern such as ``.*`` is configured. Invalid patterns are
    skipped with a warning (validate() should have rejected them already).
    """
    if not text:
        return None
    for pattern in patterns:
        try:
            compiled = re.compile(pattern)
        except re.error:
            logger.warning("skipping invalid outbound filter pattern %r", pattern)
            continue
        if compiled.search(text):
            return pattern
    return None

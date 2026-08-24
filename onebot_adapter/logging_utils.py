"""Small helpers for bounded, privacy-aware diagnostic logging."""
from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SECRET_FRAGMENTS = ("token", "authorization", "password", "secret", "cookie")
_TEXT_KEYS = {"text", "content", "caption", "reply_to_text"}
_PATH_KEYS = {"file", "file_path", "audio_path", "video_path", "image_path"}


def _summary(value: Any, label: str) -> str:
    try:
        length = len(value)
    except (TypeError, AttributeError):
        length = len(str(value))
    return f"<{label} len={length}>"


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>" if parsed.query else "", ""))


def text_summary(value: Any, *, preview: int = 0) -> str:
    """Return a bounded diagnostic view of a message body.

    By default only the length is shown (``<text len=N>``).  When *preview*
    is positive, a truncated excerpt is included for operators who already
    opted into DEBUG.
    """
    text = "" if value is None else str(value)
    if preview > 0:
        shown = text if len(text) <= preview else text[:preview] + "..."
        return f"<text len={len(text)} preview={shown!r}>"
    return _summary(text, "text")


def sanitize_for_log(value: Any, *, key: str = "") -> Any:
    """Return a JSON-serialisable diagnostic view without secrets or message bodies."""
    lowered = key.lower()
    if any(fragment in lowered for fragment in _SECRET_FRAGMENTS):
        return "<redacted>"
    if lowered in _TEXT_KEYS:
        return text_summary(value)
    if isinstance(value, dict):
        return {str(k): sanitize_for_log(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, str):
        if lowered in _PATH_KEYS and "://" not in value:
            return f"<path name={PurePath(value).name!r}>"
        return _sanitize_url(value)
    return value


def safe_json(value: Any, limit: int = 2000) -> str:
    """Render a bounded JSON diagnostic string after applying sanitisation."""
    try:
        rendered = json.dumps(sanitize_for_log(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = "<unserializable>"
    return rendered[:limit]

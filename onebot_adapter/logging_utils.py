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


def sanitize_for_log(value: Any, *, key: str = "") -> Any:
    """Return a JSON-serialisable diagnostic view without secrets or message bodies."""
    lowered = key.lower()
    if any(fragment in lowered for fragment in _SECRET_FRAGMENTS):
        return "<redacted>"
    if lowered in _TEXT_KEYS:
        return _summary(value, "text")
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

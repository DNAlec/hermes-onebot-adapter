"""Media helpers for the outbound (plugin → adapter) send path.

Inbound media is no longer downloaded by the adapter — all media (images,
videos, voice, files) are rendered as URL placeholders in the event text so
the LLM can fetch them on demand. This module retains only the helpers
needed for the outbound send path (writing plugin-uploaded bytes to temp
files for OneBot API calls).
"""
from __future__ import annotations

import logging
import os
import tempfile

logger = logging.getLogger(__name__)


def _file_ext(url: str) -> str:
    path = url.split("?")[0]
    dot = path.rfind(".")
    return path[dot:].lower() if dot != -1 else ""


def _ext_for_mime(mime: str) -> str:
    if "image" in mime:
        return ".jpg"
    if "audio" in mime:
        return ".wav"
    if "video" in mime:
        return ".mp4"
    return ".bin"


async def write_temp_media(data: bytes, name: str, mime: str) -> str:
    """Write *data* to a temp file and return a ``file://`` URI for OneBot."""
    ext = _file_ext(name) or _ext_for_mime(mime)
    fd, path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    with open(path, "wb") as f:
        f.write(data)
    return f"file://{path}"


def cleanup_temp_uri(uri: str) -> None:
    """Remove a temp file referenced by a ``file://`` URI."""
    if uri.startswith("file://"):
        try:
            os.unlink(uri[7:])
        except OSError:
            pass

"""Media download, classification, and voice conversion.

All downloads reuse a single shared ``aiohttp.ClientSession`` for efficiency.
Voice (silk/amr/ogg) is converted to 16 kHz mono WAV via an **async** ffmpeg
subprocess — never blocking the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid

import aiohttp

from onebot_adapter.relay.protocol import MediaDescriptor, MediaPayload

logger = logging.getLogger(__name__)

_AUDIO_EXTS = {".mp3", ".opus", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".silk", ".amr"}
_VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".flv", ".wmv"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".ico", ".svg"}

_MIME_BY_CLASS = {
    "image": "image/jpeg",
    "audio": "audio/wav",
    "video": "video/mp4",
    "file": "application/octet-stream",
}


def _file_ext(url: str) -> str:
    path = url.split("?")[0]
    dot = path.rfind(".")
    return path[dot:].lower() if dot != -1 else ""


def classify_media(url: str) -> str:
    ext = _file_ext(url)
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    return "file"


def _new_media_id() -> str:
    return f"m{uuid.uuid4().hex[:10]}"


async def download_bytes(
    session: aiohttp.ClientSession, url: str, max_bytes: int,
) -> tuple[bytes | None, str | None]:
    """Download a URL into memory, returning ``(data, reason)``.

    On success *reason* is ``None``. On failure *data* is ``None`` and
    *reason* is a human-readable string explaining why (size limit, network
    error, etc.) so callers can surface the cause to the LLM.
    """
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            # Pre-check Content-Length to avoid downloading huge files
            cl = resp.headers.get("Content-Length")
            if cl:
                try:
                    if int(cl) > max_bytes:
                        cl_mb = int(cl) // 1024 // 1024
                        max_mb = max_bytes // 1024 // 1024
                        logger.debug(
                            "media download skipped (Content-Length %s > %d): %s",
                            cl, max_bytes, url[:80],
                        )
                        return None, f"文件大小{cl_mb}MB超过限制{max_mb}MB"
                except ValueError:
                    pass
            data = await resp.read()
        if len(data) > max_bytes:
            data_mb = len(data) // 1024 // 1024
            max_mb = max_bytes // 1024 // 1024
            logger.debug("media download skipped (too large %d > %d): %s", len(data), max_bytes, url[:80])
            return None, f"文件大小{data_mb}MB超过限制{max_mb}MB"
        return data, None
    except Exception as exc:
        logger.warning("media download failed: %s — %s", url[:80], exc)
        return None, f"下载失败:{str(exc)[:80]}"


async def make_media_payload(
    session: aiohttp.ClientSession,
    url: str,
    *,
    media_class: str | None = None,
    name: str = "",
    max_bytes: int,
    convert_voice: bool = True,
) -> tuple[MediaPayload | None, str | None]:
    """Download *url* and wrap into a :class:`MediaPayload`.

    Returns ``(media_payload, reason)``. On success *reason* is ``None``.
    On failure *media_payload* is ``None`` and *reason* explains the cause.

    When *media_class* is ``"audio"`` and *convert_voice* is true, the bytes
    are transcoded to WAV via ffmpeg before being placed in the payload.
    If conversion fails the raw bytes are still returned (reason carries a
    note about the conversion failure).
    """
    cls = media_class or classify_media(url)
    raw, reason = await download_bytes(session, url, max_bytes)
    if raw is None:
        return None, reason

    mime = _MIME_BY_CLASS.get(cls, "application/octet-stream")
    convert_note: str | None = None

    if cls == "audio" and convert_voice:
        wav = await convert_voice_to_wav(raw)
        if wav is not None:
            raw = wav
            mime = "audio/wav"
        else:
            logger.debug("voice conversion failed; passing through raw bytes")
            # 转换失败:回退 mime 为通用二进制,避免下游按 audio/wav 解码失败
            mime = "application/octet-stream"
            convert_note = "语音转换失败,保留原始格式"

    desc = MediaDescriptor(id=_new_media_id(), mime=mime, name=name, size=len(raw))
    return MediaPayload(descriptor=desc, data=raw), convert_note


async def convert_voice_to_wav(data: bytes) -> bytes | None:
    """Convert voice bytes (silk/amr/ogg/…) to 16 kHz mono WAV via async ffmpeg."""
    in_fd, in_path = tempfile.mkstemp(suffix=".bin")
    out_fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(in_fd)
    os.close(out_fd)
    try:
        with open(in_path, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", in_path, "-ar", "16000", "-ac", "1", "-f", "wav", out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.debug("voice conversion timed out")
            return None
        if proc.returncode != 0:
            logger.debug("ffmpeg failed: %s", stderr[:200] if stderr else "?")
            return None
        with open(out_path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("ffmpeg not found; voice conversion unavailable")
        return None
    except Exception as exc:
        logger.debug("voice conversion error: %s", exc)
        return None
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


async def write_temp_media(data: bytes, name: str, mime: str) -> str:
    """Write *data* to a temp file and return a ``file://`` URI for OneBot."""
    ext = _file_ext(name) or _ext_for_mime(mime)
    fd, path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    with open(path, "wb") as f:
        f.write(data)
    return f"file://{path}"


def _ext_for_mime(mime: str) -> str:
    if "image" in mime:
        return ".jpg"
    if "audio" in mime:
        return ".wav"
    if "video" in mime:
        return ".mp4"
    return ".bin"


def cleanup_temp_uri(uri: str) -> None:
    """Remove a temp file referenced by a ``file://`` URI."""
    if uri.startswith("file://"):
        try:
            os.unlink(uri[7:])
        except OSError:
            pass

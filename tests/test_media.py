"""Tests for media download, classification, voice conversion, and temp files."""
from __future__ import annotations

import os
import struct
import wave
from io import BytesIO

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.onebot.media import (
    classify_media,
    cleanup_temp_uri,
    convert_voice_to_wav,
    download_bytes,
    make_media_payload,
    write_temp_media,
)

# ── classify_media ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://a/img.jpg", "image"),
        ("http://a/img.PNG", "image"),
        ("http://a/photo.gif", "image"),
        ("http://a/song.mp3", "audio"),
        ("http://a/voice.silk", "audio"),
        ("http://a/clip.mp4", "video"),
        ("http://a/clip.AVI", "video"),
        ("http://a/doc.pdf", "file"),
        ("http://a/noext", "file"),
        ("http://a/page.html", "file"),
    ],
)
def test_classify_media(url, expected):
    assert classify_media(url) == expected


# ── download_bytes ───────────────────────────────────────────────────────


async def test_download_bytes_success():
    app = web.Application()

    async def handler(_):
        return web.Response(body=b"hello world")

    app.router.add_get("/data", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            result, reason = await download_bytes(client.session, f"http://127.0.0.1:{server.port}/data", 1024)
        assert result == b"hello world"
        assert reason is None
    finally:
        await server.close()


async def test_download_bytes_too_large_content_length():
    app = web.Application()

    async def handler(_):
        return web.Response(body=b"x" * 100, headers={"Content-Length": "100"})

    app.router.add_get("/big", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            result, reason = await download_bytes(client.session, f"http://127.0.0.1:{server.port}/big", 50)
        assert result is None
        assert reason is not None
        assert "超过限制" in reason
    finally:
        await server.close()


async def test_download_bytes_too_large_post_check():
    """When Content-Length is missing, fall back to post-download size check."""
    app = web.Application()

    async def handler(_):
        # Chunked response — no Content-Length header
        resp = web.StreamResponse()
        await resp.prepare(_)
        await resp.write(b"x" * 200)
        await resp.write_eof()
        return resp

    app.router.add_get("/chunked", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            result, reason = await download_bytes(client.session, f"http://127.0.0.1:{server.port}/chunked", 100)
        assert result is None
        assert reason is not None
        assert "超过限制" in reason
    finally:
        await server.close()


async def test_download_bytes_404_returns_none():
    app = web.Application()

    async def handler(_):
        return web.Response(status=404, text="not found")

    app.router.add_get("/missing", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            result, reason = await download_bytes(client.session, f"http://127.0.0.1:{server.port}/missing", 1024)
        assert result is None
        assert reason is not None
        assert "下载失败" in reason
    finally:
        await server.close()


# ── make_media_payload ───────────────────────────────────────────────────


async def test_make_media_payload_image():
    img = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    app = web.Application()

    async def handler(_):
        return web.Response(body=img, headers={"Content-Type": "image/png"})

    app.router.add_get("/img.png", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            mp, reason = await make_media_payload(
                client.session, f"http://127.0.0.1:{server.port}/img.png",
                media_class="image", max_bytes=1024 * 1024,
            )
        assert mp is not None
        assert reason is None
        assert mp.data == img
        assert mp.descriptor.mime.startswith("image")
        assert mp.descriptor.size == len(img)
    finally:
        await server.close()


async def test_make_media_payload_too_large():
    app = web.Application()

    async def handler(_):
        return web.Response(body=b"x" * 500, headers={"Content-Length": "500"})

    app.router.add_get("/big", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            mp, reason = await make_media_payload(
                client.session, f"http://127.0.0.1:{server.port}/big",
                media_class="image", max_bytes=100,
            )
        assert mp is None
        assert reason is not None
        assert "超过限制" in reason
    finally:
        await server.close()


# ── convert_voice_to_wav ─────────────────────────────────────────────────


def _make_wav_bytes(duration_s: float = 0.1, freq: int = 440) -> bytes:
    """Generate a minimal WAV file for testing ffmpeg conversion."""
    sample_rate = 16000
    n_samples = int(sample_rate * duration_s)
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(n_samples):
            val = int(32767 * 0.5 * __import__("math").sin(2 * __import__("math").pi * freq * i / sample_rate))
            wf.writeframes(struct.pack("<h", val))
    return buf.getvalue()


async def test_convert_voice_to_wav_wav_input():
    """ffmpeg should accept WAV input and output valid WAV."""
    wav_in = _make_wav_bytes()
    result = await convert_voice_to_wav(wav_in)
    assert result is not None
    # Verify output is a valid WAV
    buf = BytesIO(result)
    with wave.open(buf, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2


async def test_convert_voice_to_wav_empty_input():
    result = await convert_voice_to_wav(b"")
    # ffmpeg should fail on empty input
    assert result is None


async def test_make_media_payload_audio_with_conversion():
    """Audio media payload should convert via ffmpeg to WAV."""
    wav_in = _make_wav_bytes()
    app = web.Application()

    async def handler(_):
        return web.Response(body=wav_in, content_type="audio/wav")

    app.router.add_get("/voice.wav", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            mp, reason = await make_media_payload(
                client.session, f"http://127.0.0.1:{server.port}/voice.wav",
                media_class="audio", max_bytes=1024 * 1024, convert_voice=True,
            )
        assert mp is not None
        assert mp.descriptor.mime == "audio/wav"
        # Verify converted output is valid WAV
        buf = BytesIO(mp.data)
        with wave.open(buf, "rb") as wf:
            assert wf.getframerate() == 16000
    finally:
        await server.close()


async def test_make_media_payload_audio_no_conversion():
    """When convert_voice=False, raw bytes pass through."""
    raw = b"\x00" * 100
    app = web.Application()

    async def handler(_):
        return web.Response(body=raw)

    app.router.add_get("/raw.silk", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            mp, reason = await make_media_payload(
                client.session, f"http://127.0.0.1:{server.port}/raw.silk",
                media_class="audio", max_bytes=1024 * 1024, convert_voice=False,
            )
        assert mp is not None
        assert reason is None
        assert mp.data == raw
    finally:
        await server.close()


async def test_make_media_payload_audio_conversion_failure_falls_back_mime():
    """When convert_voice=True but ffmpeg fails, mime must NOT be audio/wav
    (the raw bytes are not WAV); it falls back to application/octet-stream
    and a conversion note is returned."""
    raw = b"\x02\x01\x03not-a-real-silk-frame" * 4
    app = web.Application()

    async def handler(_):
        return web.Response(body=raw)

    app.router.add_get("/bad.silk", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            mp, reason = await make_media_payload(
                client.session, f"http://127.0.0.1:{server.port}/bad.silk",
                media_class="audio", max_bytes=1024 * 1024, convert_voice=True,
            )
        assert mp is not None
        assert mp.descriptor.mime == "application/octet-stream"
        assert reason is not None
        assert "语音转换失败" in reason
        assert mp.data == raw
    finally:
        await server.close()


# ── write_temp_media / cleanup_temp_uri ──────────────────────────────────


async def test_write_temp_media_creates_file():
    data = b"file content here"
    uri = await write_temp_media(data, "test.txt", "text/plain")
    assert uri.startswith("file://")
    path = uri[7:]
    assert os.path.exists(path)
    with open(path, "rb") as f:
        assert f.read() == data


async def test_cleanup_temp_uri_removes_file():
    data = b"temp data"
    uri = await write_temp_media(data, "temp.bin", "application/octet-stream")
    path = uri[7:]
    assert os.path.exists(path)
    cleanup_temp_uri(uri)
    assert not os.path.exists(path)


async def test_cleanup_temp_uri_nonexistent_no_error():
    cleanup_temp_uri("file:///nonexistent/path/file.bin")


async def test_cleanup_temp_uri_non_file_uri_no_error():
    cleanup_temp_uri("http://example.com/file.bin")


async def test_write_temp_media_extension_from_name():
    uri = await write_temp_media(b"x", "photo.jpg", "image/jpeg")
    assert uri.endswith(".jpg")
    cleanup_temp_uri(uri)


async def test_write_temp_media_extension_from_mime():
    uri = await write_temp_media(b"x", "noext", "video/mp4")
    assert uri.endswith(".mp4")
    cleanup_temp_uri(uri)

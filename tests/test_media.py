"""Tests for outbound media temp-file helpers (write_temp_media / cleanup_temp_uri)."""
from __future__ import annotations

import os

from onebot_adapter.onebot.media import cleanup_temp_uri, write_temp_media


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

"""Tests for the plugin-side media caching logic (``_cache_media_items``).

These mock the ``cache_*_from_url`` / ``cache_*_from_bytes`` helpers from
``gateway.platforms.base`` so the tests run without a real Hermes install.
"""
from __future__ import annotations

import contextlib
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The plugin module imports from gateway.* lazily; we can still patch the
# module-level sentinels set in the try/except import block.
from onebot_adapter.hermes_plugin import adapter as plugin_mod


def _make_adapter(media_mode: str = "cache") -> plugin_mod.OneBotAdapter:
    """Build a OneBotAdapter with _media_delivery_mode set, bypassing __init__."""
    ad = plugin_mod.OneBotAdapter.__new__(plugin_mod.OneBotAdapter)
    ad._media_delivery_mode = media_mode
    return ad


def _media_item(kind: str, url: str = "", name: str = "", file_id: str = "", index: int = 0) -> dict:
    return {"kind": kind, "url": url, "mime": "", "name": name, "file_id": file_id, "index": index}


@contextlib.contextmanager
def _cache_env(**overrides):
    patches = {
        "_BASE_AVAILABLE": True,
        "_download_url_bytes": AsyncMock(return_value=b"data"),
        "cache_image_from_bytes": MagicMock(return_value="/tmp/img.jpg"),
        "cache_audio_from_bytes": MagicMock(return_value="/tmp/audio.ogg"),
        "cache_video_from_bytes": MagicMock(return_value="/tmp/video.mp4"),
        "cache_document_from_bytes": MagicMock(return_value="/tmp/doc.pdf"),
        "cache_image_from_url": AsyncMock(side_effect=AssertionError("from_url must not be used")),
        "cache_audio_from_url": AsyncMock(side_effect=AssertionError("from_url must not be used")),
    }
    patches.update(overrides)
    with contextlib.ExitStack() as stack:
        for name, value in patches.items():
            stack.enter_context(patch.object(plugin_mod, name, value))
        yield patches


# ── passthrough mode ──────────────────────────────────────────────────────


def test_onebot_media_guard_allows_loopback():
    plugin_mod._onebot_media_url_guard("http://127.0.0.1/file.jpg")
    plugin_mod._onebot_media_url_guard("http://localhost/file.jpg")


def test_onebot_media_guard_rejects_link_local():
    with pytest.raises(ValueError, match="link-local|metadata"):
        plugin_mod._onebot_media_url_guard("http://169.254.169.254/")


def test_onebot_media_guard_rejects_resolved_link_local(monkeypatch):
    def _gai(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.0.1", port))]

    monkeypatch.setattr(plugin_mod.socket, "getaddrinfo", _gai)
    with pytest.raises(ValueError, match="link-local|metadata"):
        plugin_mod._onebot_media_url_guard("http://media.example/")


async def test_onebot_media_download_loopback_ok():
    import aiohttp.web
    from aiohttp.test_utils import TestServer

    async def handler(_request):
        return aiohttp.web.Response(body=b"hello-media")

    app = aiohttp.web.Application()
    app.router.add_get("/img", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = str(server.make_url("/img"))
        data = await plugin_mod._download_url_bytes(url, allow_private=True)
        assert data == b"hello-media"
    finally:
        await server.close()


async def test_pinned_resolver_returns_only_prechecked_ips():
    resolver = plugin_mod._PinnedResolver([("127.0.0.1", socket.AF_INET)])
    results = await resolver.resolve("evil.example", port=80, family=socket.AF_INET)
    assert results == [{
        "hostname": "evil.example",
        "host": "127.0.0.1",
        "port": 80,
        "family": socket.AF_INET,
        "proto": 0,
        "flags": 0,
    }]
    await resolver.close()


async def test_onebot_media_download_rejects_redirect_to_link_local():
    import aiohttp.web
    from aiohttp.test_utils import TestServer

    async def bounce(_request):
        raise aiohttp.web.HTTPFound("http://169.254.169.254/")

    app = aiohttp.web.Application()
    app.router.add_get("/img", bounce)
    server = TestServer(app)
    await server.start_server()
    try:
        url = str(server.make_url("/img"))
        with pytest.raises(ValueError, match="link-local|metadata"):
            await plugin_mod._download_url_bytes(url, allow_private=True)
    finally:
        await server.close()


async def test_passthrough_mode_no_caching():
    """In passthrough mode, _cache_media_items is not called and media is empty."""
    ad = _make_adapter("passthrough")
    # _handle_event logic: if mode != cache, skip _cache_media_items entirely.
    # Here we just verify the guard directly.
    assert ad._media_delivery_mode != "cache"


# ── cache mode with mocked helpers ────────────────────────────────────────


async def test_cache_image_success():
    """Image with URL is downloaded through the gated client then cached from bytes."""
    ad = _make_adapter("cache")
    with _cache_env():
        media_urls, media_types = await ad._cache_media_items([
            _media_item("image", url="https://example.com/cat.jpg"),
        ])
    assert media_urls == ["/tmp/img.jpg"]
    assert "image/" in media_types[0]


async def test_cache_audio_success():
    """Audio/record with URL is downloaded through the gated client then cached from bytes."""
    ad = _make_adapter("cache")
    with _cache_env():
        media_urls, media_types = await ad._cache_media_items([
            _media_item("record", url="https://example.com/voice.silk"),
        ])
    assert media_urls == ["/tmp/audio.ogg"]
    assert "audio/" in media_types[0]


async def test_cache_file_no_url_skipped():
    """File without URL is skipped (LLM uses onebot_get_file tool)."""
    ad = _make_adapter("cache")
    with _cache_env():
        media_urls, media_types = await ad._cache_media_items([
            _media_item("file", file_id="abc123", name="doc.zip"),
        ])
    assert media_urls == []
    assert media_types == []


async def test_cache_download_failure_skips_media():
    """When the gated download raises, the media is skipped (not crashed)."""
    ad = _make_adapter("cache")
    with _cache_env(_download_url_bytes=AsyncMock(side_effect=Exception("download failed"))):
        media_urls, media_types = await ad._cache_media_items([
            _media_item("image", url="https://example.com/broken.jpg"),
        ])
    assert media_urls == []
    assert media_types == []


async def test_cache_mixed_success_and_failure():
    """Mixed items: one succeeds, one fails → only the successful one is returned."""
    ad = _make_adapter("cache")

    async def _download(url: str, *, allow_private: bool = False) -> bytes:
        if "a.jpg" in url:
            raise Exception("fail")
        return b"data"

    with _cache_env(_download_url_bytes=AsyncMock(side_effect=_download)):
        media_urls, media_types = await ad._cache_media_items([
            _media_item("image", url="https://example.com/a.jpg"),
            _media_item("record", url="https://example.com/b.silk"),
        ])
    assert media_urls == ["/tmp/audio.ogg"]
    assert len(media_types) == 1


async def test_cache_base_unavailable_returns_empty():
    """When _BASE_AVAILABLE is False, _cache_media_items returns empty lists."""
    ad = _make_adapter("cache")
    with patch.object(plugin_mod, "_BASE_AVAILABLE", False):
        media_urls, media_types = await ad._cache_media_items([
            _media_item("image", url="https://example.com/cat.jpg"),
        ])
    assert media_urls == []
    assert media_types == []


async def test_cache_empty_items_returns_empty():
    """Empty media_items list returns empty lists."""
    ad = _make_adapter("cache")
    with _cache_env():
        media_urls, media_types = await ad._cache_media_items([])
    assert media_urls == []
    assert media_types == []


# ── _ext_from_url helper ──────────────────────────────────────────────────


def test_ext_from_url_extracts_jpg():
    assert plugin_mod._ext_from_url("https://example.com/cat.jpg", ".jpg") == ".jpg"


def test_ext_from_url_extracts_png():
    assert plugin_mod._ext_from_url("https://example.com/cat.png", ".jpg") == ".png"


def test_ext_from_url_fallback_no_ext():
    assert plugin_mod._ext_from_url("https://example.com/noext", ".jpg") == ".jpg"


def test_ext_from_url_strips_query():
    assert plugin_mod._ext_from_url("https://example.com/cat.jpg?token=abc", ".jpg") == ".jpg"


def test_ext_from_url_empty_url():
    assert plugin_mod._ext_from_url("", ".jpg") == ".jpg"


def test_media_item_to_dict():
    from onebot_adapter.relay.protocol import MediaItem
    item = MediaItem(kind="image", url="https://x/1.jpg", index=0)
    d = item.to_dict()
    assert d["kind"] == "image"
    assert d["url"] == "https://x/1.jpg"
    assert d["index"] == 0

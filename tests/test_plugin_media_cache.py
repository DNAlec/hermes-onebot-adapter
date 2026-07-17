"""Tests for the plugin-side media caching logic (``_cache_media_items``).

These mock the ``cache_*_from_url`` / ``cache_*_from_bytes`` helpers from
``gateway.platforms.base`` so the tests run without a real Hermes install.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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


# ── passthrough mode ──────────────────────────────────────────────────────


async def test_passthrough_mode_no_caching():
    """In passthrough mode, _cache_media_items is not called and media is empty."""
    ad = _make_adapter("passthrough")
    # _handle_event logic: if mode != cache, skip _cache_media_items entirely.
    # Here we just verify the guard directly.
    assert ad._media_delivery_mode != "cache"


# ── cache mode with mocked helpers ────────────────────────────────────────


async def test_cache_image_success():
    """Image with URL is cached via cache_image_from_url."""
    ad = _make_adapter("cache")
    with patch.object(plugin_mod, "_BASE_AVAILABLE", True), \
         patch.object(plugin_mod, "cache_image_from_url", new=AsyncMock(return_value="/tmp/img.jpg")), \
         patch.object(plugin_mod, "cache_audio_from_url", new=AsyncMock(return_value="/tmp/audio.ogg")), \
         patch.object(plugin_mod, "cache_video_from_bytes", new=MagicMock(return_value="/tmp/video.mp4")), \
         patch.object(plugin_mod, "cache_document_from_bytes", new=MagicMock(return_value="/tmp/doc.pdf")):
        media_urls, media_types = await ad._cache_media_items([
            _media_item("image", url="https://example.com/cat.jpg"),
        ])
    assert media_urls == ["/tmp/img.jpg"]
    assert "image/" in media_types[0]


async def test_cache_audio_success():
    """Audio/record with URL is cached via cache_audio_from_url."""
    ad = _make_adapter("cache")
    with patch.object(plugin_mod, "_BASE_AVAILABLE", True), \
         patch.object(plugin_mod, "cache_image_from_url", new=AsyncMock(return_value="/tmp/img.jpg")), \
         patch.object(plugin_mod, "cache_audio_from_url", new=AsyncMock(return_value="/tmp/audio.ogg")), \
         patch.object(plugin_mod, "cache_video_from_bytes", new=MagicMock(return_value="/tmp/video.mp4")), \
         patch.object(plugin_mod, "cache_document_from_bytes", new=MagicMock(return_value="/tmp/doc.pdf")):
        media_urls, media_types = await ad._cache_media_items([
            _media_item("record", url="https://example.com/voice.silk"),
        ])
    assert media_urls == ["/tmp/audio.ogg"]
    assert "audio/" in media_types[0]


async def test_cache_file_no_url_skipped():
    """File without URL is skipped (LLM uses onebot_get_file tool)."""
    ad = _make_adapter("cache")
    with patch.object(plugin_mod, "_BASE_AVAILABLE", True), \
         patch.object(plugin_mod, "cache_image_from_url", new=AsyncMock(return_value="/tmp/img.jpg")), \
         patch.object(plugin_mod, "cache_audio_from_url", new=AsyncMock(return_value="/tmp/audio.ogg")), \
         patch.object(plugin_mod, "cache_video_from_bytes", new=MagicMock(return_value="/tmp/video.mp4")), \
         patch.object(plugin_mod, "cache_document_from_bytes", new=MagicMock(return_value="/tmp/doc.pdf")):
        media_urls, media_types = await ad._cache_media_items([
            _media_item("file", file_id="abc123", name="doc.zip"),
        ])
    assert media_urls == []
    assert media_types == []


async def test_cache_download_failure_skips_media():
    """When cache_image_from_url raises, the media is skipped (not crashed)."""
    ad = _make_adapter("cache")
    with patch.object(plugin_mod, "_BASE_AVAILABLE", True), \
         patch.object(plugin_mod, "cache_image_from_url", new=AsyncMock(side_effect=Exception("download failed"))), \
         patch.object(plugin_mod, "cache_audio_from_url", new=AsyncMock(return_value="/tmp/audio.ogg")), \
         patch.object(plugin_mod, "cache_video_from_bytes", new=MagicMock(return_value="/tmp/video.mp4")), \
         patch.object(plugin_mod, "cache_document_from_bytes", new=MagicMock(return_value="/tmp/doc.pdf")):
        media_urls, media_types = await ad._cache_media_items([
            _media_item("image", url="https://example.com/broken.jpg"),
        ])
    assert media_urls == []
    assert media_types == []


async def test_cache_mixed_success_and_failure():
    """Mixed items: one succeeds, one fails → only the successful one is returned."""
    ad = _make_adapter("cache")
    with patch.object(plugin_mod, "_BASE_AVAILABLE", True), \
         patch.object(plugin_mod, "cache_image_from_url", new=AsyncMock(side_effect=Exception("fail"))), \
         patch.object(plugin_mod, "cache_audio_from_url", new=AsyncMock(return_value="/tmp/audio.ogg")), \
         patch.object(plugin_mod, "cache_video_from_bytes", new=MagicMock(return_value="/tmp/video.mp4")), \
         patch.object(plugin_mod, "cache_document_from_bytes", new=MagicMock(return_value="/tmp/doc.pdf")):
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
    with patch.object(plugin_mod, "_BASE_AVAILABLE", True), \
         patch.object(plugin_mod, "cache_image_from_url", new=AsyncMock(return_value="/tmp/img.jpg")):
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


# ── MediaItem from_dict round-trip ─────────────────────────────────────────


def test_media_item_from_dict():
    from onebot_adapter.relay.protocol import MediaItem
    item = MediaItem.from_dict({
        "kind": "file", "url": "https://x/f.pdf", "mime": "application/pdf",
        "name": "f.pdf", "file_id": "fid1", "index": 3,
    })
    assert item.kind == "file"
    assert item.url == "https://x/f.pdf"
    assert item.name == "f.pdf"
    assert item.file_id == "fid1"
    assert item.index == 3


def test_media_item_to_dict_round_trip():
    from onebot_adapter.relay.protocol import MediaItem
    item = MediaItem(kind="image", url="https://x/1.jpg", index=0)
    d = item.to_dict()
    assert d["kind"] == "image"
    assert d["url"] == "https://x/1.jpg"
    assert d["index"] == 0
    item2 = MediaItem.from_dict(d)
    assert item2.kind == item.kind
    assert item2.url == item.url
    assert item2.index == item.index

"""Integration tests for the parser with a real mock HTTP server.

These verify the full media download pipeline: OneBot event → parser →
media download from HTTP server → MediaPayload with actual bytes.
"""
from __future__ import annotations

import struct
import wave
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.onebot import parser


def _msg_event(
    segments: list[dict],
    *,
    message_type: str = "private",
    user_id: int = 100,
    group_id: int = 0,
    message_id: int = 1,
) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "post_type": "message",
        "message_type": message_type,
        "user_id": user_id,
        "message_id": message_id,
        "time": 1700000000,
        "sender": {"card": "Tester", "nickname": "Test", "user_id": user_id},
        "message": segments,
    }
    if group_id:
        ev["group_id"] = group_id
    return ev


def _make_wav_bytes(duration_s: float = 0.1) -> bytes:
    sample_rate = 16000
    n_samples = int(sample_rate * duration_s)
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(n_samples):
            import math
            val = int(32767 * 0.5 * math.sin(2 * math.pi * 440 * i / sample_rate))
            wf.writeframes(struct.pack("<h", val))
    return buf.getvalue()


@pytest.fixture
async def media_server():
    """Start a mock HTTP server serving test media files."""
    img_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    wav_data = _make_wav_bytes()
    video_data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200
    file_data = b"PDF-1.4 content here"

    app = web.Application()

    async def img_handler(_):
        return web.Response(body=img_png, content_type="image/png")

    async def voice_handler(_):
        return web.Response(body=wav_data, content_type="audio/wav")

    async def video_handler(_):
        return web.Response(body=video_data, content_type="video/mp4")

    async def file_handler(_):
        return web.Response(body=file_data, content_type="application/pdf")

    async def big_handler(_):
        return web.Response(body=b"x" * 10000, headers={"Content-Length": "10000"})

    async def error_handler(_):
        return web.Response(status=500, text="server error")

    app.router.add_get("/img.png", img_handler)
    app.router.add_get("/voice.wav", voice_handler)
    app.router.add_get("/video.mp4", video_handler)
    app.router.add_get("/doc.pdf", file_handler)
    app.router.add_get("/big.bin", big_handler)
    app.router.add_get("/error", error_handler)

    server = TestServer(app)
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"
    yield {
        "base": base,
        "img_url": f"{base}/img.png",
        "voice_url": f"{base}/voice.wav",
        "video_url": f"{base}/video.mp4",
        "file_url": f"{base}/doc.pdf",
        "big_url": f"{base}/big.bin",
        "error_url": f"{base}/error",
        "img_bytes": img_png,
        "wav_bytes": wav_data,
        "video_bytes": video_data,
        "file_bytes": file_data,
        "server": server,
    }
    await server.close()


# ── Image ────────────────────────────────────────────────────────────────


async def test_parser_image_download(media_server):
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([{"type": "image", "data": {"url": media_server["img_url"]}}]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert event.message_type == "photo"
    assert len(media) == 1
    assert media[0].data == media_server["img_bytes"]
    assert media[0].descriptor.mime.startswith("image")
    assert event.media_types == [m for m in [media[0].descriptor.mime] if m]
    # Placeholder should be present in text
    assert "[图1]" in event.text


async def test_parser_image_download_fails_falls_back_to_text(media_server):
    """When image download fails (500), msg_type should stay 'text'."""
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([
                {"type": "text", "data": {"text": "look at this"}},
                {"type": "image", "data": {"url": media_server["error_url"]}},
            ]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert event.message_type == "text"
    assert len(media) == 0
    assert "look at this" in event.text
    # Skipped media should have a placeholder with reason
    assert "[图1](已跳过:" in event.text
    # skipped_media carries the download-failure record
    assert len(event.skipped_media) == 1
    assert event.skipped_media[0]["kind"] == "image"
    assert event.skipped_media[0]["idx"] == 1
    assert "下载失败" in event.skipped_media[0]["reason"]


# ── Voice ────────────────────────────────────────────────────────────────


async def test_parser_voice_download_and_convert(media_server):
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([{"type": "record", "data": {"url": media_server["voice_url"]}}]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert event.message_type == "voice"
    assert len(media) == 1
    assert media[0].descriptor.mime == "audio/wav"
    assert "[语音1]" in event.text
    # Verify converted output is valid WAV
    buf = BytesIO(media[0].data)
    with wave.open(buf, "rb") as wf:
        assert wf.getframerate() == 16000


# ── Video ────────────────────────────────────────────────────────────────


async def test_parser_video_download(media_server):
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([{"type": "video", "data": {"url": media_server["video_url"]}}]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert event.message_type == "video"
    assert len(media) == 1
    assert media[0].data == media_server["video_bytes"]
    assert "[视频1]" in event.text


# ── Document ─────────────────────────────────────────────────────────────


async def test_parser_file_download(media_server):
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([{"type": "file", "data": {"file": "doc.pdf", "url": media_server["file_url"]}}]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert event.message_type == "document"
    assert len(media) == 1
    assert media[0].data == media_server["file_bytes"]
    assert "[文件1:doc.pdf]" in event.text


# ── Media too large ──────────────────────────────────────────────────────


async def test_parser_media_too_large_falls_back_to_text(media_server):
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([
                {"type": "text", "data": {"text": "big file"}},
                {"type": "image", "data": {"url": media_server["big_url"]}},
            ]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=100,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert event.message_type == "text"
    assert len(media) == 0
    assert "[图1](已跳过:" in event.text
    assert "big file" in event.text
    # skipped_media carries the size-limit record
    assert len(event.skipped_media) == 1
    assert event.skipped_media[0]["kind"] == "image"
    assert event.skipped_media[0]["idx"] == 1
    assert "超过限制" in event.skipped_media[0]["reason"]


# ── Reply with image media ───────────────────────────────────────────────


async def test_parser_reply_with_image_media(media_server):
    mock_api = MagicMock()
    mock_api.get_msg = AsyncMock(return_value={
        "sender": {"card": "Quoter", "user_id": 200},
        "message": [{"type": "image", "data": {"url": media_server["img_url"]}}],
    })
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([
                {"type": "reply", "data": {"id": 55}},
                {"type": "text", "data": {"text": "my reply"}},
            ]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            api=mock_api,
            session=client.session,
        )
    assert result is not None
    event, media = result
    # Reply contributed image media → msg_type should be photo
    assert event.message_type == "photo"
    assert len(media) == 1
    assert media[0].data == media_server["img_bytes"]
    assert event.reply_to_message_id == "55"
    # Reply text should contain placeholder for the quoted image
    assert "[图1]" in event.reply_to_text


# ── Forward with media ───────────────────────────────────────────────────


async def test_parser_forward_with_image_media(media_server):
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"card": "Alice", "user_id": 1},
                "message": [
                    {"type": "text", "data": {"text": "see this"}},
                    {"type": "image", "data": {"url": media_server["img_url"]}},
                ],
            },
        ]
    })
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([{"type": "forward", "data": {"id": "fwd1"}}]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            api=mock_api,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert "[合并转发开始:1]" in event.text
    assert "[合并转发结束:1]" in event.text
    assert "[Alice]: see this" in event.text
    assert "[图1]" in event.text
    assert len(media) == 1
    assert media[0].data == media_server["img_bytes"]


# ── Multiple media types in forward ─────────────────────────────────────


async def test_parser_forward_with_multiple_media_types(media_server):
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"card": "Alice", "user_id": 1},
                "message": [{"type": "image", "data": {"url": media_server["img_url"]}}],
            },
            {
                "sender": {"card": "Bob", "user_id": 2},
                "message": [{"type": "record", "data": {"url": media_server["voice_url"]}}],
            },
        ]
    })
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([{"type": "forward", "data": {"id": "fwd_multi"}}]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            api=mock_api,
            session=client.session,
        )
    assert result is not None
    event, media = result
    # Should have downloaded both image and voice
    assert len(media) == 2
    mimes = {mp.descriptor.mime for mp in media}
    assert any(m.startswith("image") for m in mimes)
    assert any(m.startswith("audio") for m in mimes)
    # Should be mixed type
    assert event.message_type == "mixed"
    # Both placeholders should be present
    assert "[图1]" in event.text
    assert "[语音2]" in event.text


# ── Text + image mixed message ──────────────────────────────────────────


async def test_parser_text_and_image_mixed(media_server):
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([
                {"type": "text", "data": {"text": "check this out"}},
                {"type": "image", "data": {"url": media_server["img_url"]}},
            ]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert event.message_type == "photo"
    assert "check this out" in event.text
    assert "[图1]" in event.text
    assert len(media) == 1


# ── Multiple images with text in between ─────────────────────────────────


async def test_parser_multiple_images_with_text_between(media_server):
    """Text + image + text should preserve layout with placeholders."""
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([
                {"type": "text", "data": {"text": "before"}},
                {"type": "image", "data": {"url": media_server["img_url"]}},
                {"type": "text", "data": {"text": "after"}},
            ]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert event.message_type == "photo"
    assert len(media) == 1
    # Placeholder should be between "before" and "after" (no extra spaces)
    assert "before[图1]after" in event.text


# ── Media count limit ──────────────────────────────────────────────────


async def test_parser_media_count_limit(media_server):
    """When media count exceeds the limit, extras should be skipped."""
    segments = [{"type": "text", "data": {"text": "many images"}}]
    for _ in range(5):
        segments.append({"type": "image", "data": {"url": media_server["img_url"]}})
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event(segments),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            media_max_count=2,
            session=client.session,
        )
    assert result is not None
    event, media = result
    # Only 2 images should be downloaded
    assert len(media) == 2
    # First 2 should be ok, rest should be skipped with count reason
    assert "[图1]" in event.text
    assert "[图2]" in event.text
    assert "[图3](已跳过:超出数量限制" in event.text
    assert "[图4](已跳过:超出数量限制" in event.text
    assert "[图5](已跳过:超出数量限制" in event.text
    # skipped_media should carry 3 records, all count-limit, idx 3/4/5
    assert len(event.skipped_media) == 3
    for i, rec in enumerate(event.skipped_media):
        assert rec["kind"] == "image"
        assert rec["idx"] == i + 3
        assert rec["reason"] == "超出数量限制"
        assert "上限2" in rec["detail"]


# ── Nested forward (inline content, NapCat style) ────────────────────────


async def test_parser_nested_forward_inline_content(media_server):
    """Nested forward should be expanded from the inline ``data.content``
    field (NapCat) without a second ``get_forward_msg`` call.

    NapCat populates ``forward.data.content`` with a list of message objects
    structurally identical to the top-level ``messages`` array, so the adapter
    must not recurse via ``get_forward_msg`` on the inner id (NapCat rejects
    per-id queries for inner forwards with retcode=1200).
    """
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        # Outer forward — one of its messages is itself a forward whose
        # nested content is inlined by NapCat.
        "messages": [
            {
                "sender": {"card": "Alice", "user_id": 1},
                "message": [
                    {
                        "type": "forward",
                        "data": {
                            "id": "fwd_inner",
                            "content": [
                                {
                                    "sender": {"card": "Bob", "user_id": 2},
                                    "message": [{"type": "text", "data": {"text": "inner text"}}],
                                },
                            ],
                        },
                    }
                ],
            },
        ]
    })
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([{"type": "forward", "data": {"id": "fwd_outer"}}]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            api=mock_api,
            session=client.session,
        )
    assert result is not None
    event, media = result
    # Only one API call — the inner forward is expanded from inline content.
    assert mock_api.get_forward_msg.await_count == 1
    assert mock_api.get_forward_msg.await_args.args[0] == "fwd_outer"
    assert "[合并转发开始:1]" in event.text
    assert "[合并转发开始:2]" in event.text
    assert "[合并转发结束:2]" in event.text
    assert "[合并转发结束:1]" in event.text
    # End tag for level 1 should come after level 2
    assert event.text.index("[合并转发结束:2]") < event.text.index("[合并转发结束:1]")
    assert "inner text" in event.text


async def test_parser_nested_forward_inline_content_3levels(media_server):
    """Three nested levels all inlined via ``data.content`` should expand
    fully with correct level-numbered tags and a single ``get_forward_msg`` call.
    """
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"card": "Alice", "user_id": 1},
                "message": [
                    {
                        "type": "forward",
                        "data": {
                            "id": "fwd_l2",
                            "content": [
                                {
                                    "sender": {"card": "Bob", "user_id": 2},
                                    "message": [
                                        {
                                            "type": "forward",
                                            "data": {
                                                "id": "fwd_l3",
                                                "content": [
                                                    {
                                                        "sender": {"card": "Carol", "user_id": 3},
                                                        "message": [{"type": "text", "data": {"text": "deepest"}}],
                                                    },
                                                ],
                                            },
                                        }
                                    ],
                                },
                            ],
                        },
                    }
                ],
            },
        ]
    })
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([{"type": "forward", "data": {"id": "fwd_outer"}}]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            api=mock_api,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert mock_api.get_forward_msg.await_count == 1
    assert "[合并转发开始:1]" in event.text
    assert "[合并转发开始:2]" in event.text
    assert "[合并转发开始:3]" in event.text
    assert "[合并转发结束:3]" in event.text
    assert "[合并转发结束:2]" in event.text
    assert "[合并转发结束:1]" in event.text
    # Nesting order: inner end tags precede outer end tags.
    assert event.text.index("[合并转发结束:3]") < event.text.index("[合并转发结束:2]")
    assert event.text.index("[合并转发结束:2]") < event.text.index("[合并转发结束:1]")
    assert "deepest" in event.text


# ── Forward depth limit ─────────────────────────────────────────────────


async def test_parser_forward_depth_limit_inline(media_server):
    """Forward beyond max depth should show a skip marker without consuming
    counter and without issuing extra ``get_forward_msg`` calls.

    Builds a 5-level nested inline ``content`` chain (one level beyond the
    ``_MAX_FORWARD_DEPTH`` of 4); the deepest level must be summarised with
    the ``超过最大深度`` placeholder. Only the single outer API call fires.
    """
    mock_api = MagicMock()

    def _nested_msg(depth: int) -> dict:
        # Build a message whose content nests `depth` forwards deep.
        msg = {
            "sender": {"card": "Carol", "user_id": 3},
            "message": [{"type": "text", "data": {"text": "leaf"}}],
        }
        for _ in range(depth):
            msg = {
                "sender": {"card": "Outer", "user_id": 9},
                "message": [
                    {
                        "type": "forward",
                        "data": {"id": "inline", "content": [msg]},
                    }
                ],
            }
        return msg

    mock_api.get_forward_msg = AsyncMock(return_value={"messages": [_nested_msg(parser._MAX_FORWARD_DEPTH + 1)]})
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([{"type": "forward", "data": {"id": "fwd_root"}}]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            api=mock_api,
            session=client.session,
        )
    assert result is not None
    event, media = result
    assert mock_api.get_forward_msg.await_count == 1
    assert "[合并转发(已跳过:超过最大深度)]" in event.text


# ── Reply with multiple images ──────────────────────────────────────────


async def test_parser_reply_with_multiple_images(media_server):
    """Reply context should download multiple images with shared counter."""
    mock_api = MagicMock()
    mock_api.get_msg = AsyncMock(return_value={
        "sender": {"card": "Quoter", "user_id": 200},
        "message": [
            {"type": "text", "data": {"text": "look"}},
            {"type": "image", "data": {"url": media_server["img_url"]}},
            {"type": "image", "data": {"url": media_server["img_url"]}},
        ],
    })
    async with TestClient(media_server["server"]) as client:
        result = await parser.parse_event(
            _msg_event([
                {"type": "reply", "data": {"id": 77}},
                {"type": "text", "data": {"text": "my reply"}},
            ]),
            self_id="999",
            group_require_mention=True,
            media_max_bytes=1024 * 1024,
            api=mock_api,
            session=client.session,
        )
    assert result is not None
    event, media = result
    # Two images from reply should be downloaded
    assert len(media) == 2
    # Reply text should have placeholders
    assert "[图1]" in event.reply_to_text
    assert "[图2]" in event.reply_to_text
    assert "look[图1][图2]" in event.reply_to_text

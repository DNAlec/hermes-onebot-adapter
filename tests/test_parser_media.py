"""Integration tests for the parser with media URL passthrough.

These verify that the parser renders media markers as URL placeholders
(e.g. ``[图1](https://...)``) without downloading any bytes.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
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


# ── Image ────────────────────────────────────────────────────────────────


async def test_parser_image_url_placeholder():
    result = await parser.parse_event(
        _msg_event([{"type": "image", "data": {"url": "https://example.com/cat.jpg"}}]),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert event.message_type == "text"
    assert "[图1](https://example.com/cat.jpg)" in event.text


async def test_parser_image_fallback_to_file():
    """Image segment without url falls back to data.file as the URL."""
    result = await parser.parse_event(
        _msg_event([{"type": "image", "data": {"file": "/local/snap.jpg"}}]),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert "/local/snap.jpg" in event.text


# ── Voice ───────────────────────────────────────────────────────────────


async def test_parser_voice_url_placeholder():
    result = await parser.parse_event(
        _msg_event([{"type": "record", "data": {"url": "https://example.com/voice.silk"}}]),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert "[语音1](https://example.com/voice.silk)" in event.text


# ── Video ───────────────────────────────────────────────────────────────


async def test_parser_video_url_placeholder():
    result = await parser.parse_event(
        _msg_event([{"type": "video", "data": {"url": "https://example.com/clip.mp4"}}]),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert "[视频1](https://example.com/clip.mp4)" in event.text


# ── File ────────────────────────────────────────────────────────────────


async def test_parser_file_url_placeholder():
    result = await parser.parse_event(
        _msg_event([{"type": "file", "data": {"file": "report.pdf", "url": "https://example.com/report.pdf"}}]),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert "[文件1:report.pdf](https://example.com/report.pdf)" in event.text


async def test_parser_file_no_url():
    """File segment with only file_id and no URL → placeholder shows 无URL."""
    result = await parser.parse_event(
        _msg_event([{"type": "file", "data": {"file": "doc.zip", "file_id": "abc123"}}]),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert "[文件1:doc.zip](无URL)" in event.text


# ── Mixed media ─────────────────────────────────────────────────────────


async def test_parser_text_and_image_mixed():
    result = await parser.parse_event(
        _msg_event([
            {"type": "text", "data": {"text": "look at this "}},
            {"type": "image", "data": {"url": "https://example.com/pic.jpg"}},
        ]),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert "look at this" in event.text
    assert "[图1](https://example.com/pic.jpg)" in event.text


async def test_parser_multiple_images_with_text_between():
    result = await parser.parse_event(
        _msg_event([
            {"type": "image", "data": {"url": "https://example.com/a.jpg"}},
            {"type": "text", "data": {"text": " and "}},
            {"type": "image", "data": {"url": "https://example.com/b.jpg"}},
        ]),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert "[图1](https://example.com/a.jpg)" in event.text
    assert "[图2](https://example.com/b.jpg)" in event.text
    assert " and " in event.text


# ── Reply context with media ─────────────────────────────────────────────


async def test_parser_reply_with_image():
    """Quoted message with an image renders the URL in reply_to_text."""
    mock_api = MagicMock()
    quoted = {
        "sender": {"user_id": 200, "nickname": "Other", "card": ""},
        "message_id": 555,
        "message": [{"type": "image", "data": {"url": "https://example.com/quoted.jpg"}}],
        "real_seq": "10",
    }
    mock_api.get_msg = AsyncMock(return_value=quoted)
    result = await parser.parse_event(
        _msg_event([
            {"type": "reply", "data": {"id": "555"}},
            {"type": "text", "data": {"text": "reply"}},
        ]),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
        session=None,
    )
    assert result is not None
    event = result
    assert event.reply_to_text is not None
    assert "[图1](https://example.com/quoted.jpg)" in event.reply_to_text
    assert "reply" in event.text


async def test_parser_reply_with_multiple_images():
    """Quoted message with multiple images gives consecutive numbering."""
    mock_api = MagicMock()
    quoted = {
        "sender": {"user_id": 200, "nickname": "Other", "card": ""},
        "message_id": 555,
        "message": [
            {"type": "image", "data": {"url": "https://example.com/q1.jpg"}},
            {"type": "image", "data": {"url": "https://example.com/q2.jpg"}},
        ],
        "real_seq": "10",
    }
    mock_api.get_msg = AsyncMock(return_value=quoted)
    result = await parser.parse_event(
        _msg_event([
            {"type": "reply", "data": {"id": "555"}},
            {"type": "text", "data": {"text": "ok"}},
        ]),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
        session=None,
    )
    assert result is not None
    event = result
    assert event.reply_to_text is not None
    assert "[图1](https://example.com/q1.jpg)" in event.reply_to_text
    assert "[图2](https://example.com/q2.jpg)" in event.reply_to_text


# ── Forward expansion with media ─────────────────────────────────────────


async def test_parser_forward_with_image():
    """Forward containing image renders URL placeholder within begin/end tags."""
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"user_id": 300, "nickname": "Alice", "card": ""},
                "message_id": 111,
                "message": [{"type": "image", "data": {"url": "https://example.com/fwd.jpg"}}],
            },
            {
                "sender": {"user_id": 300, "nickname": "Alice", "card": ""},
                "message_id": 112,
                "message": [{"type": "text", "data": {"text": "nice pic"}}],
            },
        ],
    })
    result = await parser.parse_event(
        _msg_event([{"type": "forward", "data": {"id": "fwd123"}}]),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
        session=None,
    )
    assert result is not None
    event = result
    assert "[合并转发开始:1]" in event.text
    assert "[图1](https://example.com/fwd.jpg)" in event.text
    assert "nice pic" in event.text
    assert "[合并转发结束:1]" in event.text


async def test_parser_forward_with_multiple_media_types():
    """Forward mixing image, video, and text renders all placeholders."""
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"user_id": 300, "nickname": "Alice"},
                "message_id": 111,
                "message": [{"type": "image", "data": {"url": "https://example.com/a.jpg"}}],
            },
            {
                "sender": {"user_id": 300, "nickname": "Alice"},
                "message_id": 112,
                "message": [{"type": "video", "data": {"url": "https://example.com/v.mp4"}}],
            },
            {
                "sender": {"user_id": 300, "nickname": "Alice"},
                "message_id": 113,
                "message": [{"type": "text", "data": {"text": "caption"}}],
            },
        ],
    })
    result = await parser.parse_event(
        _msg_event([{"type": "forward", "data": {"id": "fwd123"}}]),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
        session=None,
    )
    assert result is not None
    event = result
    assert "[图1](https://example.com/a.jpg)" in event.text
    assert "[视频2](https://example.com/v.mp4)" in event.text
    assert "caption" in event.text


# ── Nested forward (inline content) ─────────────────────────────────────


async def test_parser_nested_forward_inline_content():
    """NapCat provides nested forward content inline; no second API call needed."""
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"user_id": 300, "nickname": "Outer"},
                "message_id": 111,
                "message": [
                    {
                        "type": "forward",
                        "data": {
                            "id": "nested-fwd-id",
                            "content": [
                                {
                                    "sender": {"user_id": 400, "nickname": "Inner"},
                                    "message_id": 222,
                                    "message": [
                                        {"type": "text", "data": {"text": "inside nested"}},
                                    ],
                                },
                            ],
                        },
                    },
                ],
            },
        ],
    })
    result = await parser.parse_event(
        _msg_event([{"type": "forward", "data": {"id": "fwd123"}}]),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
        session=None,
    )
    assert result is not None
    event = result
    assert "[合并转发开始:1]" in event.text
    assert "[合并转发开始:2]" in event.text
    assert "inside nested" in event.text
    # The nested forward should NOT trigger another get_forward_msg call
    # (get_forward_msg only called once for the top-level id)
    assert mock_api.get_forward_msg.await_count == 1


async def test_parser_nested_forward_inline_content_3levels():
    """3-level nested inline content is correctly expanded."""
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"user_id": 300, "nickname": "L1"},
                "message_id": 1,
                "message": [{
                    "type": "forward",
                    "data": {"id": "lvl2", "content": [
                        {
                            "sender": {"user_id": 400, "nickname": "L2"},
                            "message_id": 2,
                            "message": [{
                                "type": "forward",
                                "data": {"id": "lvl3", "content": [
                                    {
                                        "sender": {"user_id": 500, "nickname": "L3"},
                                        "message_id": 3,
                                        "message": [{"type": "text", "data": {"text": "deepest"}}],
                                    },
                                ]},
                            }],
                        },
                    ]},
                }],
            },
        ],
    })
    result = await parser.parse_event(
        _msg_event([{"type": "forward", "data": {"id": "fwd123"}}]),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
        session=None,
    )
    assert result is not None
    event = result
    assert event.text.count("[合并转发开始:1]") == 1
    assert event.text.count("[合并转发开始:2]") == 1
    assert event.text.count("[合并转发开始:3]") == 1
    assert "deepest" in event.text


async def test_parser_forward_depth_limit_inline():
    """Forwards nested beyond _MAX_FORWARD_DEPTH (4) are skipped with a note."""
    # Build a message with 5 nested forwards via inline content (depth 0..4 OK, depth 5 skippy)
    deepevent_msg = {"type": "text", "data": {"text": "deepest real"}}
    for _ in range(6):
        deepevent_msg = {
            "type": "forward",
            "data": {"id": "x", "content": [
                {
                    "sender": {"user_id": 1, "nickname": "n"},
                    "message_id": 1,
                    "message": [deepevent_msg],
                },
            ]},
        }
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"user_id": 300, "nickname": "Root"},
                "message_id": 1,
                "message": [deepevent_msg],
            },
        ],
    })
    result = await parser.parse_event(
        _msg_event([{"type": "forward", "data": {"id": "fwd123"}}]),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
        session=None,
    )
    assert result is not None
    event = result
    # The deepest level should have skipped note
    assert "已跳过:超过最大深度" in event.text


# ── Reply context whose quoted message is itself a forward ─────────────


async def test_parser_reply_with_forward():
    """Quoted message is a forward → reply_to_text contains the full expansion."""
    mock_api = MagicMock()
    mock_api.get_msg = AsyncMock(return_value={
        "sender": {"user_id": 200, "nickname": "Sender", "card": ""},
        "message_id": 555,
        "message": [{"type": "forward", "data": {"id": "qfwd123"}}],
        "real_seq": "10",
    })
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"user_id": 300, "nickname": "Fwd", "card": ""},
                "message_id": 111,
                "message": [{"type": "text", "data": {"text": "forward content"}}],
            },
        ],
    })
    result = await parser.parse_event(
        _msg_event([
            {"type": "reply", "data": {"id": "555"}},
            {"type": "text", "data": {"text": "re"}}
        ]),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
        session=None,
    )
    assert result is not None
    event = result
    assert event.reply_to_text is not None
    assert "[合并转发开始:1]" in event.reply_to_text
    assert "forward content" in event.reply_to_text
"""Tests for the message-flow log line formatters."""
from __future__ import annotations

from unittest.mock import AsyncMock

from onebot_adapter.onebot.log_format import format_recv_line, format_send_line, truncate
from onebot_adapter.relay.protocol import NormalizedEvent

# ── truncate ─────────────────────────────────────────────────────────────


def test_truncate_short_text_unchanged():
    assert truncate("hello", 40) == "hello"


def test_truncate_long_text_gets_ellipsis():
    assert truncate("a" * 100, 40) == "a" * 40 + "..."


def test_truncate_zero_limit_no_truncation():
    assert truncate("a" * 1000, 0) == "a" * 1000


def test_truncate_exact_boundary_no_truncation():
    assert truncate("a" * 40, 40) == "a" * 40


# ── format_recv_line ─────────────────────────────────────────────────────


def test_format_recv_line_group():
    ev = NormalizedEvent(
        message_id="1",
        chat_id="group:875253815",
        chat_type="group",
        user_id="2270892742",
        user_name="听一夜相思愁",
        text="龙太克制教了",
        chat_name="875253815(影之诗穷哥们聚集地)",
    )
    line = format_recv_line(ev)
    assert "群聊" in line
    assert "[875253815(影之诗穷哥们聚集地)]" in line
    assert "[听一夜相思愁(2270892742)]" in line
    assert "龙太克制教了" in line


def test_format_recv_line_dm():
    ev = NormalizedEvent(
        message_id="1",
        chat_id="2270892742",
        chat_type="dm",
        user_id="2270892742",
        user_name="听一夜相思愁",
        text="你好",
        chat_name="听一夜相思愁",
    )
    line = format_recv_line(ev)
    assert "私聊" in line
    assert "[听一夜相思愁(2270892742)]" in line
    assert "你好" in line


def test_format_recv_line_truncate_default_40():
    long_text = "a" * 100
    ev = NormalizedEvent(
        message_id="1", chat_id="g:1", chat_type="group",
        user_id="2", user_name="u", text=long_text, chat_name="1(name)",
    )
    line = format_recv_line(ev, preview=40)
    assert "..." in line
    assert "a" * 40 in line
    assert "a" * 100 not in line


def test_format_recv_line_truncate_zero_no_limit():
    long_text = "a" * 100
    ev = NormalizedEvent(
        message_id="1", chat_id="g:1", chat_type="group",
        user_id="2", user_name="u", text=long_text, chat_name="1(name)",
    )
    line = format_recv_line(ev, preview=0)
    assert "..." not in line
    assert long_text in line


def test_format_recv_line_group_no_name():
    ev = NormalizedEvent(
        message_id="1", chat_id="group:42", chat_type="group",
        user_id="10", user_name="u", text="hi", chat_name="42",
    )
    line = format_recv_line(ev)
    assert "[42]" in line


# ── format_send_line ─────────────────────────────────────────────────────


async def test_format_send_line_text():
    segs = [{"type": "text", "data": {"text": "hello"}}]
    line = await format_send_line(chat_id="group:875253815", segs=segs, is_group=True, group_name="影之诗穷哥们聚集地")
    assert "群聊" in line
    assert "[875253815(影之诗穷哥们聚集地)]" in line
    assert "hello" in line


async def test_format_send_line_dm():
    segs = [{"type": "text", "data": {"text": "hi"}}]
    line = await format_send_line(chat_id="2270892742", segs=segs, is_group=False)
    assert "私聊" in line
    assert "[2270892742]" in line
    assert "hi" in line


async def test_format_send_line_with_reply_in_segs():
    segs = [
        {"type": "reply", "data": {"id": "12345"}},
        {"type": "text", "data": {"text": "reply content"}},
    ]
    line = await format_send_line(chat_id="group:42", segs=segs, is_group=True, group_name="g")
    assert "[回复消息#12345]" in line
    assert "reply content" in line


async def test_format_send_line_with_reply_to_param():
    """reply_to passed separately (send_text path) should surface [回复消息#id]."""
    segs = [{"type": "text", "data": {"text": "ok"}}]
    line = await format_send_line(
        chat_id="group:42", segs=segs, is_group=True, group_name="g", reply_to="99",
    )
    assert "[回复消息#99]" in line
    assert "ok" in line


async def test_format_send_line_with_at_resolved():
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value="小夜五世")
    segs = [{"type": "at", "data": {"qq": "3037068276"}}]
    line = await format_send_line(
        chat_id="group:746315490", segs=segs, is_group=True, group_name="深夜的内群",
        name_resolver=resolver,
    )
    assert "@小夜五世(3037068276)" in line


async def test_format_send_line_with_at_unresolved_falls_back_to_qq():
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value="")
    segs = [{"type": "at", "data": {"qq": "3037068276"}}]
    line = await format_send_line(
        chat_id="group:746315490", segs=segs, is_group=True, group_name="g",
        name_resolver=resolver,
    )
    assert "@3037068276" in line


async def test_format_send_line_with_at_no_resolver():
    segs = [{"type": "at", "data": {"qq": "3037068276"}}]
    line = await format_send_line(chat_id="group:42", segs=segs, is_group=True, group_name="g")
    assert "@3037068276" in line


async def test_format_send_line_image():
    segs = [{"type": "image", "data": {"file": "http://x/y.jpg"}}]
    line = await format_send_line(chat_id="group:42", segs=segs, is_group=True, group_name="g")
    assert "[图片]" in line


async def test_format_send_line_record():
    segs = [{"type": "record", "data": {"file": "http://x/y.wav"}}]
    line = await format_send_line(chat_id="group:42", segs=segs, is_group=True, group_name="g")
    assert "[语音]" in line


async def test_format_send_line_video():
    segs = [{"type": "video", "data": {"file": "http://x/y.mp4"}}]
    line = await format_send_line(chat_id="group:42", segs=segs, is_group=True, group_name="g")
    assert "[视频]" in line


async def test_format_send_line_file():
    segs = [{"type": "file", "data": {"name": "doc.pdf"}}]
    line = await format_send_line(chat_id="group:42", segs=segs, is_group=True, group_name="g")
    assert "[文件:doc.pdf]" in line


async def test_format_send_line_mixed_content():
    segs = [
        {"type": "reply", "data": {"id": "100"}},
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": "这是回复"}},
        {"type": "image", "data": {"file": "x.jpg"}},
    ]
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value="某人")
    line = await format_send_line(
        chat_id="group:42", segs=segs, is_group=True, group_name="g",
        name_resolver=resolver, preview=200,
    )
    assert "[回复消息#100]" in line
    assert "@某人(123)" in line
    assert "这是回复" in line
    assert "[图片]" in line


async def test_format_send_line_truncate():
    long_text = "a" * 100
    segs = [{"type": "text", "data": {"text": long_text}}]
    line = await format_send_line(chat_id="group:42", segs=segs, is_group=True, group_name="g", preview=40)
    assert "..." in line
    assert "a" * 40 in line
    assert "a" * 100 not in line


async def test_format_send_line_no_group_name():
    segs = [{"type": "text", "data": {"text": "hi"}}]
    line = await format_send_line(chat_id="group:42", segs=segs, is_group=True, group_name="")
    assert "[42]" in line


async def test_format_send_line_name_resolver_exception_falls_back():
    """If name_resolver raises, we still get a line with @QQ."""
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(side_effect=Exception("boom"))
    segs = [{"type": "at", "data": {"qq": "555"}}, {"type": "text", "data": {"text": "x"}}]
    line = await format_send_line(
        chat_id="group:42", segs=segs, is_group=True, group_name="g",
        name_resolver=resolver,
    )
    assert "@555" in line
    assert "x" in line


async def test_format_send_line_empty_segs():
    line = await format_send_line(chat_id="group:42", segs=[], is_group=True, group_name="g")
    assert "群聊" in line
    assert "[42" in line

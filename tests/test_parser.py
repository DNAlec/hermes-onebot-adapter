"""Tests for the OneBot 11 event parser."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from onebot_adapter.onebot import parser


def _msg_event(
    text: str = "",
    *,
    message_type: str = "private",
    user_id: int = 100,
    group_id: int = 0,
    segments: list[dict] | None = None,
    message_id: int = 1,
) -> dict[str, Any]:
    if segments is None:
        segments = [{"type": "text", "data": {"text": text}}]
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


async def test_parse_private_text():
    result = await parser.parse_event(
        _msg_event("hello", user_id=100),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert event.text == "hello"
    assert event.chat_id == "100"
    assert event.chat_type == "dm"


async def test_parse_group_with_mention():
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi bot"}},
    ]
    result = await parser.parse_event(
        _msg_event("hi bot", message_type="group", group_id=42, segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert event.chat_id == "group:42"
    assert event.chat_type == "group"
    assert event.text == "[Tester(100)#1]: hi bot"
    assert event.user_id == "100"


async def test_parse_group_without_mention_filtered():
    segs = [{"type": "text", "data": {"text": "no mention"}}]
    result = await parser.parse_event(
        _msg_event("no mention", message_type="group", group_id=42, segments=segs),
        self_id="999",
        group_require_mention=True,
    )
    assert result is None


async def test_parse_group_mention_not_required():
    segs = [{"type": "text", "data": {"text": "hi"}}]
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, segments=segs),
        self_id="999",
        group_require_mention=False,
    )
    assert result is not None


async def test_parse_non_message_event_filtered():
    result = await parser.parse_event(
        {"post_type": "notice", "notice_type": "group_upload"},
        self_id="999",
        group_require_mention=True,
    )
    assert result is None


async def test_parse_empty_message_filtered():
    result = await parser.parse_event(
        _msg_event("", segments=[]),
        self_id="999",
        group_require_mention=True,
    )
    assert result is None


async def test_parse_group_slash_command_no_prefix():
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "/help"}},
    ]
    result = await parser.parse_event(
        _msg_event("/help", message_type="group", group_id=42, segments=segs),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert event.text == "/help"


async def test_parse_reply_context():
    mock_api = MagicMock()
    mock_api.get_msg = AsyncMock(return_value={
        "sender": {"card": "Quoter", "user_id": 200},
        "message": [{"type": "text", "data": {"text": "quoted text"}}],
    })
    segs = [
        {"type": "reply", "data": {"id": 55}},
        {"type": "text", "data": {"text": "my reply"}},
    ]
    result = await parser.parse_event(
        _msg_event("my reply", segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
    )
    assert result is not None
    event = result
    assert event.reply_to_message_id == "55"
    assert event.reply_to_text == "[Quoter(200)]: quoted text"
    mock_api.get_msg.assert_awaited_once_with(55)


async def test_parse_group_reply_with_mention_first_only():
    """群聊引用回复 + @bot：reply 段应被跳过，@bot 视为首段，消息不被丢弃。

    Regression: has_bot_mention_first 仅看 segments[0]，遇到 reply 段返回 False，
    导致 group_require_mention + group_mention_first_only 下引用+@bot 消息被丢弃。
    """
    mock_api = MagicMock()
    mock_api.get_msg = AsyncMock(return_value={
        "sender": {"card": "Quoter", "user_id": 200},
        "message": [{"type": "text", "data": {"text": "quoted text"}}],
    })
    segs = [
        {"type": "reply", "data": {"id": 55}},
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "my reply"}},
    ]
    result = await parser.parse_event(
        _msg_event("my reply", message_type="group", group_id=42,
                   segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        mention_first_only=True,
        api=mock_api,
    )
    assert result is not None
    event = result
    assert event.text == "[Tester(100)#1]: my reply"
    assert event.chat_id == "group:42"
    assert event.reply_to_message_id == "55"


async def test_parse_group_reply_without_mention_dropped_first_only():
    """群聊引用回复未 @bot：mention_first_only 下应被丢弃。"""
    mock_api = MagicMock()
    mock_api.get_msg = AsyncMock(return_value={
        "sender": {"card": "Quoter", "user_id": 200},
        "message": [{"type": "text", "data": {"text": "quoted text"}}],
    })
    segs = [
        {"type": "reply", "data": {"id": 55}},
        {"type": "text", "data": {"text": "my reply"}},
    ]
    result = await parser.parse_event(
        _msg_event("my reply", message_type="group", group_id=42,
                   segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        mention_first_only=True,
        api=mock_api,
    )
    assert result is None  # dropped: reply skipped, first non-reply is text, not @bot


async def test_parse_forward_expansion():
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"card": "Alice", "user_id": 1},
                "message": [{"type": "text", "data": {"text": "msg one"}}],
            },
            {
                "sender": {"card": "Bob", "user_id": 2},
                "message": [{"type": "text", "data": {"text": "msg two"}}],
            },
        ]
    })
    segs = [{"type": "forward", "data": {"id": "fwd1"}}]
    result = await parser.parse_event(
        _msg_event("", segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
    )
    assert result is not None
    event = result
    assert "[合并转发开始:1]" in event.text
    assert "[合并转发结束:1]" in event.text
    # 顶层只包一层,不应出现双重 [合并转发开始:1]\n[合并转发开始:1]
    assert event.text.count("[合并转发开始:1]") == 1
    assert event.text.count("[合并转发结束:1]") == 1
    assert "[Alice]: msg one" in event.text
    assert "[Bob]: msg two" in event.text


async def test_parse_forward_prefix_no_qq_no_seq():
    """Forward sub-message prefixes must show only the nickname — no QQ
    number, no real_seq — because these IDs are unreliable in merged
    forwards (NapCat may fill placeholder values).
    """
    mock_api = MagicMock()
    mock_api.get_forward_msg = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"card": "小 白 龙🍃", "user_id": 1094950020},
                "message_id": 999,
                "real_seq": "163963",
                "group_id": 284840486,
                "message": [{"type": "text", "data": {"text": "hello"}}],
            },
            {
                "sender": {"card": "Sylphy", "user_id": 469405634},
                "message_id": 998,
                "real_seq": "163964",
                "group_id": 284840486,
                "message": [{"type": "text", "data": {"text": "world"}}],
            },
        ]
    })
    segs = [{"type": "forward", "data": {"id": "fwd1"}}]
    result = await parser.parse_event(
        _msg_event("", segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
    )
    assert result is not None
    event = result
    assert "[小 白 龙🍃]: hello" in event.text
    assert "[Sylphy]: world" in event.text
    assert "1094950020" not in event.text
    assert "469405634" not in event.text
    assert "163963" not in event.text
    assert "163964" not in event.text
    assert "284840486" not in event.text


async def test_parse_image_url_placeholder():
    result = await parser.parse_event(
        _msg_event("", segments=[{"type": "image", "data": {"url": "http://x/img.png"}}], user_id=100),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert "[图1](http://x/img.png)" in event.text


async def test_parse_reply_with_image():
    mock_api = MagicMock()
    mock_api.get_msg = AsyncMock(return_value={
        "sender": {"card": "Q", "user_id": 200},
        "message": [{"type": "image", "data": {"url": "http://x/q.png"}}],
    })
    segs = [{"type": "reply", "data": {"id": 7}}, {"type": "text", "data": {"text": "see this"}}]
    result = await parser.parse_event(
        _msg_event("see this", segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
    )
    assert result is not None
    event = result
    assert "[图1](http://x/q.png)" in event.reply_to_text


# ── @ mention name resolution ────────────────────────────────────────────


def _mock_name_resolver(
    names: dict[str, str] | None = None,
    *,
    group_names: dict[str, str] | None = None,
) -> MagicMock:
    """Build a fake NameResolver whose resolve() returns from *names* and
    resolve_group_name() returns from *group_names*."""
    resolver = MagicMock()
    _names = names or {}
    _group_names = group_names or {}

    async def _resolve(user_id, group_id=""):
        return _names.get(user_id, "")

    async def _resolve_group_name(group_id):
        return _group_names.get(str(group_id), "")

    resolver.resolve = _resolve
    resolver.resolve_group_name = _resolve_group_name
    return resolver


async def test_parse_at_mentions_resolved_in_main_message():
    """@QQ号 in main message should be resolved to @QQ号(昵称)."""
    segs = [
        {"type": "at", "data": {"qq": "123456"}},
        {"type": "text", "data": {"text": " hello"}},
        {"type": "at", "data": {"qq": "789012"}},
    ]
    resolver = _mock_name_resolver({"123456": "张三", "789012": "李四"})
    result = await parser.parse_event(
        _msg_event("", segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert "@123456(张三)" in event.text
    assert "@789012(李四)" in event.text


async def test_parse_at_mentions_resolved_in_reply_context():
    """@QQ号 in reply context should be resolved to @QQ号(昵称)."""
    mock_api = MagicMock()
    mock_api.get_msg = AsyncMock(return_value={
        "sender": {"card": "Quoter", "user_id": 200},
        "message": [
            {"type": "at", "data": {"qq": "111111"}},
            {"type": "text", "data": {"text": " hey"}},
        ],
    })
    resolver = _mock_name_resolver({"111111": "Bob"})
    segs = [
        {"type": "reply", "data": {"id": 55}},
        {"type": "text", "data": {"text": "my reply"}},
    ]
    result = await parser.parse_event(
        _msg_event("my reply", segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        api=mock_api,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert "@111111(Bob)" in event.reply_to_text


async def test_parse_at_mentions_unknown_user_when_resolution_fails():
    """When name resolver returns empty, @ should show (未知用户)."""
    segs = [
        {"type": "at", "data": {"qq": "999999"}},
        {"type": "text", "data": {"text": " hi"}},
    ]
    resolver = _mock_name_resolver({})
    result = await parser.parse_event(
        _msg_event("", segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert "@999999(未知用户)" in event.text


async def test_parse_at_mentions_no_resolver_keeps_at_qq_format():
    """Without a name_resolver, @QQ号 should stay as-is."""
    segs = [
        {"type": "at", "data": {"qq": "123456"}},
        {"type": "text", "data": {"text": " hi"}},
    ]
    result = await parser.parse_event(
        _msg_event("", segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert "@123456" in event.text
    assert "(" not in event.text


# ── Admin prefix in group messages ──────────────────────────────────────


async def test_parse_group_admin_prefix():
    """管理员发送群消息应带有 (管理员) 标识。"""
    from onebot_adapter.config import AdapterConfig

    cfg = AdapterConfig(global_admins=["100"], message_show_group_id=False)
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        config=cfg,
    )
    assert result is not None
    event = result
    assert event.is_admin is True
    assert "[Tester(100)(管理员)#1]: hi" == event.text


async def test_parse_group_non_admin_no_suffix():
    """非管理员发送群消息不带 (管理员) 标识。"""
    from onebot_adapter.config import AdapterConfig

    cfg = AdapterConfig(global_admins=["200"], message_show_group_id=False)
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        config=cfg,
    )
    assert result is not None
    event = result
    assert event.is_admin is False
    assert "[Tester(100)#1]: hi" == event.text
    assert "(管理员)" not in event.text


# ── First-@ and keyword trigger tests ──────────────────────────────────


async def test_first_mention_only_triggers_when_at_first():
    """mention_first_only=True: @bot as first segment → triggers."""
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi bot"}},
    ]
    result = await parser.parse_event(
        _msg_event("hi bot", message_type="group", group_id=42, segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        mention_first_only=True,
    )
    assert result is not None
    event = result
    assert event.text == "[Tester(100)#1]: hi bot"


async def test_first_mention_only_filtered_when_not_first():
    """mention_first_only=True: @bot in middle → filtered (returns None)."""
    segs = [
        {"type": "text", "data": {"text": "hi "}},
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "bot"}},
    ]
    result = await parser.parse_event(
        _msg_event("hi @999 bot", message_type="group", group_id=42, segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        mention_first_only=True,
    )
    assert result is None


async def test_keyword_trigger_any_position():
    """trigger_keywords with keyword_first_only=False: keyword anywhere → triggers."""
    segs = [{"type": "text", "data": {"text": "hello #bot please"}}]
    result = await parser.parse_event(
        _msg_event("hello #bot please", message_type="group", group_id=42, segments=segs),
        self_id="999",
        group_require_mention=False,
        trigger_keywords=["#bot"],
        keyword_first_only=False,
    )
    assert result is not None


async def test_keyword_trigger_filtered_when_no_keyword():
    """trigger_keywords present but message lacks keyword → filtered."""
    segs = [{"type": "text", "data": {"text": "nothing here"}}]
    result = await parser.parse_event(
        _msg_event("nothing here", message_type="group", group_id=42, segments=segs),
        self_id="999",
        group_require_mention=False,
        trigger_keywords=["#bot"],
        keyword_first_only=False,
    )
    assert result is None


async def test_keyword_first_only_triggers_at_start():
    """keyword_first_only=True: keyword at text start → triggers."""
    segs = [{"type": "text", "data": {"text": "#bot hello"}}]
    result = await parser.parse_event(
        _msg_event("#bot hello", message_type="group", group_id=42, segments=segs),
        self_id="999",
        group_require_mention=False,
        trigger_keywords=["#bot"],
        keyword_first_only=True,
    )
    assert result is not None


async def test_keyword_first_only_filtered_when_mid():
    """keyword_first_only=True: keyword in middle → filtered."""
    segs = [{"type": "text", "data": {"text": "hi #bot"}}]
    result = await parser.parse_event(
        _msg_event("hi #bot", message_type="group", group_id=42, segments=segs),
        self_id="999",
        group_require_mention=False,
        trigger_keywords=["#bot"],
        keyword_first_only=True,
    )
    assert result is None


async def test_mention_or_keyword_both_pass():
    """require_mention=True + keywords: satisfying @bot alone triggers (OR)."""
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "no keyword here"}},
    ]
    result = await parser.parse_event(
        _msg_event("no keyword here", message_type="group", group_id=42, segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        trigger_keywords=["#bot"],
        keyword_first_only=False,
    )
    assert result is not None


async def test_mention_or_keyword_neither_filtered():
    """require_mention=True + keywords: neither satisfied → filtered."""
    segs = [{"type": "text", "data": {"text": "nothing relevant"}}]
    result = await parser.parse_event(
        _msg_event("nothing relevant", message_type="group", group_id=42, segments=segs),
        self_id="999",
        group_require_mention=True,
        trigger_keywords=["#bot"],
        keyword_first_only=False,
    )
    assert result is None


async def test_no_trigger_requirements_passes_all():
    """require_mention=False + no keywords → all messages pass (backward compat)."""
    segs = [{"type": "text", "data": {"text": "random msg"}}]
    result = await parser.parse_event(
        _msg_event("random msg", message_type="group", group_id=42, segments=segs),
        self_id="999",
        group_require_mention=False,
        trigger_keywords=[],
        keyword_first_only=False,
    )
    assert result is not None


async def test_keep_mention_preserves_at_segment():
    """keep_mention=True: @bot segment stays in text as @QQ号(昵称)."""
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value="BotNick")
    resolver.resolve_group_name = AsyncMock(return_value="")

    segs = [
        {"type": "at", "data": {"qq": "99999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    result = await parser.parse_event(
        _msg_event("@99999 hi", message_type="group", group_id=42, segments=segs, user_id=100),
        self_id="99999",
        group_require_mention=True,
        keep_mention=True,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert "@99999(BotNick)" in event.text


async def test_per_group_mention_first_only_override():
    """GroupConfig.mention_first_only overrides global via config.resolve_*."""
    from onebot_adapter.config import AdapterConfig, GroupConfig

    gc = GroupConfig(group_id="42", require_mention=True, mention_first_only=True)
    cfg = AdapterConfig(
        group_require_mention=True,
        group_mention_first_only=False,
        groups={"42": gc.to_dict()},
    )
    segs = [
        {"type": "text", "data": {"text": "hi "}},
        {"type": "at", "data": {"qq": "999"}},
    ]
    result = await parser.parse_event(
        _msg_event("hi @999", message_type="group", group_id=42, segments=segs, user_id=100),
        self_id="999",
        group_require_mention=True,
        config=cfg,
    )
    assert result is None


# ── message_show_group_id + chat_name ───────────────────────────────────


async def test_message_show_group_id_enabled():
    """When message_show_group_id is on, main message gets a [群:42(测试群)] header."""
    from onebot_adapter.config import AdapterConfig

    cfg = AdapterConfig(
        group_require_mention=False,
        message_show_group_id=True,
    )
    resolver = _mock_name_resolver(group_names={"42": "测试群"})
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999",
        group_require_mention=False,
        config=cfg,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert event.text.startswith("[群:42(测试群)]\n")
    assert "[Tester(100)#1]: hi" in event.text


async def test_message_show_group_id_enabled_no_group_name():
    """When group name is unavailable, header falls back to [群:42]."""
    from onebot_adapter.config import AdapterConfig

    cfg = AdapterConfig(
        group_require_mention=False,
        message_show_group_id=True,
    )
    resolver = _mock_name_resolver()  # no group_names
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999",
        group_require_mention=False,
        config=cfg,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert event.text.startswith("[群:42]\n")


async def test_message_show_group_id_disabled_explicitly():
    """With message_show_group_id=False, no [群:...] header."""
    from onebot_adapter.config import AdapterConfig

    cfg = AdapterConfig(group_require_mention=False, message_show_group_id=False)
    resolver = _mock_name_resolver(group_names={"42": "测试群"})
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999",
        group_require_mention=False,
        config=cfg,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert "[群:" not in event.text


async def test_message_show_group_id_skipped_for_dm():
    """message_show_group_id only applies to group chats, not DMs."""
    from onebot_adapter.config import AdapterConfig

    cfg = AdapterConfig(
        dm_user_filter_mode="blacklist",
        dm_user_list=[],
        message_show_group_id=True,
    )
    result = await parser.parse_event(
        _msg_event("hi", message_type="private", user_id=100),
        self_id="999",
        group_require_mention=True,
        config=cfg,
    )
    assert result is not None
    event = result
    assert "[群:" not in event.text
    assert event.text == "hi"


async def test_message_show_group_id_skipped_for_slash_command():
    """Slash commands don't get the [群:...] header."""
    from onebot_adapter.config import AdapterConfig

    cfg = AdapterConfig(
        group_require_mention=False,
        message_show_group_id=True,
    )
    resolver = _mock_name_resolver(group_names={"42": "测试群"})
    result = await parser.parse_event(
        _msg_event("/reset", message_type="group", group_id=42, user_id=100),
        self_id="999",
        group_require_mention=False,
        config=cfg,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert "[群:" not in event.text
    assert event.text == "/reset"


async def test_chat_name_group_with_name():
    """NormalizedEvent.chat_name is '群号(群名)' for group chats with name."""
    from onebot_adapter.config import AdapterConfig

    cfg = AdapterConfig(group_require_mention=False)
    resolver = _mock_name_resolver(group_names={"42": "测试群"})
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999",
        group_require_mention=False,
        config=cfg,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert event.chat_name == "42(测试群)"


async def test_chat_name_group_without_name():
    """NormalizedEvent.chat_name falls back to '群号' when group name is empty."""
    from onebot_adapter.config import AdapterConfig

    cfg = AdapterConfig(group_require_mention=False)
    resolver = _mock_name_resolver()  # no group_names
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999",
        group_require_mention=False,
        config=cfg,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert event.chat_name == "42"


async def test_chat_name_dm_is_sender_name():
    """NormalizedEvent.chat_name is sender_name for DMs."""
    result = await parser.parse_event(
        _msg_event("hi", message_type="private", user_id=100),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert event.chat_name == "Tester"  # sender card from _msg_event


async def test_message_show_group_id_per_group_override():
    """GroupConfig.message_show_group_id overrides global."""
    from onebot_adapter.config import AdapterConfig, GroupConfig

    gc = GroupConfig(group_id="42", message_show_group_id=True)
    cfg = AdapterConfig(
        group_require_mention=False,
        message_show_group_id=False,  # global off
        groups={"42": gc.to_dict()},
    )
    resolver = _mock_name_resolver(group_names={"42": "测试群"})
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999",
        group_require_mention=False,
        config=cfg,
        name_resolver=resolver,
    )
    assert result is not None
    event = result
    assert event.text.startswith("[群:42(测试群)]\n")



# ── real_seq prefix tests ────────────────────────────────────────────────


async def test_group_prefix_shows_real_seq_when_present():
    """群聊消息前缀显示 real_seq(#后数字)。"""
    ev = _msg_event("hi", message_type="group", group_id=42, user_id=100, message_id=1380622136)
    ev["real_seq"] = "15154"
    result = await parser.parse_event(
        ev,
        self_id="999",
        group_require_mention=False,
    )
    assert result is not None
    event = result
    assert "[Tester(100)#15154]: hi" == event.text
    assert "#1380622136" not in event.text  # message_id 不出现在前缀


async def test_group_prefix_falls_back_to_message_id_without_real_seq():
    """拿不到 real_seq 时群聊前缀回退显示 message_id。"""
    ev = _msg_event("hi", message_type="group", group_id=42, user_id=100, message_id=9999)
    # 不设 real_seq
    result = await parser.parse_event(
        ev,
        self_id="999",
        group_require_mention=False,
    )
    assert result is not None
    event = result
    assert "[Tester(100)#9999]: hi" == event.text


async def test_group_admin_prefix_with_real_seq():
    """管理员前缀也带 real_seq。"""
    from onebot_adapter.config import AdapterConfig, GroupConfig
    cfg = AdapterConfig(group_require_mention=False, message_show_group_id=False,
        groups={"42": GroupConfig(group_id="42", admins=["100"]).to_dict()})
    ev = _msg_event("hi", message_type="group", group_id=42, user_id=100, message_id=1)
    ev["real_seq"] = "200"
    result = await parser.parse_event(
        ev,
        self_id="999",
        group_require_mention=False,
        config=cfg,
    )
    assert result is not None
    event = result
    assert "[Tester(100)(管理员)#200]: hi" == event.text


async def test_private_prefix_has_no_seq():
    """私聊消息不加前缀(无 # 序号)。"""
    result = await parser.parse_event(
        _msg_event("hello", user_id=100, message_id=888),
        self_id="999",
        group_require_mention=True,
    )
    assert result is not None
    event = result
    assert event.text == "hello"  # 私聊不加前缀


async def test_normalized_event_carries_real_seq():
    """NormalizedEvent.real_seq 字段被填充。"""
    ev = _msg_event("hi", message_type="group", group_id=42, user_id=100, message_id=1)
    ev["real_seq"] = "15154"
    result = await parser.parse_event(
        ev,
        self_id="999",
        group_require_mention=False,
    )
    assert result is not None
    event = result
    assert event.real_seq == "15154"


async def test_normalized_event_real_seq_empty_when_absent():
    """无 real_seq 时 NormalizedEvent.real_seq 为空字符串。"""
    result = await parser.parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100, message_id=1),
        self_id="999",
        group_require_mention=False,
    )
    assert result is not None
    event = result
    assert event.real_seq == ""


async def test_slash_command_no_prefix_with_real_seq():
    """斜杠命令不加前缀(即使有 real_seq)。"""
    ev = _msg_event("/reset", message_type="group", group_id=42, user_id=100, message_id=1)
    ev["real_seq"] = "300"
    result = await parser.parse_event(
        ev,
        self_id="999",
        group_require_mention=False,
    )
    assert result is not None
    event = result
    assert event.text == "/reset"

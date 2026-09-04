"""Tests for GroupConfig, group filtering, session mode, and admin computation."""
from __future__ import annotations

from onebot_adapter.config import AdapterConfig, GroupConfig
from onebot_adapter.onebot.parser import parse_event
from onebot_adapter.relay.protocol import DroppedEvent, FilteredEvent


def _msg_event(
    text: str = "hello",
    *,
    message_type: str = "private",
    user_id: int = 100,
    group_id: int = 0,
    segments: list[dict] | None = None,
    message_id: int = 1,
) -> dict:
    if segments is None:
        segments = [{"type": "text", "data": {"text": text}}]
    ev: dict = {
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


# ── GroupConfig dataclass ────────────────────────────────────────────────


def test_group_config_defaults():
    gc = GroupConfig(group_id="123")
    assert gc.enabled is True
    assert gc.require_mention is None
    assert gc.custom_prompt == ""
    assert gc.admins == []
    assert gc.outbound_filter_enabled is None
    assert gc.outbound_filter_patterns is None


def test_group_config_to_dict_roundtrip():
    gc = GroupConfig(group_id="42", name="Test", admins=["1", "2"])
    d = gc.to_dict()
    gc2 = GroupConfig.from_dict(d)
    assert gc2.group_id == "42"
    assert gc2.name == "Test"
    assert gc2.admins == ["1", "2"]


# ── AdapterConfig group/user helpers ─────────────────────────────────────


def test_is_group_user_allowed_blacklist_empty_allows_all():
    cfg = AdapterConfig()
    assert cfg.is_group_user_allowed("123", "100") is True


def test_is_group_user_allowed_blacklist_blocks_listed():
    cfg = AdapterConfig(groups={"42": GroupConfig(
        group_id="42", group_user_filter_mode="blacklist", group_user_list=["100"]).to_dict()})
    assert cfg.is_group_user_allowed("42", "100") is False
    assert cfg.is_group_user_allowed("42", "200") is True


def test_is_group_user_allowed_whitelist_blocks_unlisted():
    cfg = AdapterConfig(groups={"42": GroupConfig(
        group_id="42", group_user_filter_mode="whitelist", group_user_list=["100"]).to_dict()})
    assert cfg.is_group_user_allowed("42", "100") is True
    assert cfg.is_group_user_allowed("42", "200") is False


def test_is_group_user_allowed_whitelist_empty_rejects_all():
    cfg = AdapterConfig(groups={"42": GroupConfig(
        group_id="42", group_user_filter_mode="whitelist", group_user_list=[]).to_dict()})
    assert cfg.is_group_user_allowed("42", "100") is False


def test_is_group_user_allowed_unconfigured_group_defaults_blacklist_empty():
    cfg = AdapterConfig()
    assert cfg.is_group_user_allowed("999", "100") is True


def test_is_dm_allowed_default_deny_rejects_all():
    cfg = AdapterConfig()
    assert cfg.dm_policy == "deny"
    assert cfg.is_dm_allowed("100") is False
    assert cfg.is_dm_allowed("100", is_friend=True) is False


def test_is_dm_allowed_whitelist_bypasses_deny_and_friends():
    cfg = AdapterConfig(dm_policy="deny", dm_whitelist=["100"])
    assert cfg.is_dm_allowed("100") is True
    assert cfg.is_dm_allowed("200") is False
    friends = AdapterConfig(dm_policy="friends", dm_whitelist=["100"])
    assert friends.is_dm_allowed("100", is_friend=False) is True
    assert friends.dm_needs_friend_lookup("100") is False
    assert friends.dm_needs_friend_lookup("200") is True


def test_is_dm_allowed_allow_mode_blocks_blacklist_only():
    cfg = AdapterConfig(dm_policy="allow")
    assert cfg.is_dm_allowed("100") is True
    blocked = AdapterConfig(dm_policy="allow", dm_blacklist=["100"])
    assert blocked.is_dm_allowed("100") is False
    assert blocked.is_dm_allowed("200") is True


def test_is_dm_allowed_friends_mode_uses_friend_flag():
    cfg = AdapterConfig(dm_policy="friends")
    assert cfg.is_dm_allowed("100") is False
    assert cfg.is_dm_allowed("100", is_friend=True) is True


def test_is_dm_allowed_blacklist_wins_over_whitelist():
    cfg = AdapterConfig(
        dm_policy="deny", dm_whitelist=["100"], dm_blacklist=["100"],
    )
    assert cfg.is_dm_allowed("100") is False
    assert cfg.dm_needs_friend_lookup("100") is False


def test_dm_reject_reason_labels():
    deny = AdapterConfig(dm_policy="deny")
    assert deny.dm_reject_reason("100") == "禁止私聊"
    assert deny.render_dm_reject_message("禁止私聊") == "⛔ 当前私聊策略为：禁止私聊"
    listed = AdapterConfig(dm_policy="allow", dm_blacklist=["100"])
    assert listed.dm_reject_reason("100") == "禁止私聊"
    friends = AdapterConfig(dm_policy="friends")
    assert friends.dm_reject_reason("100") == "仅限好友"
    assert friends.dm_reject_reason("100", is_friend=True) is None


def test_is_admin_global():
    cfg = AdapterConfig(global_admins=["100"])
    assert cfg.is_admin("100") is True
    assert cfg.is_admin("200") is False


def test_is_admin_group_specific():
    cfg = AdapterConfig(groups={"42": GroupConfig(group_id="42", admins=["200"]).to_dict()})
    assert cfg.is_admin("200", "42") is True
    assert cfg.is_admin("200") is False  # not a global admin
    assert cfg.is_admin("100", "42") is False


def test_resolve_require_mention_global():
    cfg = AdapterConfig(group_require_mention=False)
    assert cfg.resolve_require_mention("999") is False


def test_resolve_require_mention_group_override():
    cfg = AdapterConfig(
    group_require_mention=True,
    groups={"42": GroupConfig(group_id="42", require_mention=False).to_dict()},
)
    assert cfg.resolve_require_mention("42") is False


def test_resolve_mention_first_only_global():
    cfg = AdapterConfig(group_mention_first_only=True)
    assert cfg.resolve_mention_first_only("999") is True


def test_resolve_mention_first_only_group_override():
    cfg = AdapterConfig(
        group_mention_first_only=False,
        groups={"42": GroupConfig(group_id="42", mention_first_only=True).to_dict()},
    )
    assert cfg.resolve_mention_first_only("42") is True


def test_resolve_trigger_keywords_global():
    cfg = AdapterConfig(group_trigger_keywords=["#bot", "hi"])
    assert cfg.resolve_trigger_keywords("999") == ["#bot", "hi"]


def test_resolve_trigger_keywords_group_override():
    cfg = AdapterConfig(
        group_trigger_keywords=["#bot"],
        groups={"42": GroupConfig(group_id="42", trigger_keywords=["x", "y"]).to_dict()},
    )
    assert cfg.resolve_trigger_keywords("42") == ["x", "y"]


def test_resolve_trigger_keywords_empty_list_disables():
    """An empty list per-group explicitly disables keyword triggering."""
    cfg = AdapterConfig(
        group_trigger_keywords=["#bot"],
        groups={"42": GroupConfig(group_id="42", trigger_keywords=[]).to_dict()},
    )
    assert cfg.resolve_trigger_keywords("42") == []


def test_resolve_keyword_first_only_global():
    cfg = AdapterConfig(group_keyword_first_only=True)
    assert cfg.resolve_keyword_first_only("999") is True


def test_resolve_keyword_first_only_group_override():
    cfg = AdapterConfig(
        group_keyword_first_only=False,
        groups={"42": GroupConfig(group_id="42", keyword_first_only=True).to_dict()},
    )
    assert cfg.resolve_keyword_first_only("42") is True


def test_resolve_strip_first_mention_global():
    cfg = AdapterConfig(group_strip_first_mention=True)
    assert cfg.resolve_strip_first_mention("999") is True


def test_resolve_strip_first_mention_group_override():
    cfg = AdapterConfig(
        group_strip_first_mention=False,
        groups={"42": GroupConfig(group_id="42", strip_first_mention=True).to_dict()},
    )
    assert cfg.resolve_strip_first_mention("42") is True


def test_resolve_reaction_emoji_default_enabled():
    cfg = AdapterConfig()
    assert cfg.resolve_reaction_emoji_enabled("999") is True
    assert cfg.resolve_reaction_emoji_enabled(None) is True  # DM


def test_resolve_reaction_emoji_global_enabled():
    cfg = AdapterConfig(reaction_emoji_enabled=True)
    assert cfg.resolve_reaction_emoji_enabled("999") is True
    assert cfg.resolve_reaction_emoji_enabled(None) is True


def test_resolve_reaction_emoji_group_override_true():
    cfg = AdapterConfig(
        reaction_emoji_enabled=False,
        groups={"42": GroupConfig(group_id="42", reaction_emoji_enabled=True).to_dict()},
    )
    assert cfg.resolve_reaction_emoji_enabled("42") is True
    assert cfg.resolve_reaction_emoji_enabled("999") is False


def test_resolve_reaction_emoji_group_override_false():
    cfg = AdapterConfig(
        reaction_emoji_enabled=True,
        groups={"42": GroupConfig(group_id="42", reaction_emoji_enabled=False).to_dict()},
    )
    assert cfg.resolve_reaction_emoji_enabled("42") is False
    assert cfg.resolve_reaction_emoji_enabled("999") is True


def test_resolve_reaction_emoji_group_none_follows_global():
    cfg = AdapterConfig(
        reaction_emoji_enabled=True,
        groups={"42": GroupConfig(group_id="42", reaction_emoji_enabled=None).to_dict()},
    )
    assert cfg.resolve_reaction_emoji_enabled("42") is True




# ── Parser with config ──────────────────────────────────────────────────


async def test_parser_group_user_blacklist_filter():
    cfg = AdapterConfig(group_require_mention=False,
        groups={"42": GroupConfig(
            group_id="42", group_user_filter_mode="blacklist", group_user_list=["100"]).to_dict()})
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert isinstance(result, DroppedEvent)
    assert result.reason == "user_filter"


async def test_parser_group_user_whitelist_allows_listed():
    cfg = AdapterConfig(group_require_mention=False,
        groups={"42": GroupConfig(
            group_id="42", group_user_filter_mode="whitelist", group_user_list=["100"]).to_dict()})
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None


async def test_parser_group_user_whitelist_empty_rejects_all():
    cfg = AdapterConfig(group_require_mention=False,
        groups={"42": GroupConfig(
            group_id="42", group_user_filter_mode="whitelist", group_user_list=[]).to_dict()})
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert isinstance(result, DroppedEvent)
    assert result.reason == "user_filter"


async def test_parser_group_disabled():
    """Disabled groups are user_filter even with default require-mention and no @."""
    cfg = AdapterConfig(
        group_require_mention=True,
        groups={"42": GroupConfig(group_id="42", enabled=False).to_dict()},
    )
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=True, config=cfg,
    )
    assert isinstance(result, DroppedEvent)
    assert result.reason == "user_filter"


async def test_parser_group_disabled_without_mention_requirement():
    """Disabled groups stay user_filter when every message would otherwise match."""
    cfg = AdapterConfig(
        group_require_mention=False,
        groups={"42": GroupConfig(group_id="42", enabled=False).to_dict()},
    )
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42),
        self_id="999", group_require_mention=False, config=cfg,
    )
    assert isinstance(result, DroppedEvent)
    assert result.reason == "user_filter"


async def test_parser_unmatched_before_user_filter():
    """Trigger miss in an enabled group is leftover traffic (cascade)."""
    cfg = AdapterConfig(
        group_require_mention=True,
        groups={"42": GroupConfig(
            group_id="42", group_user_filter_mode="whitelist", group_user_list=[],
        ).to_dict()},
    )
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=True, config=cfg,
    )
    assert isinstance(result, DroppedEvent)
    assert result.reason == "trigger"


async def test_parser_matched_then_user_filter():
    cfg = AdapterConfig(
        group_require_mention=True,
        groups={"42": GroupConfig(
            group_id="42", group_user_filter_mode="whitelist", group_user_list=[],
        ).to_dict()},
    )
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100, segments=segs),
        self_id="999", group_require_mention=True, config=cfg,
    )
    assert isinstance(result, DroppedEvent)
    assert result.reason == "user_filter"


async def test_parser_group_chat_id():
    """群聊 chat_id 固定为 group:<gid>(Hermes 自己决定是否隔离)。"""
    cfg = AdapterConfig(group_require_mention=False)
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None
    event = result
    assert event.chat_id == "group:42"


async def test_parser_group_custom_prompt_not_in_event():
    """custom_prompt is no longer read by the parser (it's materialized into
    Hermes config.yaml by the WebUI and read via resolve_channel_prompt in
    the plugin).  Verify the field is absent from NormalizedEvent."""
    cfg = AdapterConfig(group_require_mention=False,
        groups={"42": GroupConfig(group_id="42", custom_prompt="你是测试群助手").to_dict()})
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None
    event = result
    assert not hasattr(event, "channel_prompt")


async def test_parser_group_admin():
    cfg = AdapterConfig(group_require_mention=False,
        groups={"42": GroupConfig(group_id="42", admins=["100"]).to_dict()})
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None
    event = result
    assert event.is_admin is True
    assert event.is_global_admin is False


async def test_parser_global_admin_is_distinguished_from_group_admin():
    cfg = AdapterConfig(
        group_require_mention=False,
        global_admins=["100"],
        groups={"42": GroupConfig(group_id="42").to_dict()},
    )
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None
    assert result.is_admin is True
    assert result.is_global_admin is True


async def test_parser_group_not_admin():
    cfg = AdapterConfig(group_require_mention=False,
        groups={"42": GroupConfig(group_id="42", admins=["100"]).to_dict()})
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=200),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None
    event = result
    assert event.is_admin is False


async def test_parser_dm_default_deny_rejects():
    cfg = AdapterConfig()  # default: deny all except whitelist
    result = await parse_event(
        _msg_event("hi", user_id=200),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert isinstance(result, DroppedEvent)
    assert result.reason == "user_filter"


async def test_parser_dm_whitelist_allows_when_denied():
    cfg = AdapterConfig(dm_policy="deny", dm_whitelist=["100"])
    result = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None
    assert not isinstance(result, DroppedEvent)


async def test_parser_dm_allow_unlisted():
    cfg = AdapterConfig(dm_policy="allow", dm_blacklist=["100"])
    result = await parse_event(
        _msg_event("hi", user_id=200),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None
    assert not isinstance(result, DroppedEvent)


async def test_parser_dm_blacklist_blocks_in_allow_mode():
    cfg = AdapterConfig(dm_policy="allow", dm_blacklist=["100"])
    result = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert isinstance(result, DroppedEvent)
    assert result.reason == "user_filter"


async def test_parser_dm_friends_mode_allows_friends_only():
    from unittest.mock import AsyncMock

    cfg = AdapterConfig(dm_policy="friends")
    is_friend = AsyncMock(side_effect=lambda uid: uid == "100")
    allowed = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg, is_friend_fn=is_friend,
    )
    denied = await parse_event(
        _msg_event("hi", user_id=200),
        self_id="999", group_require_mention=False,
        config=cfg, is_friend_fn=is_friend,
    )
    assert allowed is not None
    assert not isinstance(allowed, DroppedEvent)
    assert isinstance(denied, DroppedEvent)
    assert denied.reason == "user_filter"
    is_friend.assert_awaited()


async def test_parser_dm_whitelist_skips_friend_lookup():
    from unittest.mock import AsyncMock

    cfg = AdapterConfig(dm_policy="friends", dm_whitelist=["100"])
    is_friend = AsyncMock(return_value=False)
    result = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg, is_friend_fn=is_friend,
    )
    assert result is not None
    assert not isinstance(result, DroppedEvent)
    is_friend.assert_not_called()


async def test_parser_dm_friends_without_lookup_fn_fail_closed():
    cfg = AdapterConfig(dm_policy="friends")
    result = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert isinstance(result, DroppedEvent)
    assert result.reason == "user_filter"


async def test_parser_dm_reject_reply_deny_and_blacklist():
    deny = AdapterConfig(dm_policy="deny", dm_reject_reply_enabled=True)
    denied = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=deny,
    )
    assert isinstance(denied, FilteredEvent)
    assert denied.filter_type == "dm_policy"
    assert denied.reject_message == "⛔ 当前私聊策略为：禁止私聊"
    assert denied.reply_to_message_id == "1"

    listed = AdapterConfig(
        dm_policy="allow", dm_blacklist=["100"], dm_reject_reply_enabled=True,
    )
    blocked = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=listed,
    )
    assert isinstance(blocked, FilteredEvent)
    assert blocked.reject_message == "⛔ 当前私聊策略为：禁止私聊"


async def test_parser_dm_reject_reply_friends_mode():
    from unittest.mock import AsyncMock

    cfg = AdapterConfig(dm_policy="friends", dm_reject_reply_enabled=True)
    is_friend = AsyncMock(return_value=False)
    result = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg, is_friend_fn=is_friend,
    )
    assert isinstance(result, FilteredEvent)
    assert result.reject_message == "⛔ 当前私聊策略为：仅限好友"


async def test_parser_dm_reject_reply_custom_template():
    cfg = AdapterConfig(
        dm_policy="deny",
        dm_reject_reply_enabled=True,
        dm_reject_message="拒绝：{reason}",
    )
    result = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert isinstance(result, FilteredEvent)
    assert result.reject_message == "拒绝：禁止私聊"


async def test_parser_group_require_mention_override():
    """Group with require_mention=False overrides global True."""
    cfg = AdapterConfig(
        group_require_mention=True,
        groups={"42": GroupConfig(group_id="42", require_mention=False).to_dict()},
    )
    # No @bot mention, but group overrides to not require it
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=True,
        config=cfg,
    )
    assert result is not None


async def test_parser_no_config_fallback():
    """Without config, parse_event should work as before (backward compat)."""
    result = await parse_event(
        _msg_event("hello", user_id=100),
        self_id="999", group_require_mention=True,
    )
    assert result is not None
    event = result
    assert event.chat_id == "100"
    assert event.is_admin is False

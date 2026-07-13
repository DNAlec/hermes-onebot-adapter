"""Tests for GroupConfig, group filtering, session mode, and admin computation."""
from __future__ import annotations

from onebot_adapter.config import AdapterConfig, GroupConfig
from onebot_adapter.onebot.parser import parse_event


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


def test_is_dm_allowed_whitelist_empty_rejects_all():
    cfg = AdapterConfig()
    assert cfg.is_dm_allowed("100") is False


def test_is_dm_allowed_whitelist_allows_listed():
    cfg = AdapterConfig(dm_user_filter_mode="whitelist", dm_user_list=["100"])
    assert cfg.is_dm_allowed("100") is True
    assert cfg.is_dm_allowed("200") is False


def test_is_dm_allowed_blacklist_empty_allows_all():
    cfg = AdapterConfig(dm_user_filter_mode="blacklist", dm_user_list=[])
    assert cfg.is_dm_allowed("100") is True


def test_is_dm_allowed_blacklist_blocks_listed():
    cfg = AdapterConfig(dm_user_filter_mode="blacklist", dm_user_list=["100"])
    assert cfg.is_dm_allowed("100") is False
    assert cfg.is_dm_allowed("200") is True


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





def test_resolve_custom_prompt_global_none():
    cfg = AdapterConfig()
    assert cfg.resolve_custom_prompt("999") is None


def test_resolve_custom_prompt_group():
    cfg = AdapterConfig(groups={"42": GroupConfig(group_id="42", custom_prompt="custom").to_dict()})
    assert cfg.resolve_custom_prompt("42") == "custom"


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
    assert result is None


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
    assert result is None


async def test_parser_group_disabled():
    cfg = AdapterConfig(groups={"42": GroupConfig(group_id="42", enabled=False).to_dict()})
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is None


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


async def test_parser_group_custom_prompt():
    cfg = AdapterConfig(group_require_mention=False,
        groups={"42": GroupConfig(group_id="42", custom_prompt="你是测试群助手").to_dict()})
    result = await parse_event(
        _msg_event("hi", message_type="group", group_id=42, user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None
    event = result
    assert event.channel_prompt == "你是测试群助手"


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


async def test_parser_dm_whitelist_default_rejects():
    cfg = AdapterConfig()  # default: whitelist, empty list → reject all
    result = await parse_event(
        _msg_event("hi", user_id=200),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is None


async def test_parser_dm_whitelist_allows_listed():
    cfg = AdapterConfig(dm_user_filter_mode="whitelist", dm_user_list=["100"])
    result = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None


async def test_parser_dm_blacklist_allows_unlisted():
    cfg = AdapterConfig(dm_user_filter_mode="blacklist", dm_user_list=["100"])
    result = await parse_event(
        _msg_event("hi", user_id=200),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is not None


async def test_parser_dm_blacklist_blocks_listed():
    cfg = AdapterConfig(dm_user_filter_mode="blacklist", dm_user_list=["100"])
    result = await parse_event(
        _msg_event("hi", user_id=100),
        self_id="999", group_require_mention=False,
        config=cfg,
    )
    assert result is None


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
    assert event.channel_prompt is None

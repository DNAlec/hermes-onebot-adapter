"""Tests for the /command filter (config + parser integration)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from onebot_adapter.config import (
    COMMAND_PERM_ADMIN,
    COMMAND_PERM_DISABLED,
    COMMAND_PERM_EVERYONE,
    AdapterConfig,
    GroupConfig,
)
from onebot_adapter.onebot import parser
from onebot_adapter.relay.protocol import FilteredEvent


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


def _group_cmd_event(
    text: str, *, group_id: int = 42, user_id: int = 100, segments: list[dict] | None = None,
    self_id: str = "999", mention: bool = True, message_id: int = 1,
) -> dict[str, Any]:
    """Build a group /command event with @bot mention (satisfies trigger)."""
    if segments is None:
        segs: list[dict] = []
        if mention:
            segs.append({"type": "at", "data": {"qq": self_id}})
        segs.append({"type": "text", "data": {"text": text}})
        segments = segs
    return _msg_event(
        text, message_type="group", group_id=group_id, segments=segments,
        user_id=user_id, message_id=message_id,
    )


# ── Config-level permission checks ──────────────────────────────────────


def test_config_command_filter_disabled_by_default():
    cfg = AdapterConfig()
    assert cfg.command_filter_enabled is False
    assert cfg.command_filter_unknown is False
    assert cfg.command_permissions == {}


def test_config_check_permission_filter_disabled():
    """When filter is disabled, everything is allowed."""
    cfg = AdapterConfig(command_filter_enabled=False)
    allowed, msg = cfg.check_command_permission("42", "100", "kick", is_known=True)
    assert allowed is True
    assert msg is None


def test_config_check_permission_everyone():
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"help": COMMAND_PERM_EVERYONE},
    )
    allowed, msg = cfg.check_command_permission("42", "100", "help", is_known=True)
    assert allowed is True
    assert msg is None


def test_config_check_permission_admin_non_admin_denied():
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_ADMIN},
    )
    allowed, msg = cfg.check_command_permission("42", "100", "kick", is_known=True)
    assert allowed is False
    assert msg is not None
    assert "kick" in msg


def test_config_check_permission_admin_admin_allowed():
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_ADMIN},
        global_admins=["100"],
    )
    allowed, msg = cfg.check_command_permission("42", "100", "kick", is_known=True)
    assert allowed is True
    assert msg is None


def test_config_check_permission_disabled():
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_DISABLED},
    )
    allowed, msg = cfg.check_command_permission("42", "100", "kick", is_known=True)
    assert allowed is False
    assert msg is not None


def test_config_check_permission_unknown_command_default_passthrough():
    cfg = AdapterConfig(command_filter_enabled=True, command_filter_unknown=False)
    allowed, msg = cfg.check_command_permission("42", "100", "foobar", is_known=False)
    assert allowed is True
    assert msg is None


def test_config_check_permission_unknown_command_filter_when_enabled():
    cfg = AdapterConfig(command_filter_enabled=True, command_filter_unknown=True)
    allowed, msg = cfg.check_command_permission("42", "100", "foobar", is_known=False)
    assert allowed is False
    assert msg is not None


def test_config_check_permission_unconfigured_command_passthrough():
    """A known command with no explicit permission config → allowed."""
    cfg = AdapterConfig(command_filter_enabled=True)
    allowed, msg = cfg.check_command_permission("42", "100", "help", is_known=True)
    assert allowed is True
    assert msg is None


def test_config_per_group_override_command_filter_enabled():
    """Group-level command_filter_enabled overrides global."""
    gc = GroupConfig(group_id="42", command_filter_enabled=True)
    cfg = AdapterConfig(command_filter_enabled=False, groups={"42": gc.to_dict()})
    assert cfg.resolve_command_filter_enabled("42") is True
    assert cfg.resolve_command_filter_enabled("99") is False


def test_config_per_group_override_permissions():
    """Group-level command_permissions overrides global for specified commands."""
    gc = GroupConfig(
        group_id="42",
        command_permissions={"kick": COMMAND_PERM_DISABLED},
    )
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_EVERYONE, "help": COMMAND_PERM_EVERYONE},
        groups={"42": gc.to_dict()},
    )
    # Group 42: kick is disabled (group override), help is not in group config → None
    assert cfg.resolve_command_permission("42", "kick") == COMMAND_PERM_DISABLED
    assert cfg.resolve_command_permission("42", "help") is None
    # Global: kick is everyone
    assert cfg.resolve_command_permission("99", "kick") == COMMAND_PERM_EVERYONE


def test_config_per_group_empty_dict_clears_permissions():
    """Group command_permissions={} (empty dict) means no command has a
    configured permission (all None), distinct from null=跟随全局."""
    gc = GroupConfig(group_id="42", command_permissions={})
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_ADMIN},
        groups={"42": gc.to_dict()},
    )
    assert cfg.resolve_command_permission("42", "kick") is None
    assert cfg.resolve_command_permission("99", "kick") == COMMAND_PERM_ADMIN


def test_config_reject_message_template():
    cfg = AdapterConfig(command_reject_message="⛔ /{cmd} 被禁止")
    allowed, msg = cfg.check_command_permission("42", "100", "kick", is_known=True)
    # kick is not configured → allowed
    assert allowed is True
    # Now test with disabled
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_DISABLED},
        command_reject_message="⛔ /{cmd} 被禁止",
    )
    allowed, msg = cfg.check_command_permission("42", "100", "kick", is_known=True)
    assert allowed is False
    assert msg == "⛔ /kick 被禁止"


def test_config_validate_rejects_invalid_permission():
    cfg = AdapterConfig(command_permissions={"kick": "bogus"})
    errors = cfg.validate()
    assert any("command_permissions" in e for e in errors)


def test_config_validate_rejects_invalid_group_permission():
    gc = GroupConfig(group_id="42", command_permissions={"kick": "bogus"})
    cfg = AdapterConfig(groups={"42": gc.to_dict()})
    errors = cfg.validate()
    assert any("group 42 command_permissions" in e for e in errors)


# ── Parser-level command extraction ─────────────────────────────────────


def test_extract_command_name_simple():
    assert parser._extract_command_name([{"type": "text", "data": {"text": "/help"}}]) == "help"


def test_extract_command_name_with_args():
    assert parser._extract_command_name([{"type": "text", "data": {"text": "/kick 123"}}]) == "kick"


def test_extract_command_name_at_bot_suffix():
    """Telegram-style /cmd@BotName → cmd."""
    assert parser._extract_command_name([{"type": "text", "data": {"text": "/approve@MyBot"}}]) == "approve"


def test_extract_command_name_case_lowered():
    assert parser._extract_command_name([{"type": "text", "data": {"text": "/HELP"}}]) == "help"


def test_extract_command_name_not_a_command():
    assert parser._extract_command_name([{"type": "text", "data": {"text": "hello"}}]) is None


def test_extract_command_name_after_at_bot_strip():
    """When @bot segments are stripped, the remaining text starts with /."""
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "/help"}},
    ]
    from onebot_adapter.onebot import segments as seg
    stripped = seg.strip_first_bot_mention(segs, "999")
    assert parser._extract_command_name(stripped) == "help"


def test_extract_command_name_leading_whitespace():
    assert parser._extract_command_name([{"type": "text", "data": {"text": "  /help"}}]) == "help"


def test_extract_command_name_bare_slash():
    assert parser._extract_command_name([{"type": "text", "data": {"text": "/"}}]) is None


def test_extract_command_name_path_like():
    """A token containing / (like a file path) is not a command."""
    assert parser._extract_command_name([{"type": "text", "data": {"text": "/path/to/file"}}]) is None


# ── Parser integration: filter returns FilteredEvent ─────────────────────


async def test_parser_command_filter_disabled_passthrough():
    """command_filter_enabled=False → /command passes through normally."""
    cfg = AdapterConfig(command_filter_enabled=False)
    result = await parser.parse_event(
        _group_cmd_event("/help"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
    )
    assert result is not None
    assert not isinstance(result, FilteredEvent)
    event = result
    assert "/help" in event.text


async def test_parser_command_filter_admin_denied_returns_filtered_event():
    """Non-admin sends /kick with admin-only permission → FilteredEvent."""
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_ADMIN},
    )
    is_known = MagicMock(return_value=True)
    canonical = MagicMock(return_value="kick")
    result = await parser.parse_event(
        _group_cmd_event("/kick 123"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=canonical,
    )
    assert isinstance(result, FilteredEvent)
    assert result.command_name == "kick"
    assert result.chat_id == "group:42"
    assert result.user_id == "100"
    assert result.reject_message


async def test_parser_command_filter_admin_allowed_for_admin():
    """Admin sends /kick with admin-only permission → passes through."""
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_ADMIN},
        global_admins=["100"],
    )
    is_known = MagicMock(return_value=True)
    result = await parser.parse_event(
        _group_cmd_event("/kick 123", user_id=100),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="kick"),
    )
    assert result is not None
    assert not isinstance(result, FilteredEvent)


async def test_parser_command_filter_disabled_command():
    """Disabled command → FilteredEvent for any user."""
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_DISABLED},
        global_admins=["100"],
    )
    is_known = MagicMock(return_value=True)
    result = await parser.parse_event(
        _group_cmd_event("/kick", user_id=100),  # even admin
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="kick"),
    )
    assert isinstance(result, FilteredEvent)


async def test_parser_command_filter_everyone_allowed():
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"help": COMMAND_PERM_EVERYONE},
    )
    is_known = MagicMock(return_value=True)
    result = await parser.parse_event(
        _group_cmd_event("/help"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="help"),
    )
    assert result is not None
    assert not isinstance(result, FilteredEvent)


async def test_parser_command_filter_unknown_passthrough_default():
    """Unknown command (not in registry) → passes through by default."""
    cfg = AdapterConfig(command_filter_enabled=True, command_filter_unknown=False)
    is_known = MagicMock(return_value=False)
    result = await parser.parse_event(
        _group_cmd_event("/foobar"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="foobar"),
    )
    assert result is not None
    assert not isinstance(result, FilteredEvent)


async def test_parser_command_filter_unknown_filtered_when_enabled():
    """Unknown command with command_filter_unknown=True → FilteredEvent."""
    cfg = AdapterConfig(command_filter_enabled=True, command_filter_unknown=True)
    is_known = MagicMock(return_value=False)
    result = await parser.parse_event(
        _group_cmd_event("/foobar"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="foobar"),
    )
    assert isinstance(result, FilteredEvent)


async def test_parser_command_filter_no_is_known_fn_treats_as_unknown():
    """Without is_known_command_fn, commands are treated as unknown."""
    cfg = AdapterConfig(command_filter_enabled=True, command_filter_unknown=True)
    result = await parser.parse_event(
        _group_cmd_event("/foobar"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
    )
    assert isinstance(result, FilteredEvent)


async def test_parser_command_filter_non_command_passthrough():
    """Non-/command messages are not affected by the filter."""
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_DISABLED},
    )
    result = await parser.parse_event(
        _group_cmd_event("hello world"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=MagicMock(return_value=True),
    )
    assert result is not None
    assert not isinstance(result, FilteredEvent)


async def test_parser_command_filter_dm_also_applies():
    """Command filter applies to DM (private chat) too, using global config."""
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_DISABLED},
        dm_user_filter_mode="blacklist",  # allow all DMs for this test
    )
    is_known = MagicMock(return_value=True)
    result = await parser.parse_event(
        _msg_event("/kick", message_type="private", user_id=100),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="kick"),
    )
    assert isinstance(result, FilteredEvent)
    assert result.chat_type == "dm"
    assert result.chat_id == "100"


async def test_parser_command_filter_per_group_override():
    """Group-level command_filter_enabled=True overrides global False."""
    gc = GroupConfig(group_id="42", command_filter_enabled=True,
                     command_permissions={"kick": COMMAND_PERM_DISABLED})
    cfg = AdapterConfig(command_filter_enabled=False, groups={"42": gc.to_dict()})
    is_known = MagicMock(return_value=True)
    result = await parser.parse_event(
        _group_cmd_event("/kick"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="kick"),
    )
    assert isinstance(result, FilteredEvent)


async def test_parser_command_filter_works_with_non_text_segments():
    """Filtered commands should return FilteredEvent regardless of segment types.

    The filter gating runs before message expansion (forward/reply) and should
    short-circuit for any segment, including rich-media / at segments.
    """
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_DISABLED},
    )
    is_known = MagicMock(return_value=True)
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "/kick"}},
        {"type": "image", "data": {"url": "http://example.com/x.png"}},
    ]
    result = await parser.parse_event(
        _group_cmd_event("/kick", segments=segs),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="kick"),
    )
    # Should be FilteredEvent, not a tuple with media
    assert isinstance(result, FilteredEvent)


async def test_parser_command_filter_reject_message_includes_cmd():
    """The reject message should have {cmd} replaced."""
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_DISABLED},
        command_reject_message="⛔ /{cmd} 被禁止",
    )
    is_known = MagicMock(return_value=True)
    result = await parser.parse_event(
        _group_cmd_event("/kick"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="kick"),
    )
    assert isinstance(result, FilteredEvent)
    assert result.reject_message == "⛔ /kick 被禁止"


async def test_parser_command_filter_alias_resolved():
    """When the command is an alias, canonical_command_name_fn resolves it
    and the permission check uses the canonical name."""
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"background": COMMAND_PERM_ADMIN},
    )
    is_known = MagicMock(return_value=True)  # "bg" is known (alias)
    canonical = MagicMock(return_value="background")  # "bg" → "background"
    result = await parser.parse_event(
        _group_cmd_event("/bg"),
        self_id="999",
        group_require_mention=True,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=canonical,
    )
    assert isinstance(result, FilteredEvent)
    assert result.command_name == "background"


async def test_parser_command_filter_works_without_mention_requirement():
    """Command filter works when group_require_mention=False (no @bot needed)."""
    cfg = AdapterConfig(
        command_filter_enabled=True,
        command_permissions={"kick": COMMAND_PERM_DISABLED},
        group_require_mention=False,
    )
    is_known = MagicMock(return_value=True)
    result = await parser.parse_event(
        _group_cmd_event("/kick", mention=False),
        self_id="999",
        group_require_mention=False,
        config=cfg,
        is_known_command_fn=is_known,
        canonical_command_name_fn=MagicMock(return_value="kick"),
    )
    assert isinstance(result, FilteredEvent)


# ── Relay protocol: FilteredEvent serialization ─────────────────────────


def test_filtered_event_fields():
    fe = FilteredEvent(
        chat_id="group:42",
        chat_type="group",
        user_id="100",
        user_name="Tester",
        command_name="kick",
        reject_message="⛔ no permission",
        message_id="99",
        timestamp=1700000000.0,
    )
    assert fe.chat_id == "group:42"
    assert fe.command_name == "kick"
    assert fe.reject_message == "⛔ no permission"
    assert fe.chat_type == "group"
    assert fe.message_id == "99"


def test_filtered_event_defaults():
    fe = FilteredEvent(
        chat_id="group:42",
        chat_type="group",
        user_id="100",
        user_name="Tester",
        command_name="kick",
        reject_message="⛔ no",
    )
    assert fe.message_id == ""
    assert fe.reply_to_message_id is None
    assert fe.timestamp == 0.0


# ── Relay: commands_snapshot storage & lookup ───────────────────────────


def test_relay_store_commands():
    from onebot_adapter.relay.hermes_ws import HermesRelayServer

    relay = HermesRelayServer(
        config=AdapterConfig(),
        api=MagicMock(),
        adapter_version="test",
        onebot_connected_fn=lambda: False,
    )
    commands = [
        {"name": "help", "description": "Show help", "source": "builtin",
         "aliases": ["h", "?"], "args_hint": ""},
        {"name": "kick", "description": "Kick user", "source": "builtin",
         "aliases": [], "args_hint": "<user>"},
        {"name": "my-tool", "description": "Custom", "source": "my_plugin",
         "aliases": [], "args_hint": ""},
    ]
    relay._store_commands(commands)
    assert relay.is_known_command("help")
    assert relay.is_known_command("kick")
    assert relay.is_known_command("my-tool")
    # Aliases
    assert relay.is_known_command("h")
    assert relay.is_known_command("?")
    # Canonical resolution
    assert relay.canonical_command_name("h") == "help"
    assert relay.canonical_command_name("?") == "help"
    assert relay.canonical_command_name("kick") == "kick"
    # Unknown
    assert not relay.is_known_command("foobar")
    assert relay.canonical_command_name("foobar") == "foobar"
    # Commands list
    names = [c["name"] for c in relay.commands]
    assert "help" in names
    assert "kick" in names


def test_relay_store_commands_case_insensitive():
    from onebot_adapter.relay.hermes_ws import HermesRelayServer

    relay = HermesRelayServer(
        config=AdapterConfig(),
        api=MagicMock(),
        adapter_version="test",
        onebot_connected_fn=lambda: False,
    )
    relay._store_commands([{"name": "Help", "aliases": ["H"], "source": "builtin"}])
    assert relay.is_known_command("help")
    assert relay.is_known_command("h")


def test_relay_store_empty_commands_clears_previous():
    from onebot_adapter.relay.hermes_ws import HermesRelayServer

    relay = HermesRelayServer(
        config=AdapterConfig(),
        api=MagicMock(),
        adapter_version="test",
        onebot_connected_fn=lambda: False,
    )
    relay._store_commands([{"name": "help", "source": "builtin", "aliases": []}])
    assert relay.is_known_command("help")
    # Push empty snapshot
    relay._store_commands([])
    assert not relay.is_known_command("help")

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from onebot_adapter.app import AdapterService
from onebot_adapter.bot_blacklist import BotBlacklistStore, format_duration
from onebot_adapter.config import AdapterConfig, ConfigStore, GroupConfig
from onebot_adapter.onebot.handler import OneBotHandler
from onebot_adapter.onebot.parser import parse_event
from onebot_adapter.relay.protocol import FilteredEvent, NormalizedEvent


def _store(tmp_path):
    store = BotBlacklistStore(tmp_path / "bot_blacklist.sqlite3")
    store.start()
    return store


def _group_event(text: str, *, mention: bool = True, user_id: int = 100) -> dict:
    segments = []
    if mention:
        segments.append({"type": "at", "data": {"qq": "999"}})
    segments.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message", "message_type": "group", "group_id": 42,
        "user_id": user_id, "message_id": 8, "time": 1700000000,
        "sender": {"nickname": "Tester"}, "message": segments,
    }


def _dm_event(user_id: int = 100) -> dict:
    return {
        "post_type": "message", "message_type": "private", "user_id": user_id,
        "message_id": 9, "time": 1700000000, "sender": {"nickname": "Tester"},
        "message": [{"type": "text", "data": {"text": "hello"}}],
    }


def _poke_event(*, user_id: int = 100, group_id: int | None = None) -> dict:
    event = {
        "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
        "user_id": user_id, "target_id": 999, "time": 1700000000,
    }
    if group_id is not None:
        event["group_id"] = group_id
    return event


def test_store_scopes_upsert_match_expiry_and_persistence(tmp_path):
    store = _store(tmp_path)
    group = store.set(
        scope="group", group_id="42", user_id="100", duration_seconds=60,
        reason="group", created_by_user_id="1", now=1000,
    )
    assert store.match(user_id="100", group_id="42", now=1010) == group
    assert store.match(user_id="100", group_id="43", now=1010) is None

    global_entry = store.set(
        scope="global", user_id="100", duration_seconds=120,
        reason="global", created_by_user_id="2", now=1000,
    )
    assert store.match(user_id="100", group_id="42", now=1010) == global_entry
    assert store.match(user_id="100", group_id=None, now=1010) == global_entry
    updated = store.set(
        scope="global", user_id="100", duration_seconds=30,
        reason="updated", created_by_user_id="3", now=1100,
    )
    assert updated.id == global_entry.id
    assert updated.reason == "updated"
    assert len(store.list(user_id="100", now=1101)) == 1
    assert store.match(user_id="100", group_id=None, now=1131) is None
    store.close()

    reopened = BotBlacklistStore(tmp_path / "bot_blacklist.sqlite3")
    reopened.start()
    assert reopened.list(now=2000) == []
    reopened.close()


def test_store_dm_remove_clamp_and_format(tmp_path):
    store = _store(tmp_path)
    entry = store.set(
        scope="dm", user_id="100", duration_seconds=7200,
        reason="dm", created_by_user_id="1", now=1000,
    )
    assert store.match(user_id="100", group_id=None, now=1001) == entry
    assert store.match(user_id="100", group_id="42", now=1001) is None
    assert store.clamp(60, now=1010) == 1
    assert store.match(user_id="100", group_id=None, now=1069) is not None
    assert store.match(user_id="100", group_id=None, now=1071) is None
    assert store.remove(scope="dm", user_id="100") is False
    assert format_duration(3661) == "1小时1分钟"
    store.close()


async def test_parser_blacklist_after_group_trigger_and_before_delivery(tmp_path):
    store = _store(tmp_path)
    store.set(
        scope="group", group_id="42", user_id="100", duration_seconds=3600,
        reason="刷屏", created_by_user_id="200",
    )
    cfg = AdapterConfig(
        onebot_ws_token="x", hermes_ws_token="y", dm_user_filter_mode="blacklist",
        bot_blacklist_reject_message="blocked {user_id} {remaining} {reason}",
    )
    def match(uid, gid):
        return store.match(user_id=uid, group_id=gid)
    ignored = await parse_event(
        _group_event("ordinary", mention=False), self_id="999", group_require_mention=True,
        config=cfg, bot_blacklist_match_fn=match,
    )
    assert ignored is None
    blocked = await parse_event(
        _group_event("hello"), self_id="999", group_require_mention=True,
        config=cfg, bot_blacklist_match_fn=match,
    )
    assert isinstance(blocked, FilteredEvent)
    assert blocked.filter_type == "bot_blacklist"
    assert blocked.reject_message.startswith("blocked 100 ")
    assert blocked.reject_message.endswith(" 刷屏")
    store.close()


async def test_parser_global_scope_dm_and_admin_exemption(tmp_path):
    store = _store(tmp_path)
    store.set(
        scope="global", user_id="100", duration_seconds=3600,
        reason="global", created_by_user_id="200",
    )
    def match(uid, gid):
        return store.match(user_id=uid, group_id=gid)
    cfg = AdapterConfig(
        onebot_ws_token="x", hermes_ws_token="y", dm_user_filter_mode="blacklist",
    )
    blocked = await parse_event(
        _dm_event(), self_id="999", group_require_mention=True,
        config=cfg, bot_blacklist_match_fn=match,
    )
    assert isinstance(blocked, FilteredEvent)

    cfg.global_admins = ["100"]
    allowed = await parse_event(
        _dm_event(), self_id="999", group_require_mention=True,
        config=cfg, bot_blacklist_match_fn=match,
    )
    assert allowed is not None and not isinstance(allowed, FilteredEvent)

    cfg.global_admins = []
    cfg.groups = {"42": GroupConfig(group_id="42", admins=["100"]).to_dict()}
    group_allowed = await parse_event(
        _group_event("hello"), self_id="999", group_require_mention=True,
        config=cfg, bot_blacklist_match_fn=match,
    )
    assert group_allowed is not None and not isinstance(group_allowed, FilteredEvent)
    store.close()


async def test_service_local_action_clamps_audits_and_protects_admins(tmp_path):
    cfg = AdapterConfig(
        onebot_ws_token="x", hermes_ws_token="y", bot_blacklist_max_duration_seconds=60,
        global_admins=["900"],
        groups={"42": GroupConfig(group_id="42", admins=["901"]).to_dict()},
    )
    service = AdapterService(ConfigStore(cfg))
    service._bot_blacklist = _store(tmp_path)
    result = await service._handle_local_api_call("adapter_edit_bot_blacklist", {
        "operation": "set", "scope": "group", "group_id": "42", "user_id": "100",
        "duration_seconds": 120, "reason": "刷屏", "created_by_user_id": "200",
    })
    assert result["clamped"] is True
    assert result["actual_duration_seconds"] == 60
    assert result["created_by_user_id"] == "200"
    assert service._bot_blacklist.match(user_id="100", group_id="42") is not None

    with pytest.raises(PermissionError):
        await service._handle_local_api_call("adapter_edit_bot_blacklist", {
            "operation": "set", "scope": "global", "user_id": "900",
            "duration_seconds": 10, "reason": "no",
        })
    with pytest.raises(PermissionError):
        await service._handle_local_api_call("adapter_edit_bot_blacklist", {
            "operation": "set", "scope": "group", "group_id": "42", "user_id": "901",
            "duration_seconds": 10, "reason": "no",
        })
    service.store.patch(bot_blacklist_enabled=False)
    with pytest.raises(RuntimeError, match="已关闭"):
        await service._handle_local_api_call("adapter_get_bot_blacklist", {})
    service._bot_blacklist.close()


@pytest.mark.parametrize(
    ("scope", "group_id", "event_group_id"),
    [("group", "42", 42), ("dm", "", None), ("global", "", 42)],
)
async def test_poke_dynamic_blacklist_is_filtered(tmp_path, scope, group_id, event_group_id):
    store = _store(tmp_path)
    store.set(
        scope=scope, group_id=group_id, user_id="100", duration_seconds=3600,
        reason="反复戳一戳", created_by_user_id="200",
    )
    cfg = AdapterConfig(
        onebot_ws_token="x", hermes_ws_token="y", self_id="999", notify_poke_enabled=True,
        dm_user_filter_mode="blacklist",
    )

    def match(uid, gid):
        return store.match(user_id=uid, group_id=gid)

    result = await parse_event(
        _poke_event(group_id=event_group_id), self_id="999", group_require_mention=True,
        config=cfg, bot_blacklist_match_fn=match,
    )
    assert isinstance(result, FilteredEvent)
    assert result.filter_type == "bot_blacklist"
    assert result.chat_id == ("group:42" if event_group_id else "100")
    assert result.reply_to_message_id is None
    assert "反复戳一戳" in result.reject_message
    store.close()


async def test_poke_blacklist_admin_disabled_and_expired_are_allowed(tmp_path):
    store = _store(tmp_path)
    store.set(
        scope="group", group_id="42", user_id="100", duration_seconds=3600,
        reason="active", created_by_user_id="200",
    )

    def match(uid, gid):
        return store.match(user_id=uid, group_id=gid)

    cfg = AdapterConfig(
        onebot_ws_token="x", hermes_ws_token="y", notify_poke_enabled=True,
        groups={"42": GroupConfig(group_id="42", admins=["100"]).to_dict()},
    )
    admin_result = await parse_event(
        _poke_event(group_id=42), self_id="999", group_require_mention=True,
        config=cfg, bot_blacklist_match_fn=match,
    )
    assert isinstance(admin_result, NormalizedEvent)

    cfg.groups = {}
    cfg.bot_blacklist_enabled = False
    disabled_result = await parse_event(
        _poke_event(group_id=42), self_id="999", group_require_mention=True,
        config=cfg, bot_blacklist_match_fn=match,
    )
    assert isinstance(disabled_result, NormalizedEvent)

    store.set(
        scope="group", group_id="42", user_id="100", duration_seconds=1,
        reason="expired", created_by_user_id="200", now=1,
    )
    cfg.bot_blacklist_enabled = True
    expired_result = await parse_event(
        _poke_event(group_id=42), self_id="999", group_require_mention=True,
        config=cfg, bot_blacklist_match_fn=match,
    )
    assert isinstance(expired_result, NormalizedEvent)
    store.close()


async def test_member_notice_ignores_dynamic_blacklist(tmp_path):
    store = _store(tmp_path)
    store.set(
        scope="global", user_id="100", duration_seconds=3600,
        reason="blocked", created_by_user_id="200",
    )
    cfg = AdapterConfig(
        onebot_ws_token="x", hermes_ws_token="y", notify_member_change_enabled=True,
    )

    def match(uid, gid):
        return store.match(user_id=uid, group_id=gid)

    result = await parse_event(
        {
            "post_type": "notice", "notice_type": "group_increase", "sub_type": "approve",
            "user_id": 100, "group_id": 42, "time": 1700000000,
        },
        self_id="999", group_require_mention=True, config=cfg, bot_blacklist_match_fn=match,
    )
    assert isinstance(result, NormalizedEvent)
    assert result.is_system_notice is True
    store.close()


async def test_handler_dispatches_blocked_poke_to_filtered_callback(tmp_path):
    store = _store(tmp_path)
    store.set(
        scope="dm", user_id="100", duration_seconds=3600,
        reason="blocked", created_by_user_id="200",
    )
    cfg = AdapterConfig(
        onebot_ws_token="x", hermes_ws_token="y", self_id="999", notify_poke_enabled=True,
        dm_user_filter_mode="blacklist",
    )

    def match(uid, gid):
        return store.match(user_id=uid, group_id=gid)

    on_event = AsyncMock()
    on_filtered = AsyncMock()
    handler = OneBotHandler(
        label="test", config=cfg, api=MagicMock(), on_event=on_event,
        on_filtered=on_filtered, bot_blacklist_match_fn=match,
    )
    await handler.handle_text(json.dumps(_poke_event()))
    on_filtered.assert_awaited_once()
    assert on_filtered.await_args.args[0].filter_type == "bot_blacklist"
    on_event.assert_not_awaited()
    store.close()

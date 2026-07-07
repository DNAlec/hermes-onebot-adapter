"""Tests for message delivery reaction emoji (_maybe_react_delivered_* in app.py)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore, GroupConfig
from onebot_adapter.relay.protocol import NormalizedEvent


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        onebot_reverse_ws_port=28860,
        hermes_ws_port=28861,
        webui_port=28862,
        self_id="123",
        hermes_ws_token="reacttest",
        onebot_ws_token="obtest",
        onebot_mode="reverse",
    ))
    svc = AdapterService(store)
    svc._session = aiohttp.ClientSession()
    svc._init_components()
    yield svc
    if svc._onebot_forward:
        await svc._onebot_forward.stop()
    if svc._session and not svc._session.closed:
        await svc._session.close()


def _group_event(msg_id: str = "100", chat_id: str = "group:42") -> NormalizedEvent:
    return NormalizedEvent(
        message_id=msg_id,
        chat_id=chat_id,
        chat_type="group",
        user_id="u1",
        user_name="Alice",
        text="hello",
    )


def _dm_event(msg_id: str = "100", chat_id: str = "100") -> NormalizedEvent:
    return NormalizedEvent(
        message_id=msg_id,
        chat_id=chat_id,
        chat_type="dm",
        user_id="100",
        user_name="Alice",
        text="hello",
    )


# ── disabled / gating ────────────────────────────────────────────────────


async def test_disabled_no_call(service):
    service._api.call = AsyncMock()
    await service._maybe_react_delivered(_group_event())
    service._api.call.assert_not_called()


async def test_no_clients_no_call(service):
    service.store.patch(reaction_emoji_enabled=True)
    service._relay._clients = {}  # no plugin connected
    service._api.call = AsyncMock()
    await service._maybe_react_delivered(_group_event())
    service._api.call.assert_not_called()


async def test_bad_message_id_no_call(service):
    service.store.patch(reaction_emoji_enabled=True)
    service._api.call = AsyncMock()
    await service._maybe_react_delivered(_group_event(msg_id="not-a-number"))
    service._api.call.assert_not_called()


async def test_api_failure_swallows_exception(service):
    service.store.patch(reaction_emoji_enabled=True)
    service._relay._clients = {MagicMock(): MagicMock()}  # has_clients → True
    service._api.call = AsyncMock(side_effect=RuntimeError("boom"))
    # Should not raise
    await service._maybe_react_delivered(_group_event())
    service._api.call.assert_called_once()


# ── enabled group ────────────────────────────────────────────────────────


async def test_enabled_group_calls_api(service):
    service.store.patch(reaction_emoji_enabled=True, reaction_emoji_id="76")
    service._relay._clients = {MagicMock(): MagicMock()}  # has_clients → True
    service._api.call = AsyncMock(return_value={"retcode": 0, "data": {}})
    await service._maybe_react_delivered(_group_event(msg_id="100", chat_id="group:42"))
    service._api.call.assert_called_once()
    action, params = service._api.call.call_args.args
    assert action == "set_msg_emoji_like"
    assert params == {"message_id": 100, "emoji_id": "76", "group_id": 42}


async def test_enabled_dm_calls_api(service):
    service.store.patch(reaction_emoji_enabled=True, reaction_emoji_id="76")
    service._relay._clients = {MagicMock(): MagicMock()}  # has_clients → True
    service._api.call = AsyncMock(return_value={"retcode": 0, "data": {}})
    await service._maybe_react_delivered(_dm_event(msg_id="100", chat_id="100"))
    service._api.call.assert_called_once()
    action, params = service._api.call.call_args.args
    assert action == "set_msg_emoji_like"
    assert params == {"message_id": 100, "emoji_id": "76", "user_id": 100}


# ── per-group override ──────────────────────────────────────────────────


async def test_group_override_false_blocks(service):
    service.store.patch(
        reaction_emoji_enabled=True,
        reaction_emoji_id="76",
        groups={"42": GroupConfig(group_id="42", reaction_emoji_enabled=False).to_dict()},
    )
    service._relay._clients = {MagicMock(): MagicMock()}
    service._api.call = AsyncMock()
    await service._maybe_react_delivered(_group_event(chat_id="group:42"))
    service._api.call.assert_not_called()


async def test_group_override_true_enables_when_global_off(service):
    service.store.patch(
        reaction_emoji_enabled=False,
        reaction_emoji_id="76",
        groups={"42": GroupConfig(group_id="42", reaction_emoji_enabled=True).to_dict()},
    )
    service._relay._clients = {MagicMock(): MagicMock()}
    service._api.call = AsyncMock(return_value={"retcode": 0, "data": {}})
    await service._maybe_react_delivered(_group_event(chat_id="group:42"))
    service._api.call.assert_called_once()


async def test_group_none_follows_global(service):
    service.store.patch(
        reaction_emoji_enabled=True,
        reaction_emoji_id="76",
        groups={"42": GroupConfig(group_id="42", reaction_emoji_enabled=None).to_dict()},
    )
    service._relay._clients = {MagicMock(): MagicMock()}
    service._api.call = AsyncMock(return_value={"retcode": 0, "data": {}})
    await service._maybe_react_delivered(_group_event(chat_id="group:42"))
    service._api.call.assert_called_once()


# ── queued reaction (_maybe_react_queued) ───────────────────────────────


async def test_queued_empty_id_no_call(service):
    """reaction_emoji_id_queued 为空时不贴表情。"""
    service.store.patch(reaction_emoji_enabled=True, reaction_emoji_id_queued="")
    service._relay._clients = {MagicMock(): MagicMock()}
    service._api.call = AsyncMock()
    await service._maybe_react_queued(_group_event())
    service._api.call.assert_not_called()


async def test_queued_disabled_no_call(service):
    """reaction_emoji_enabled=False 时不贴排队表情。"""
    service.store.patch(reaction_emoji_enabled=False, reaction_emoji_id_queued="⏳")
    service._relay._clients = {MagicMock(): MagicMock()}
    service._api.call = AsyncMock()
    await service._maybe_react_queued(_group_event())
    service._api.call.assert_not_called()


async def test_queued_no_clients_no_call(service):
    service.store.patch(reaction_emoji_enabled=True, reaction_emoji_id_queued="⏳")
    service._relay._clients = {}
    service._api.call = AsyncMock()
    await service._maybe_react_queued(_group_event())
    service._api.call.assert_not_called()


async def test_queued_enabled_group_calls_api(service):
    service.store.patch(reaction_emoji_enabled=True, reaction_emoji_id_queued="⏳")
    service._relay._clients = {MagicMock(): MagicMock()}
    service._api.call = AsyncMock(return_value={"retcode": 0, "data": {}})
    await service._maybe_react_queued(_group_event(msg_id="100", chat_id="group:42"))
    service._api.call.assert_called_once()
    action, params = service._api.call.call_args.args
    assert action == "set_msg_emoji_like"
    assert params == {"message_id": 100, "emoji_id": "⏳", "group_id": 42}


async def test_queued_enabled_dm_calls_api(service):
    service.store.patch(reaction_emoji_enabled=True, reaction_emoji_id_queued="⏳")
    service._relay._clients = {MagicMock(): MagicMock()}
    service._api.call = AsyncMock(return_value={"retcode": 0, "data": {}})
    await service._maybe_react_queued(_dm_event(msg_id="100", chat_id="100"))
    service._api.call.assert_called_once()
    action, params = service._api.call.call_args.args
    assert action == "set_msg_emoji_like"
    assert params == {"message_id": 100, "emoji_id": "⏳", "user_id": 100}


async def test_queued_api_failure_swallows_exception(service):
    service.store.patch(reaction_emoji_enabled=True, reaction_emoji_id_queued="⏳")
    service._relay._clients = {MagicMock(): MagicMock()}
    service._api.call = AsyncMock(side_effect=RuntimeError("boom"))
    await service._maybe_react_queued(_group_event())
    service._api.call.assert_called_once()


async def test_queued_group_override_false_blocks(service):
    service.store.patch(
        reaction_emoji_enabled=True,
        reaction_emoji_id_queued="⏳",
        groups={"42": GroupConfig(group_id="42", reaction_emoji_enabled=False).to_dict()},
    )
    service._relay._clients = {MagicMock(): MagicMock()}
    service._api.call = AsyncMock()
    await service._maybe_react_queued(_group_event(chat_id="group:42"))
    service._api.call.assert_not_called()

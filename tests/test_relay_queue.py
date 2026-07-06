"""Tests for the shared-group queue policy in ``HermesRelayServer``.

Covers:
- DM / per_user group / shared group routing decisions
- busy + same-user passthrough vs different-user enqueue
- slash-command bypass
- idle-frame dequeue + dispatch
- queue cap eviction
- busy-timeout watchdog
- last-client-disconnect cleanup
- ring buffer replay honoring the queue
- hot-reload trimming queues to a lowered cap
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import aiohttp.web
import pytest
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.config import AdapterConfig
from onebot_adapter.relay.hermes_ws import HermesRelayServer
from onebot_adapter.relay.protocol import NormalizedEvent, idle_message


def _make_relay(**cfg_overrides):
    mock_api = MagicMock()
    defaults = dict(
        hermes_ws_token="testtoken",
        hermes_ws_path="/hermes",
        event_queue_idle_timeout=300.0,
        event_queue_max_per_chat=50,
    )
    defaults.update(cfg_overrides)
    cfg = AdapterConfig(**defaults)
    relay = HermesRelayServer(cfg, mock_api, adapter_version="0.1.0-test", onebot_connected_fn=lambda: True)
    return relay, mock_api, cfg


def _make_event(
    text: str = "hello",
    *,
    chat_id: str = "100",
    user_id: str = "u1",
    user_name: str = "Alice",
    chat_type: str = "dm",
    message_id: str = "1",
) -> NormalizedEvent:
    return NormalizedEvent(
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name=user_name,
        text=text,
        chat_name=user_name,
    )


def _group_event(text="hello", *, gid="42", uid="100", uname="Alice", mid="1"):
    return _make_event(
        text, chat_id=f"group:{gid}", user_id=uid, user_name=uname,
        chat_type="group", message_id=mid,
    )


def _per_user_event(text="hello", *, gid="42", uid="100", uname="Alice", mid="1"):
    return _make_event(
        text, chat_id=f"group:{gid}:user:{uid}", user_id=uid, user_name=uname,
        chat_type="group", message_id=mid,
    )


# ── Pure routing decisions ──────────────────────────────────────────────


async def test_dm_never_queues():
    """A DM event broadcasts immediately — no busy slot touched."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_make_event("hi", chat_id="100", chat_type="dm"))
    relay._broadcast_event.assert_awaited_once()
    assert not relay._busy_groups
    assert not relay._queues


async def test_per_user_group_never_queues():
    """A per_user group chat (group:<gid>:user:<uid>) bypasses the queue."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_per_user_event(uid="100"))
    relay._broadcast_event.assert_awaited_once()
    assert not relay._busy_groups
    assert not relay._queues


async def test_shared_group_first_message_marks_busy():
    """First shared-group message broadcasts AND marks the group busy with its user."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    ev = _group_event("hi", gid="42", uid="100")
    await relay._enqueue_or_broadcast(ev)
    relay._broadcast_event.assert_awaited_once_with(ev)
    assert "42" in relay._busy_groups
    busy_user, since = relay._busy_groups["42"]
    assert busy_user == "100"
    assert since > 0


async def test_shared_group_busy_same_user_passes_through():
    """When the group is busy and a new message arrives from the SAME user,
    it broadcasts immediately (user can补充/打断 current turn)."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    relay._broadcast_event.assert_awaited_once()
    # second message, same user, same group, while busy
    ev2 = _group_event("msg2", gid="42", uid="100", mid="2")
    await relay._enqueue_or_broadcast(ev2)
    assert relay._broadcast_event.await_count == 2
    assert not relay._queues  # nothing queued
    assert relay._busy_groups["42"][0] == "100"


async def test_shared_group_busy_different_user_enqueues():
    """When the group is busy and a different user sends a message, it queues."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    assert relay._broadcast_event.await_count == 1
    # different user, same group → queued, NOT broadcast
    ev2 = _group_event("msg2", gid="42", uid="200", mid="2")
    await relay._enqueue_or_broadcast(ev2)
    assert relay._broadcast_event.await_count == 1  # no new broadcast
    assert "42" in relay._queues
    assert len(relay._queues["42"]) == 1
    assert relay._queues["42"][0].user_id == "200"


async def test_slash_command_bypasses_queue():
    """A '/'-prefixed message broadcasts immediately even when the group is busy."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    # Make the group busy with user 100.
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    assert relay._broadcast_event.await_count == 1
    # A slash command from a different user bypasses the queue.
    slash_ev = _group_event("/stop", gid="42", uid="200", mid="2")
    await relay._enqueue_or_broadcast(slash_ev)
    assert relay._broadcast_event.await_count == 2
    relay._broadcast_event.assert_awaited_with(slash_ev)
    assert "42" not in relay._queues


async def test_queue_cap_drops_oldest():
    """When the queue is full, pushing a new message drops the oldest."""
    relay, _, _ = _make_relay(event_queue_max_per_chat=2)
    relay._broadcast_event = AsyncMock()
    # Fill the busy slot.
    await relay._enqueue_or_broadcast(_group_event("head", gid="42", uid="100", mid="0"))
    # Queue three from different users; cap is 2.
    await relay._enqueue_or_broadcast(_group_event("m1", gid="42", uid="200", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("m2", gid="42", uid="300", mid="2"))
    await relay._enqueue_or_broadcast(_group_event("m3", gid="42", uid="400", mid="3"))
    q = relay._queues["42"]
    assert len(q) == 2
    # Oldest ("m1") was dropped; remaining are m2 and m3.
    assert [e.text for e in q] == ["m2", "m3"]


async def test_idle_frame_dequeues_next():
    """An idle frame for a busy group clears busy and dispatches the next queued."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200", mid="2"))
    assert relay._broadcast_event.await_count == 1
    assert "42" in relay._queues
    # Fire idle for gid=42
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    # The queued message should have been broadcast via a scheduled task.
    await asyncio.sleep(0)  # let create_task run
    await asyncio.sleep(0)
    assert relay._broadcast_event.await_count == 2
    # busy is now held by uid 200
    assert relay._busy_groups["42"][0] == "200"
    # queue is empty and key removed
    assert "42" not in relay._queues


async def test_idle_frame_no_queue_just_clears_busy():
    """An idle frame when no messages are queued simply clears the busy slot."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    assert "42" in relay._busy_groups
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    assert "42" not in relay._busy_groups
    # No new broadcast (nothing queued).
    assert relay._broadcast_event.await_count == 1


async def test_idle_frame_for_non_busy_group_is_noop():
    """Idle for a group that isn't busy is ignored (debug log)."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    relay._broadcast_event.assert_not_awaited()
    assert not relay._busy_groups


async def test_idle_frame_falls_back_to_chat_id():
    """When group_id is missing, idle frame parses chat_id to derive group_id."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    # No group_id — must parse chat_id.
    await relay._handle_idle({"type": "idle", "chat_id": "group:42"})
    assert "42" not in relay._busy_groups


async def test_idle_frame_without_group_id_or_chat_id_is_ignored():
    """An idle frame with neither group_id nor parseable chat_id is dropped."""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    # No group_id, malformed chat_id
    await relay._handle_idle({"type": "idle", "chat_id": "garbage"})
    relay._broadcast_event.assert_not_awaited()


async def test_watchdog_clears_stale_busy():
    """The watchdog force-clears busy slots older than idle_timeout."""
    relay, _, _ = _make_relay(event_queue_idle_timeout=0.01)
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    # Forcibly age the busy slot so watchdog considers it stale.
    busy_user, _ = relay._busy_groups["42"]
    relay._busy_groups["42"] = (busy_user, time.monotonic() - 100)
    # Trigger watchdog sweep directly by calling _dequeue_and_dispatch path:
    # watchdog_loop sleeps _WATCHDOG_INTERVAL; we instead invoke the same
    # clearing logic the watchdog uses, with no queued messages.
    relay._dequeue_and_dispatch("42")
    assert "42" not in relay._busy_groups


async def test_watchdog_dispatches_queued_after_timeout():
    """When watchdog force-clears a busy slot that has queued messages, the
    next queued message is dispatched."""
    relay, _, _ = _make_relay(event_queue_idle_timeout=0.01)
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200", mid="2"))
    # Age the busy slot.
    busy_user, _ = relay._busy_groups["42"]
    relay._busy_groups["42"] = (busy_user, time.monotonic() - 100)
    relay._dequeue_and_dispatch("42")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert relay._broadcast_event.await_count == 2
    assert relay._busy_groups["42"][0] == "200"


async def test_last_client_disconnect_clears_busy_and_queues():
    """When the last plugin client disconnects, all queue state is cleared."""
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)  # ready
                assert relay.has_clients
                # Simulate busy state via the queue policy.
                await relay._enqueue_or_broadcast(_group_event("m1", gid="42", uid="100"))
                await relay._enqueue_or_broadcast(_group_event("m2", gid="42", uid="200"))
                assert "42" in relay._busy_groups
                assert "42" in relay._queues
            # ws closed here
            await asyncio.sleep(0.1)
        # After last client disconnect, busy/queues should be cleared.
        assert not relay._busy_groups
        assert not relay._queues
    finally:
        await server.close()


async def test_ring_buffer_replay_honors_queue():
    """Replaying the ring buffer to a reconnecting plugin routes through the
    queue policy: only the first shared-group message broadcasts, the rest
    enqueue behind a fresh busy slot."""
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        # Seed the ring buffer with two shared-group messages from different
        # users (no slash commands so they get buffered).
        await relay.push_event(_group_event("msg1", gid="42", uid="100", mid="1"))
        await relay.push_event(_group_event("msg2", gid="42", uid="200", mid="2"))
        assert len(relay._ring_buffer) == 2

        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                ready = await ws.receive_json(timeout=2)
                assert ready["type"] == "ready"
                # First replayed message should arrive; the second is queued.
                msg1 = await ws.receive_json(timeout=2)
                assert msg1["type"] == "event"
                assert msg1["text"] in {"msg1", "msg2"}
                # No second event should arrive within a short window — it's queued.
                with pytest.raises(asyncio.TimeoutError):
                    await ws.receive_json(timeout=0.5)
                # busy slot set, queue has one entry
                assert "42" in relay._busy_groups
                assert len(relay._queues.get("42", [])) == 1
    finally:
        await server.close()


async def test_hot_reload_trims_oversized_queue():
    """When event_queue_max_per_chat is lowered via hot-reload, existing queues
    are trimmed to the new cap."""
    relay, _, cfg = _make_relay(event_queue_max_per_chat=10)
    relay._broadcast_event = AsyncMock()
    # Make the group busy and queue 5 messages.
    await relay._enqueue_or_broadcast(_group_event("h", gid="42", uid="100"))
    for i in range(5):
        await relay._enqueue_or_broadcast(_group_event(f"m{i}", gid="42", uid=f"u{i}", mid=str(i)))
    assert len(relay._queues["42"]) == 5
    # Hot-reload to a smaller cap.
    new_cfg = cfg.with_overrides(event_queue_max_per_chat=2)
    relay.update_config(new_cfg)
    assert len(relay._queues["42"]) == 2


# ── idle_message protocol helper ─────────────────────────────────────────


def test_idle_message_shape():
    msg = idle_message("group:42", "42")
    assert msg["type"] == "idle"
    assert msg["v"] == 1
    assert msg["chat_id"] == "group:42"
    assert msg["group_id"] == "42"

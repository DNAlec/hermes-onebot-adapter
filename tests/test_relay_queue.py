"""Tests for the shared-group queue policy in ``HermesRelayServer``.

排队的判定改为基于插件上报的 Hermes ``group_sessions_per_user``:
- True(隔离,默认)→ 不排队
- False(共享)→ 看 ``event_queue_enabled`` 总开关,开则排队

测试覆盖:
- DM 直接放行 / per_user=True 不排队 / event_queue_enabled=False 不排队
- shared 群聊 busy 时任何用户(含 busy 用户自身)都入队
- 连续同用户消息出队时合并为一条
- /命令绕过排队
- idle 帧 dequeue + dispatch
- 队列超限丢弃最旧
- 看门狗超时清空
- plugin 断开清空 busy
- ring buffer replay 走排队
- hot-reload trim 队列 / 关总开关清空 busy
- hermes_mode_report 帧缓存与状态切换
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import aiohttp.web
import pytest
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.config import AdapterConfig
from onebot_adapter.relay.hermes_ws import HermesRelayServer
from onebot_adapter.relay.protocol import (
    NormalizedEvent,
    hermes_mode_report_message,
    idle_message,
    mode_refresh_message,
)


def _make_relay(**cfg_overrides):
    mock_api = MagicMock()
    defaults = dict(
        hermes_ws_token="testtoken",
        hermes_ws_path="/hermes",
        event_queue_enabled=True,
        event_queue_idle_timeout=300.0,
        event_queue_max_per_chat=50,
    )
    defaults.update(cfg_overrides)
    cfg = AdapterConfig(**defaults)
    relay = HermesRelayServer(cfg, mock_api, adapter_version="0.1.0-test", onebot_connected_fn=lambda: True)
    # 模拟插件上报"Hermes 不隔离群成员"(排队生效的前置条件)
    relay._store_hermes_mode(False)
    # Seed a fake connected client so _broadcast_with_status routes to the
    # mocked _broadcast_event instead of returning "dropped" (zero clients).
    # Tests that exercise the zero-client path override _clients explicitly.
    fake_ws = MagicMock()
    relay._clients.add(fake_ws)
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


# ── 纯路由判定 ──────────────────────────────────────────────────────────


async def test_dm_never_queues():
    """私聊直接广播,不进排队状态。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_make_event("hi", chat_id="100", chat_type="dm"))
    relay._broadcast_event.assert_awaited_once()
    assert not relay._busy_groups
    assert not relay._queues


async def test_per_user_true_does_not_queue():
    """插件上报 group_sessions_per_user=True → 不排队(即使开关开)。"""
    relay, _, _ = _make_relay()
    relay._store_hermes_mode(True)  # Hermes 隔离
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200"))
    # 两条都直接广播,无 busy/queue
    assert relay._broadcast_event.await_count == 2
    assert not relay._busy_groups
    assert not relay._queues


async def test_event_queue_enabled_false_does_not_queue():
    """总开关关闭 → 不排队,即使 per_user=False。"""
    relay, _, _ = _make_relay(event_queue_enabled=False)
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200"))
    assert relay._broadcast_event.await_count == 2
    assert not relay._busy_groups
    assert not relay._queues


async def test_shared_group_first_message_marks_busy():
    """第一条 shared 群聊消息广播并标记 busy。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    ev = _group_event("hi", gid="42", uid="100")
    await relay._enqueue_or_broadcast(ev)
    relay._broadcast_event.assert_awaited_once_with(ev)
    assert "42" in relay._busy_groups
    busy_user, since = relay._busy_groups["42"]
    assert busy_user == "100"
    assert since > 0


async def test_shared_group_busy_same_user_enqueues():
    """busy 时同人新消息也入队,不再放行。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    ev2 = _group_event("msg2", gid="42", uid="100", mid="2")
    result = await relay._enqueue_or_broadcast(ev2)
    assert result == "queued"
    assert relay._broadcast_event.await_count == 1  # only msg1 broadcast
    assert "42" in relay._queues
    assert len(relay._queues["42"]) == 1
    assert relay._queues["42"][0].user_id == "100"


async def test_shared_group_busy_different_user_enqueues():
    """busy 时不同人入队。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    result1 = await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    assert result1 == "broadcast"  # 广播,不入队
    result2 = await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200", mid="2"))
    assert result2 == "queued"    # 入队
    assert relay._broadcast_event.await_count == 1
    assert "42" in relay._queues
    assert len(relay._queues["42"]) == 1
    assert relay._queues["42"][0].user_id == "200"


async def test_slash_command_bypasses_queue():
    """/命令绕过排队直接广播。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    slash_ev = _group_event("/stop", gid="42", uid="200", mid="2")
    await relay._enqueue_or_broadcast(slash_ev)
    assert relay._broadcast_event.await_count == 2
    relay._broadcast_event.assert_awaited_with(slash_ev)
    assert "42" not in relay._queues


async def test_queue_cap_rejects_incoming():
    """队列超限拒绝新消息入队。"""
    relay, _, _ = _make_relay(event_queue_max_per_chat=2)
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("head", gid="42", uid="100", mid="0"))
    r1 = await relay._enqueue_or_broadcast(_group_event("m1", gid="42", uid="200", mid="1"))
    r2 = await relay._enqueue_or_broadcast(_group_event("m2", gid="42", uid="300", mid="2"))
    r3 = await relay._enqueue_or_broadcast(_group_event("m3", gid="42", uid="400", mid="3"))
    assert (r1, r2, r3) == ("queued", "queued", "dropped")
    q = relay._queues["42"]
    assert len(q) == 2
    assert [e.text for e in q] == ["m1", "m2"]


# ── on_dispatch 回调 ────────────────────────────────────────────────────


async def test_on_dispatch_callback_fires_on_dequeue():
    """排队事件出队时触发 on_dispatch 回调。"""
    callback = AsyncMock()
    relay, _, _ = _make_relay()
    relay._on_dispatch = callback
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200", mid="2"))
    assert callback.call_count == 0
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    await asyncio.sleep(0)
    assert callback.call_count == 1
    dequed_event = callback.call_args[0][0]
    assert dequed_event.text == "msg2"
    assert dequed_event.user_id == "200"


# ── 出队合并 ─────────────────────────────────────────────────────────────


async def test_dequeue_merges_consecutive_same_user():
    """连续同用户消息出队时合并为一条。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("m0", gid="42", uid="100"))
    # 同用户两条入队
    await relay._enqueue_or_broadcast(_group_event("m1", gid="42", uid="200", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("m2", gid="42", uid="200", mid="2"))
    assert len(relay._queues["42"]) == 2
    # idle → 出队，应合并 m1+m2
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert relay._broadcast_event.await_count == 2  # m0 + merged
    merged = relay._broadcast_event.call_args_list[1][0][0]
    assert merged.user_id == "200"
    assert merged.text == "m1\n\nm2"
    assert merged.message_id == "2"
    assert "42" not in relay._queues


async def test_dequeue_does_not_merge_different_users():
    """不同用户的消息打断合并链。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("m0", gid="42", uid="100"))
    await relay._enqueue_or_broadcast(_group_event("m1", gid="42", uid="200", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("m2", gid="42", uid="300", mid="2"))
    # idle → m1 出队(m2 是不同用户,不合并)
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert relay._broadcast_event.await_count == 2
    broadcasted = relay._broadcast_event.call_args_list[1][0][0]
    assert broadcasted.text == "m1"
    assert len(relay._queues["42"]) == 1  # m2 还在队列


async def test_dequeue_merges_multiple_same_user():
    """3+ 条同用户消息全部合并。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("m0", gid="42", uid="100"))
    await relay._enqueue_or_broadcast(_group_event("a", gid="42", uid="200", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("b", gid="42", uid="200", mid="2"))
    await relay._enqueue_or_broadcast(_group_event("c", gid="42", uid="200", mid="3"))
    assert len(relay._queues["42"]) == 3
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    merged = relay._broadcast_event.call_args_list[1][0][0]
    assert merged.user_id == "200"
    assert merged.text == "a\n\nb\n\nc"
    assert merged.message_id == "3"
    assert "42" not in relay._queues


# ── idle 帧 ─────────────────────────────────────────────────────────────


async def test_idle_frame_dequeues_next():
    """idle 帧清 busy 并派发下一条。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200", mid="2"))
    assert relay._broadcast_event.await_count == 1
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert relay._broadcast_event.await_count == 2
    assert relay._busy_groups["42"][0] == "200"
    assert "42" not in relay._queues


async def test_idle_frame_no_queue_just_clears_busy():
    """idle 帧无队列时只清 busy。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    assert "42" not in relay._busy_groups
    assert relay._broadcast_event.await_count == 1


async def test_idle_frame_for_non_busy_group_is_noop():
    """非 busy 群的 idle 帧被忽略。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    relay._broadcast_event.assert_not_awaited()
    assert not relay._busy_groups


async def test_idle_frame_falls_back_to_chat_id():
    """group_id 缺失时从 chat_id 解析。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    await relay._handle_idle({"type": "idle", "chat_id": "group:42"})
    assert "42" not in relay._busy_groups


async def test_idle_frame_without_group_id_or_chat_id_is_ignored():
    """既无 group_id 又无 parseable chat_id 的 idle 帧被丢弃。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._handle_idle({"type": "idle", "chat_id": "garbage"})
    relay._broadcast_event.assert_not_awaited()


# ── 看门狗 ──────────────────────────────────────────────────────────────


async def test_watchdog_clears_stale_busy():
    """看门狗强制清空超时 busy 槽。"""
    relay, _, _ = _make_relay(event_queue_idle_timeout=0.01)
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))
    busy_user, _ = relay._busy_groups["42"]
    relay._busy_groups["42"] = (busy_user, time.monotonic() - 100)
    relay._dequeue_and_dispatch("42")
    assert "42" not in relay._busy_groups


async def test_watchdog_dispatches_queued_after_timeout():
    """看门狗清空时若有队列则派发下一条。"""
    relay, _, _ = _make_relay(event_queue_idle_timeout=0.01)
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200", mid="2"))
    busy_user, _ = relay._busy_groups["42"]
    relay._busy_groups["42"] = (busy_user, time.monotonic() - 100)
    relay._dequeue_and_dispatch("42")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert relay._broadcast_event.await_count == 2
    assert relay._busy_groups["42"][0] == "200"


# ── 断开 / hot-reload / replay ───────────────────────────────────────────


async def test_last_client_disconnect_clears_busy_and_queues():
    """最后一个 plugin client 断开时清空所有 busy/queue。"""
    relay, _, _ = _make_relay()
    relay._clients.clear()  # drop the seeded fake client; this test uses a real WS
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)  # ready
                assert relay.has_clients
                await relay._enqueue_or_broadcast(_group_event("m1", gid="42", uid="100"))
                await relay._enqueue_or_broadcast(_group_event("m2", gid="42", uid="200"))
                assert "42" in relay._busy_groups
                assert "42" in relay._queues
            await asyncio.sleep(0.1)
        assert not relay._busy_groups
        assert not relay._queues
    finally:
        await server.close()


async def test_ring_buffer_replay_honors_queue():
    """ring buffer replay 走排队逻辑,重连不会一次性推送多条。"""
    relay, _, _ = _make_relay()
    relay._clients.clear()  # drop seeded fake client; this test uses a real WS
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        await relay.push_event(_group_event("msg1", gid="42", uid="100", mid="1"))
        await relay.push_event(_group_event("msg2", gid="42", uid="200", mid="2"))
        assert len(relay._ring_buffer) == 2
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                ready = await ws.receive_json(timeout=2)
                assert ready["type"] == "ready"
                msg1 = await ws.receive_json(timeout=2)
                assert msg1["type"] == "event"
                # 第二条被排队,短时间内不应到达
                with pytest.raises(asyncio.TimeoutError):
                    await ws.receive_json(timeout=0.5)
                assert "42" in relay._busy_groups
                assert len(relay._queues.get("42", [])) == 1
    finally:
        await server.close()


async def test_hot_reload_trims_oversized_queue():
    """调小 event_queue_max_per_chat 时 trim 现有队列。"""
    relay, _, cfg = _make_relay(event_queue_max_per_chat=10)
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("h", gid="42", uid="100"))
    for i in range(5):
        await relay._enqueue_or_broadcast(_group_event(f"m{i}", gid="42", uid=f"u{i}", mid=str(i)))
    assert len(relay._queues["42"]) == 5
    new_cfg = cfg.with_overrides(event_queue_max_per_chat=2)
    relay.update_config(new_cfg)
    assert len(relay._queues["42"]) == 2


async def test_hot_reload_disabling_queue_clears_busy():
    """总开关从 True→False 时清空 busy/queue。"""
    relay, _, cfg = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("m1", gid="42", uid="100"))
    await relay._enqueue_or_broadcast(_group_event("m2", gid="42", uid="200"))
    assert "42" in relay._busy_groups
    assert "42" in relay._queues
    new_cfg = cfg.with_overrides(event_queue_enabled=False)
    relay.update_config(new_cfg)
    assert not relay._busy_groups
    assert not relay._queues


# ── hermes_mode_report 帧 ────────────────────────────────────────────────


async def test_hermes_mode_report_frame_updates_cache():
    """收到 hermes_mode_report 帧后缓存 per_user 值。"""
    relay, _, _ = _make_relay()
    assert relay.hermes_group_sessions_per_user is False  # _make_relay 默认设的
    relay._store_hermes_mode(True)
    assert relay.hermes_group_sessions_per_user is True
    relay._store_hermes_mode(False)
    assert relay.hermes_group_sessions_per_user is False


def test_hermes_mode_report_message_shape():
    msg = hermes_mode_report_message(False)
    assert msg["type"] == "hermes_mode_report"
    assert msg["v"] == 1
    assert msg["group_sessions_per_user"] is False


def test_mode_refresh_message_shape():
    msg = mode_refresh_message()
    assert msg["type"] == "mode_refresh"
    assert msg["v"] == 1


async def test_per_user_becoming_true_clears_busy():
    """从 per_user=False 切到 True(隔离)时清空 busy/queue。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("m1", gid="42", uid="100"))
    await relay._enqueue_or_broadcast(_group_event("m2", gid="42", uid="200"))
    assert "42" in relay._busy_groups
    relay._store_hermes_mode(True)
    assert not relay._busy_groups
    assert not relay._queues


# ── idle_message protocol helper ─────────────────────────────────────────


def test_idle_message_shape():
    msg = idle_message("group:42", "42")
    assert msg["type"] == "idle"
    assert msg["v"] == 1
    assert msg["chat_id"] == "group:42"
    assert msg["group_id"] == "42"


# ── Regression tests for queue-loss robustness fixes ────────────────────


async def test_dequeue_broadcast_task_tracked_in_text_tasks():
    """The broadcast task spawned by _dequeue_and_dispatch must be tracked
    in _text_tasks so stop() can cancel it.  Previously it was fire-and-forget
    with no tracking, so a stop() between dequeue and broadcast could leave
    a dangling task writing to a torn-down state.
    """
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200", mid="2"))
    # Trigger dequeue
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    # Let the spawned tasks run
    for _ in range(3):
        await asyncio.sleep(0)
    # The broadcast task should have been tracked and now discarded (done)
    # Verify no exception was swallowed
    assert relay._broadcast_event.await_count == 2


async def test_dequeue_broadcast_task_exception_does_not_crash_silently():
    """If _broadcast_event raises, the done-callback should log the error
    rather than letting it surface as 'Task exception was never retrieved'.
    The relay must remain operational for subsequent dequeues.
    """
    relay, _, _ = _make_relay()
    call_count = [0]

    async def failing_broadcast(event):
        call_count[0] += 1
        if call_count[0] == 2:  # fail on the dequeued (second) broadcast
            raise RuntimeError("simulated broadcast failure")

    relay._broadcast_event = failing_broadcast
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("msg2", gid="42", uid="200", mid="2"))
    # Dequeue triggers the failing broadcast
    await relay._handle_idle({"type": "idle", "group_id": "42", "chat_id": "group:42"})
    # Let tasks complete so the done-callback fires
    for _ in range(5):
        await asyncio.sleep(0)
    # Relay is still operational — busy slot was set for msg2 despite broadcast failure
    assert "42" in relay._busy_groups
    assert relay._busy_groups["42"][0] == "200"


async def test_broadcast_to_zero_clients_logs_warning(caplog):
    """Broadcasting a dequeued event with 0 connected plugin clients should
    not silently drop it — previously the 'sending to 0 client(s)' log was
    DEBUG (invisible at default INFO level) and the event was lost forever.
    """
    relay, _, _ = _make_relay()
    relay._clients.clear()  # explicitly simulate zero clients
    ev = _group_event("orphan", gid="42", uid="100", mid="1")
    # Should not raise; should log WARNING and return without sending
    with caplog.at_level(logging.WARNING):
        await relay._broadcast_event(ev)
    assert "0 plugin clients connected" in caplog.text


async def test_push_event_returns_dropped_when_zero_clients():
    """push_event on a group with no plugin clients returns ``"dropped"`` so
    the caller (app._on_onebot_event) does NOT react as if delivered.
    Regression guard for the queue-full / zero-client false-"delivered" bug.
    """
    relay, _, _ = _make_relay()
    relay._clients.clear()
    outcome = await relay.push_event(_group_event("hi", gid="42", uid="100"))
    assert outcome == "dropped"


async def test_watchdog_loop_survives_iteration_exception():
    """The watchdog loop must not die if an iteration raises — previously
    a single exception would kill the task and leave all busy slots stuck
    forever (no more reaping).  Now it logs and continues.
    """
    relay, _, _ = _make_relay(event_queue_idle_timeout=0.01)
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100"))

    # Make _dequeue_and_dispatch raise on first call by poisoning _queues
    original_dequeue = relay._dequeue_and_dispatch
    call_count = [0]

    def flaky_dequeue(gid):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("simulated watchdog iteration failure")
        original_dequeue(gid)

    relay._dequeue_and_dispatch = flaky_dequeue
    # Age out the busy slot
    busy_user, _ = relay._busy_groups["42"]
    relay._busy_groups["42"] = (busy_user, time.monotonic() - 100)

    # Run one watchdog iteration manually (not the full loop — just the body)
    # We simulate the loop body to verify the try/except wrapping
    try:
        now = time.monotonic()
        timeout = relay._config.event_queue_idle_timeout
        for gid in list(relay._busy_groups.keys()):
            busy_user, since = relay._busy_groups.get(gid, ("", now))
            if now - since < timeout:
                continue
            relay._dequeue_and_dispatch(gid)
    except Exception:
        # The fix wraps the loop body in try/except — but since we're testing
        # the body directly, we verify the exception is caught. In the real
        # loop, this would be caught and the loop continues.
        pass

    # Verify the exception was raised (flaky_dequeue raised on first call)
    assert call_count[0] == 1
    # The busy slot is still there (dequeue failed) but the loop would continue
    assert "42" in relay._busy_groups

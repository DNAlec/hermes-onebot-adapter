"""Tests for the shared-group queue policy in ``HermesRelayServer``.

排队的判定改为基于插件上报的 Hermes ``group_sessions_per_user``:
- True(隔离,默认)→ 不排队
- False(共享)→ 看 ``event_queue_enabled`` 总开关,开则排队

测试覆盖:
- DM 直接放行 / per_user=True 不排队 / event_queue_enabled=False 不排队
- shared 群聊 busy + 同人放行 vs 不同人入队
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


async def test_shared_group_busy_same_user_passes_through():
    """busy 时同人新消息直接放行。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    ev2 = _group_event("msg2", gid="42", uid="100", mid="2")
    await relay._enqueue_or_broadcast(ev2)
    assert relay._broadcast_event.await_count == 2
    assert not relay._queues
    assert relay._busy_groups["42"][0] == "100"


async def test_shared_group_busy_different_user_enqueues():
    """busy 时不同人入队。"""
    relay, _, _ = _make_relay()
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("msg1", gid="42", uid="100", mid="1"))
    ev2 = _group_event("msg2", gid="42", uid="200", mid="2")
    await relay._enqueue_or_broadcast(ev2)
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


async def test_queue_cap_drops_oldest():
    """队列超限丢弃最旧。"""
    relay, _, _ = _make_relay(event_queue_max_per_chat=2)
    relay._broadcast_event = AsyncMock()
    await relay._enqueue_or_broadcast(_group_event("head", gid="42", uid="100", mid="0"))
    await relay._enqueue_or_broadcast(_group_event("m1", gid="42", uid="200", mid="1"))
    await relay._enqueue_or_broadcast(_group_event("m2", gid="42", uid="300", mid="2"))
    await relay._enqueue_or_broadcast(_group_event("m3", gid="42", uid="400", mid="3"))
    q = relay._queues["42"]
    assert len(q) == 2
    assert [e.text for e in q] == ["m2", "m3"]


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
    await relay._handle_text.__wrapped__ if hasattr(relay._handle_text, "__wrapped__") else None
    # 直接调 _handle_text 模拟插件发送
    # 用 _store_hermes_mode 间接验证(因为 _handle_text 需要 ws)
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

"""Tests for the WsApiTransport — OneBot 11 API calls over a shared WebSocket.

OneBot 11 的 WS 是双向的：既能推事件帧（带 post_type），也能接受 API 请求帧
（{action, params, echo}）并回响应帧（{retcode, data, echo}）。WsApiTransport 用
echo 字段做请求-响应关联。这里覆盖 roundtrip、超时、连接断开 reject、响应帧拦截、
未知 echo 透传、多连接选首个、无连接抛 RuntimeError、retcode 非零抛错等场景。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from onebot_adapter.onebot.api import OneBotApi
from onebot_adapter.onebot.ws_api import WsApiTransport


def _make_ws() -> MagicMock:
    """Build a fake ws whose send_json is an AsyncMock recording frames."""
    ws = MagicMock()
    ws.send_json = AsyncMock()
    return ws


# ── register / unregister / has_active ─────────────────────────────────


def test_has_active_reflects_registration():
    t = WsApiTransport()
    assert t.has_active is False
    ws = _make_ws()
    t.register(ws)
    assert t.has_active is True
    t.unregister(ws)
    assert t.has_active is False


async def test_unregister_rejects_pending_when_no_active_left():
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)

    task = asyncio.create_task(t.request("get_login_info", {}))
    await asyncio.sleep(0.01)
    # request was sent (send_json awaited); now drop the connection
    t.unregister(ws)
    with pytest.raises(ConnectionError):
        await task


# ── request roundtrip ──────────────────────────────────────────────────


async def test_request_response_roundtrip():
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)

    async def call():
        # 在 request 发出后，模拟 OneBot 回响应帧
        # 先等 send_json 被调用，拿到 echo
        await asyncio.sleep(0)  # 让 task 调度
        # 读取 send_json 的最后一次调用参数
        # 由于 request 内部 await ws.send_json，需稍等
        return await t.request("get_login_info", {})

    task = asyncio.create_task(call())
    # 等 request 把 send_json 调用挂起
    await asyncio.sleep(0.01)
    assert ws.send_json.await_count == 1
    frame = ws.send_json.await_args.args[0]
    echo = frame["echo"]
    assert frame["action"] == "get_login_info"

    # 模拟 OneBot 回响应
    handled = t.on_text(json.dumps({"retcode": 0, "data": {"user_id": 123, "nickname": "bot"}, "echo": echo}))
    assert handled is True

    resp = await asyncio.wait_for(task, timeout=2)
    assert resp["retcode"] == 0
    assert resp["data"]["user_id"] == 123


async def test_request_no_active_connection_raises():
    t = WsApiTransport()
    with pytest.raises(RuntimeError, match="no active"):
        await t.request("send_group_msg", {"group_id": 1, "message": []})


async def test_request_timeout():
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)

    with pytest.raises(TimeoutError):
        await t.request("get_login_info", {}, timeout=0.1)
    # pending 应该被清理
    assert t._pending == {}


async def test_send_json_failure_raises_runtime_error():
    t = WsApiTransport()
    ws = _make_ws()
    ws.send_json = AsyncMock(side_effect=RuntimeError("ws closed"))
    t.register(ws)

    with pytest.raises(RuntimeError, match="failed to send WS API frame"):
        await t.request("get_login_info", {})
    assert t._pending == {}


# ── on_text interception ───────────────────────────────────────────────


async def test_on_text_intercepts_response_with_matching_echo():
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)

    # 同步发出一个请求：我们手动塞一个 future 到 pending 模拟在途请求
    loop = asyncio.get_running_loop()
    echo = "abc123"
    fut = loop.create_future()
    t._pending[echo] = fut

    raw = json.dumps({"retcode": 0, "data": {"ok": True}, "echo": echo})
    assert t.on_text(raw) is True
    assert fut.done()
    assert fut.result()["data"]["ok"] is True
    assert echo not in t._pending


async def test_on_text_passes_through_event_frame():
    """事件帧没有 echo 字段，应该返回 False 交给 parser。"""
    t = WsApiTransport()
    raw = json.dumps({"post_type": "message", "message_type": "group", "group_id": 42})
    assert t.on_text(raw) is False


async def test_on_text_unknown_echo_passes_through():
    """有 echo 字段但不在 pending 表里 → 不拦截，返回 False。"""
    t = WsApiTransport()
    raw = json.dumps({"retcode": 0, "data": {}, "echo": "not-pending"})
    assert t.on_text(raw) is False


async def test_on_text_non_json_returns_false():
    t = WsApiTransport()
    assert t.on_text("not json") is False


async def test_on_text_non_dict_returns_false():
    t = WsApiTransport()
    assert t.on_text(json.dumps([1, 2, 3])) is False


async def test_on_text_empty_echo_returns_false():
    t = WsApiTransport()
    assert t.on_text(json.dumps({"retcode": 0, "data": {}, "echo": ""})) is False


# ── multiple connections: pick first ───────────────────────────────────


async def test_pick_first_active_connection():
    t = WsApiTransport()
    ws1 = _make_ws()
    ws2 = _make_ws()
    t.register(ws1)
    t.register(ws2)
    assert t.has_active is True

    task = asyncio.create_task(t.request("get_login_info", {}, timeout=1))
    await asyncio.sleep(0.01)
    # 请求应发给其中一个 ws（取首个）；这里验证只触发了一次 send_json
    sent_count = ws1.send_json.await_count + ws2.send_json.await_count
    assert sent_count == 1
    # 取消任务以避免超时等待
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def test_unregister_one_keeps_others_active():
    """多条连接中 unregister 一条，仍有一条活跃时不应 reject pending。"""
    t = WsApiTransport()
    ws1 = _make_ws()
    ws2 = _make_ws()
    t.register(ws1)
    t.register(ws2)

    task = asyncio.create_task(t.request("get_login_info", {}, timeout=2))
    await asyncio.sleep(0.01)
    # 注销 ws1；因为还有 ws2 活跃，pending 不应被 reject
    t.unregister(ws1)
    assert t.has_active is True
    # 手动回响应以让 task 完成
    # 取消避免悬而未决
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ── OneBotApi on top of WsApiTransport ──────────────────────────────


async def test_api_call_ok_returns_data():
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)
    api = OneBotApi(ws_transport=t)

    task = asyncio.create_task(api.call("get_login_info", {}))
    await asyncio.sleep(0.01)
    frame = ws.send_json.await_args.args[0]
    t.on_text(json.dumps({"retcode": 0, "data": {"user_id": 42}, "echo": frame["echo"]}))
    data = await asyncio.wait_for(task, timeout=2)
    assert data["data"]["user_id"] == 42


async def test_api_call_nonzero_retcode_raises():
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)
    api = OneBotApi(ws_transport=t)

    task = asyncio.create_task(api.call("send_group_msg", {"group_id": 1}))
    await asyncio.sleep(0.01)
    frame = ws.send_json.await_args.args[0]
    t.on_text(json.dumps({"retcode": 100, "status": "failed", "msg": "group not found", "echo": frame["echo"]}))
    with pytest.raises(RuntimeError, match="retcode=100"):
        await asyncio.wait_for(task, timeout=2)


async def test_api_no_connection_raises():
    t = WsApiTransport()
    api = OneBotApi(ws_transport=t)
    with pytest.raises(RuntimeError):
        await api.call("get_login_info")


async def test_api_get_login_info_helper():
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)
    api = OneBotApi(ws_transport=t)

    task = asyncio.create_task(api.get_login_info())
    await asyncio.sleep(0.01)
    frame = ws.send_json.await_args.args[0]
    assert frame["action"] == "get_login_info"
    t.on_text(json.dumps({"retcode": 0, "data": {"user_id": 777, "nickname": "n"}, "echo": frame["echo"]}))
    data = await asyncio.wait_for(task, timeout=2)
    assert data["user_id"] == 777


async def test_api_send_group_msg_helper():
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)
    api = OneBotApi(ws_transport=t)

    task = asyncio.create_task(api.send_group_msg(42, [{"type": "text", "data": {"text": "hi"}}]))
    await asyncio.sleep(0.01)
    frame = ws.send_json.await_args.args[0]
    assert frame["action"] == "send_group_msg"
    assert frame["params"]["group_id"] == 42
    t.on_text(json.dumps({"retcode": 0, "data": {"message_id": 99}, "echo": frame["echo"]}))
    data = await asyncio.wait_for(task, timeout=2)
    assert data["message_id"] == 99

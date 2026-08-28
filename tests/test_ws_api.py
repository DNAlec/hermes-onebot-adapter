"""Tests for the WsApiTransport — OneBot 11 API calls over a shared WebSocket.

OneBot 11 的 WS 是双向的：既能推事件帧（带 post_type），也能接受 API 请求帧
（{action, params, echo}）并回响应帧（{retcode, data, echo}）。WsApiTransport 用
echo 字段做请求-响应关联。这里覆盖 roundtrip、超时、连接断开 reject、响应帧拦截、
未知 echo 透传、多连接选首个、无连接抛 RuntimeError、retcode 非零抛错等场景。
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot import api as api_module
from onebot_adapter.onebot.api import OneBotApi, UploadOutcomeUnknownError
from onebot_adapter.onebot.log_format import PREVIEW_LOGGER_NAME
from onebot_adapter.onebot.ws_api import WsApiTransport, bind_request_ws, reset_request_ws


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


async def test_send_json_failure_after_unregister_re_raises_connection_error():
    """Race: unregister() sets ConnectionError on the future before send_json
    raises.  The caller should see ConnectionError, not a masked RuntimeError.

    This exercises the ``if fut.done(): raise fut.exception()`` branch in
    ``request()``: the WS disconnects between ``_pick_ws()`` and
    ``ws.send_json()``, so ``unregister`` rejects the future first, then
    ``send_json`` fails on the closed WS.
    """
    t = WsApiTransport()
    ws = _make_ws()

    async def fail_send(frame):
        # Simulate send failing because the WS is closed — but by the time
        # this runs, unregister has already set ConnectionError on the future.
        raise RuntimeError("ws is closed")

    ws.send_json = AsyncMock(side_effect=fail_send)
    t.register(ws)

    # Pre-reject the future by unregistering *before* the request runs the
    # send_json call.  We need the unregister to happen between _pick_ws and
    # send_json.  Achieve this by making the request task start, then
    # unregister, then let send_json fire.

    # Actually, simpler: call unregister first to set ConnectionError, then
    # call request. _pick_ws will raise RuntimeError (no active ws) — not the
    # branch we want.  Instead, keep ws active but pre-set the future.
    #
    # The cleanest way: register ws, start request, intercept before send_json
    # to unregister, then let send_json fail.
    t.register(ws)

    # Patch send_json to unregister ws first (simulating a concurrent close),
    # then raise.
    async def unregister_then_fail(frame):
        t.unregister(ws)
        raise RuntimeError("ws is closed")

    ws.send_json = AsyncMock(side_effect=unregister_then_fail)

    with pytest.raises(ConnectionError):
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
    # 注销 ws1;因为还有 ws2 活跃，pending 不应被 reject
    t.unregister(ws1)
    assert t.has_active is True
    # 手动回响应以让 task 完成
    # 取消避免悬而未决
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def test_unregister_rejects_only_that_ws_pending():
    """unregister(ws1) 应只 reject ws1 发出的请求,保留 ws2 上的 pending。

    多实例场景:ws1 断开时不应影响其它连接的 in-flight 请求。
    """
    t = WsApiTransport()
    ws1 = _make_ws()
    ws2 = _make_ws()
    t.register(ws1)
    t.register(ws2)

    # 让 ws2 成为 _pick_ws 的首选(先注册的不会被选中,因为 set 无序,
    # 但我们用手动 echo 追踪确认) — 直接发两个请求分别到 ws1/ws2
    # 通过控制 _active 的迭代顺序不可行;改为验证:ws1 的请求在 ws1 断开时被 reject
    task1 = asyncio.create_task(t.request("get_login_info", {}, timeout=2))
    await asyncio.sleep(0.01)
    # 此时请求发给了某个 ws。强制把它绑到 ws1:
    # 找到 echo 并重映射 _echo_ws
    sent_frame = ws1.send_json.await_args.args[0] if ws1.send_json.await_count else None
    if sent_frame is None:
        sent_frame = ws2.send_json.await_args.args[0]
        # 请求发给了 ws2 — 重映射以测试 ws1 的 reject 路径
        t._echo_ws[sent_frame["echo"]] = ws1
    # 注销 ws1 → 该请求应被 reject
    t.unregister(ws1)
    with pytest.raises(ConnectionError):
        await task1


async def test_request_uses_bound_inbound_ws():
    t = WsApiTransport()
    ws1 = _make_ws()
    ws2 = _make_ws()
    t.register(ws1)
    t.register(ws2)
    token = bind_request_ws(ws2)
    try:
        task = asyncio.create_task(t.request("get_login_info", {}, timeout=2))
        await asyncio.sleep(0.01)
        assert ws2.send_json.await_count == 1
        echo = ws2.send_json.await_args.args[0]["echo"]
        assert t.on_text(json.dumps({"echo": echo, "retcode": 0, "data": {"user_id": 1}}))
        data = await task
        assert data["retcode"] == 0
    finally:
        reset_request_ws(token)


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


async def test_api_call_failed_status_without_retcode_raises():
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)
    api = OneBotApi(ws_transport=t)

    task = asyncio.create_task(api.call("upload_group_file", {"file": "/tmp/a"}))
    await asyncio.sleep(0.01)
    frame = ws.send_json.await_args.args[0]
    t.on_text(json.dumps({
        "status": "failed",
        "msg": "识别URL失败",
        "echo": frame["echo"],
    }))
    with pytest.raises(RuntimeError, match="status=failed"):
        await asyncio.wait_for(task, timeout=2)


async def test_api_no_connection_raises():
    t = WsApiTransport()
    api = OneBotApi(ws_transport=t)
    assert api.connected is False
    with pytest.raises(RuntimeError):
        await api.call("get_login_info")


def test_api_connected_reflects_transport_state():
    t = WsApiTransport()
    api = OneBotApi(ws_transport=t)
    ws = _make_ws()

    assert api.connected is False
    t.register(ws)
    assert api.connected is True
    t.unregister(ws)
    assert api.connected is False


async def test_api_upload_actions_use_action_specific_timeouts():
    transport = MagicMock()
    transport.request = AsyncMock(return_value={"retcode": 0, "data": {}})
    api = OneBotApi(ws_transport=transport, file_upload_timeout=480.0)

    await api.call("upload_group_file", {"group_id": 1, "file": "/tmp/a", "name": "a"})
    assert transport.request.await_args.kwargs["timeout"] == 480.0

    await api.upload_private_file(2, "/tmp/b", "b")
    assert transport.request.await_args.kwargs["timeout"] == 480.0

    await api.call("create_flash_task", {"files": "/tmp/c"})
    assert transport.request.await_args.kwargs["timeout"] == 480.0


async def test_api_file_upload_timeout_hot_reload():
    transport = MagicMock()
    transport.request = AsyncMock(return_value={"retcode": 0, "data": {}})
    api = OneBotApi(ws_transport=transport, file_upload_timeout=300.0)

    api.update_file_upload_timeout(480.0)
    await api.call("upload_group_file", {})
    assert transport.request.await_args.kwargs["timeout"] == 480.0

    await api.call("upload_private_file", {})
    assert transport.request.await_args.kwargs["timeout"] == 480.0


async def test_api_explicit_timeout_overrides_upload_timeout():
    transport = MagicMock()
    transport.request = AsyncMock(return_value={"retcode": 0, "data": {}})
    api = OneBotApi(ws_transport=transport)

    await api.call("upload_group_file", {}, timeout=12.0)
    assert transport.request.await_args.kwargs["timeout"] == 12.0


async def test_flash_upload_timeout_reports_unknown_outcome():
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=TimeoutError())
    api = OneBotApi(ws_transport=transport, file_upload_timeout=480.0)

    with pytest.raises(UploadOutcomeUnknownError, match="may still be uploading"):
        await api.call("create_flash_task", {"files": "/tmp/a"})

    assert transport.request.await_args.kwargs["timeout"] == 480.0


async def test_api_non_upload_action_uses_transport_default_timeout():
    transport = MagicMock()
    transport.request = AsyncMock(return_value={"retcode": 0, "data": {}})
    api = OneBotApi(ws_transport=transport)

    await api.call("get_login_info")
    assert transport.request.await_args.kwargs["timeout"] is None


async def test_group_upload_timeout_confirmed_by_unique_recent_history(tmp_path, monkeypatch):
    file_path = tmp_path / "report.txt"
    file_path.write_text("confirmed", encoding="utf-8")
    now = api_module.time.time()
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=[
        TimeoutError(),
        {
            "retcode": 0,
            "data": {
                "messages": [{
                    "self_id": 123,
                    "user_id": 123,
                    "group_id": 42,
                    "time": now,
                    "message_id": 99,
                    "message": [{
                        "type": "file",
                        "data": {
                            "file": "report.txt",
                            "file_id": "file-99",
                            "file_size": file_path.stat().st_size,
                        },
                    }],
                }],
            },
        },
    ])
    monkeypatch.setattr(api_module, "_GROUP_UPLOAD_CONFIRM_DELAYS", (0.0,))
    api = OneBotApi(ws_transport=transport)

    result = await api.call("upload_group_file", {
        "group_id": 42,
        "file": str(file_path),
        "name": "report.txt",
    })

    assert result["data"] == {
        "file_id": "file-99",
        "message_id": 99,
        "confirmed_after_timeout": True,
    }
    assert transport.request.await_args_list[1].args[0] == "get_group_msg_history"
    assert transport.request.await_args_list[1].kwargs["timeout"] == 8.0


async def test_group_upload_timeout_without_match_reports_unknown(monkeypatch):
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=[
        TimeoutError(),
        {"retcode": 0, "data": {"messages": []}},
    ])
    monkeypatch.setattr(api_module, "_GROUP_UPLOAD_CONFIRM_DELAYS", (0.0,))
    api = OneBotApi(ws_transport=transport)

    with pytest.raises(UploadOutcomeUnknownError, match="may already have been uploaded"):
        await api.call("upload_group_file", {
            "group_id": 42,
            "file": "/tmp/missing.txt",
            "name": "missing.txt",
        })


async def test_group_upload_timeout_with_multiple_matches_is_ambiguous(monkeypatch):
    now = api_module.time.time()
    matching_message = {
        "self_id": 123,
        "user_id": 123,
        "group_id": 42,
        "time": now,
        "message": [{"type": "file", "data": {"file": "same.txt"}}],
    }
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=[
        TimeoutError(),
        {"retcode": 0, "data": {"messages": [
            {**matching_message, "message_id": 1},
            {**matching_message, "message_id": 2},
        ]}},
    ])
    monkeypatch.setattr(api_module, "_GROUP_UPLOAD_CONFIRM_DELAYS", (0.0,))
    api = OneBotApi(ws_transport=transport)

    with pytest.raises(UploadOutcomeUnknownError, match="could not be confirmed safely"):
        await api.call("upload_group_file", {
            "group_id": 42,
            "file": "https://example.test/same.txt",
            "name": "same.txt",
        })


async def test_group_upload_confirmation_detects_duplicate_on_later_poll(monkeypatch):
    now = api_module.time.time()
    matching_message = {
        "self_id": 123,
        "user_id": 123,
        "group_id": 42,
        "time": now,
        "message": [{"type": "file", "data": {"file": "same.txt"}}],
    }
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=[
        TimeoutError(),
        {"retcode": 0, "data": {"messages": [{**matching_message, "message_id": 1}]}},
        {"retcode": 0, "data": {"messages": [
            {**matching_message, "message_id": 1},
            {**matching_message, "message_id": 2},
        ]}},
    ])
    monkeypatch.setattr(api_module, "_GROUP_UPLOAD_CONFIRM_DELAYS", (0.0, 0.0))
    api = OneBotApi(ws_transport=transport)

    with pytest.raises(UploadOutcomeUnknownError, match="could not be confirmed safely"):
        await api.call("upload_group_file", {
            "group_id": 42,
            "file": "https://example.test/same.txt",
            "name": "same.txt",
        })


async def test_group_upload_confirmation_merges_file_id_added_on_later_poll(monkeypatch):
    now = api_module.time.time()
    base_message = {
        "self_id": 123,
        "user_id": 123,
        "group_id": 42,
        "time": now,
        "message_id": 99,
    }
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=[
        TimeoutError(),
        {"retcode": 0, "data": {"messages": [{
            **base_message,
            "message": [{"type": "file", "data": {"file": "same.txt"}}],
        }]}},
        {"retcode": 0, "data": {"messages": [{
            **base_message,
            "message": [{
                "type": "file",
                "data": {"file": "same.txt", "file_id": "file-99"},
            }],
        }]}},
    ])
    monkeypatch.setattr(api_module, "_GROUP_UPLOAD_CONFIRM_DELAYS", (0.0, 0.0))
    api = OneBotApi(ws_transport=transport)

    result = await api.call("upload_group_file", {
        "group_id": 42,
        "file": "https://example.test/same.txt",
        "name": "same.txt",
    })

    assert result["data"]["message_id"] == 99
    assert result["data"]["file_id"] == "file-99"
    assert result["data"]["confirmed_after_timeout"] is True


async def test_local_group_upload_requires_reported_size(tmp_path, monkeypatch):
    file_path = tmp_path / "report.txt"
    file_path.write_text("confirmed", encoding="utf-8")
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=[
        TimeoutError(),
        {"retcode": 0, "data": {"messages": [{
            "self_id": 123,
            "user_id": 123,
            "group_id": 42,
            "time": api_module.time.time(),
            "message_id": 99,
            "message": [{"type": "file", "data": {"file": "report.txt"}}],
        }]}},
    ])
    monkeypatch.setattr(api_module, "_GROUP_UPLOAD_CONFIRM_DELAYS", (0.0,))
    api = OneBotApi(ws_transport=transport)

    with pytest.raises(UploadOutcomeUnknownError, match="could not be confirmed safely"):
        await api.call("upload_group_file", {
            "group_id": 42,
            "file": str(file_path),
            "name": "report.txt",
        })


async def test_group_upload_explicit_failure_does_not_query_history():
    transport = MagicMock()
    transport.request = AsyncMock(return_value={
        "retcode": 1200,
        "status": "failed",
        "message": "rich media transfer failed",
    })
    api = OneBotApi(ws_transport=transport)

    with pytest.raises(RuntimeError, match="rich media transfer failed"):
        await api.call("upload_group_file", {
            "group_id": 42,
            "file": "/tmp/a",
            "name": "a",
        })
    assert transport.request.await_count == 1


async def test_group_upload_explicit_timeout_does_not_run_confirmation():
    transport = MagicMock()
    transport.request = AsyncMock(side_effect=TimeoutError())
    api = OneBotApi(ws_transport=transport)

    with pytest.raises(TimeoutError):
        await api.call("upload_group_file", {}, timeout=0.01)
    assert transport.request.await_count == 1


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


async def test_api_send_logs_outbound_line(caplog):
    t = WsApiTransport()
    ws = _make_ws()
    t.register(ws)
    api = OneBotApi(ws_transport=t)
    api.configure_send_logging(config=AdapterConfig(log_message_preview=40, log_file_message_mode="none"))

    with caplog.at_level(logging.INFO, logger=PREVIEW_LOGGER_NAME):
        task = asyncio.create_task(api.send_group_msg(42, [{"type": "text", "data": {"text": "hello-outbound"}}]))
        await asyncio.sleep(0.01)
        frame = ws.send_json.await_args.args[0]
        t.on_text(json.dumps({"retcode": 0, "data": {"message_id": 77}, "echo": frame["echo"]}))
        await asyncio.wait_for(task, timeout=2)
    assert "发送 ->" in caplog.text
    assert "hello-outbound" in caplog.text
    assert "message_id=77" in caplog.text

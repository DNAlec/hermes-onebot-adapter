"""Cascade reverse WS: unmatched group events out, downstream frames in."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import aiohttp.web
import pytest
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot.handler import OneBotHandler
from onebot_adapter.onebot.ws_api import WsApiTransport
from onebot_adapter.onebot.ws_cascade import _OUT_QUEUE_SIZE, CascadeWsServer
from onebot_adapter.relay.protocol import FilteredEvent, NormalizedEvent


def _cfg(**kw) -> AdapterConfig:
    base = dict(
        onebot_ws_token="t1",
        hermes_ws_token="t2",
        cascade_ws_enabled=True,
        cascade_ws_token="sekret",
        cascade_ws_path="/onebot",
        group_require_mention=True,
        self_id="999",
        dm_policy="allow",
    )
    base.update(kw)
    return AdapterConfig(**base)


def _group_raw(text: str = "hello", *, at: bool = False, user_id: int = 100) -> str:
    segs: list[dict] = []
    if at:
        segs.append({"type": "at", "data": {"qq": "999"}})
    segs.append({"type": "text", "data": {"text": text}})
    return json.dumps({
        "post_type": "message",
        "message_type": "group",
        "group_id": 42,
        "user_id": user_id,
        "message_id": 7,
        "time": 1700000000,
        "self_id": 999,
        "sender": {"nickname": "Tester", "user_id": user_id},
        "message": segs,
        "real_seq": "3",
    })


def _dm_raw(text: str = "hi") -> str:
    return json.dumps({
        "post_type": "message",
        "message_type": "private",
        "user_id": 100,
        "message_id": 8,
        "time": 1700000000,
        "self_id": 999,
        "sender": {"nickname": "Tester", "user_id": 100},
        "message": [{"type": "text", "data": {"text": text}}],
    })


class _RecordingCascade(CascadeWsServer):
    """Real cascade policy; records frames without requiring a live client."""

    def __init__(self, *, forward_meta: bool = True, **kw) -> None:
        super().__init__(_cfg(cascade_forward_meta=forward_meta, **kw))
        self.frames: list[str] = []

    def broadcast_raw(self, raw: str) -> bool:
        if not self._config.cascade_ws_enabled:
            return False
        self.frames.append(raw)
        return True


def _handler(*, cascade: _RecordingCascade | None = None, **kw) -> OneBotHandler:
    if cascade is not None:
        kw.setdefault("on_dropped", cascade.observe_dropped)
        kw.setdefault("on_ignored", cascade.observe_ignored)
    return OneBotHandler(label=kw.pop("label", "t"), **kw)


async def _receive_text(ws, timeout: float = 1.0) -> str:
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    assert msg.type == aiohttp.WSMsgType.TEXT, msg
    return msg.data


# ── Handler forwarding rules ─────────────────────────────────────────────


async def test_unmatched_group_is_cascaded():
    cascade = _RecordingCascade()
    handler = _handler(config=_cfg(), api=None, cascade=cascade)
    raw = _group_raw("hello", at=False)
    await handler.handle_event_text(raw)
    assert cascade.frames == [raw]


async def test_matched_group_is_not_cascaded():
    cascade = _RecordingCascade()
    events: list = []

    async def on_event(ev):
        events.append(ev)

    handler = _handler(config=_cfg(), api=None, cascade=cascade, on_event=on_event)
    await handler.handle_event_text(_group_raw("hello", at=True))
    assert cascade.frames == []
    assert len(events) == 1
    assert isinstance(events[0], NormalizedEvent)


async def test_matched_then_blacklist_not_cascaded():
    cascade = _RecordingCascade()
    filtered: list = []

    async def on_filtered(ev):
        filtered.append(ev)

    entry = MagicMock()
    entry.scope = "global"
    entry.reason = "spam"
    entry.expires_at = 9e9
    entry.to_dict.return_value = {"remaining": "1分钟"}
    handler = _handler(
        config=_cfg(), api=None, cascade=cascade,
        on_filtered=on_filtered,
        bot_blacklist_match_fn=lambda uid, gid: entry,
    )
    await handler.handle_event_text(_group_raw("hello", at=True))
    assert cascade.frames == []
    assert len(filtered) == 1
    assert isinstance(filtered[0], FilteredEvent)
    assert filtered[0].filter_type == "bot_blacklist"


async def test_matched_then_command_filter_not_cascaded():
    cascade = _RecordingCascade()
    filtered: list = []

    async def on_filtered(ev):
        filtered.append(ev)

    cfg = _cfg(command_filter_enabled=True, command_permissions={"stop": "admin"})
    handler = _handler(
        config=cfg, api=None, cascade=cascade,
        on_filtered=on_filtered,
        is_known_command_fn=lambda name: name == "stop",
        canonical_command_name_fn=lambda name: name,
    )
    await handler.handle_event_text(_group_raw("/stop", at=True))
    assert cascade.frames == []
    assert len(filtered) == 1
    assert filtered[0].command_name == "stop"


async def test_matched_then_user_filter_not_cascaded():
    cascade = _RecordingCascade()
    cfg = _cfg(groups={"42": {
        "group_id": "42",
        "group_user_filter_mode": "whitelist",
        "group_user_list": [],
    }})
    handler = _handler(config=cfg, api=None, cascade=cascade)
    await handler.handle_event_text(_group_raw("hello", at=True))
    assert cascade.frames == []


async def test_disabled_group_unmatched_not_cascaded():
    cascade = _RecordingCascade()
    cfg = _cfg(groups={"42": {"group_id": "42", "enabled": False}})
    handler = _handler(config=cfg, api=None, cascade=cascade)
    await handler.handle_event_text(_group_raw("hello", at=False))
    assert cascade.frames == []


async def test_private_message_not_cascaded():
    cascade = _RecordingCascade()
    events: list = []

    async def on_event(ev):
        events.append(ev)

    handler = _handler(config=_cfg(), api=None, cascade=cascade, on_event=on_event)
    await handler.handle_event_text(_dm_raw())
    assert cascade.frames == []
    assert len(events) == 1


async def test_all_group_messages_match_when_no_trigger():
    cascade = _RecordingCascade()
    events: list = []

    async def on_event(ev):
        events.append(ev)

    handler = _handler(
        config=_cfg(group_require_mention=False), api=None,
        cascade=cascade, on_event=on_event,
    )
    await handler.handle_event_text(_group_raw("hello", at=False))
    assert cascade.frames == []
    assert len(events) == 1


async def test_meta_event_forwarded_when_enabled():
    cascade = _RecordingCascade()
    handler = _handler(config=_cfg(), api=None, cascade=cascade)
    raw = json.dumps({"post_type": "meta_event", "meta_event_type": "heartbeat", "interval": 5000})
    await handler.handle_event_text(raw)
    assert cascade.frames == [raw]


async def test_meta_event_skipped_when_disabled():
    cascade = _RecordingCascade(forward_meta=False)
    handler = _handler(config=_cfg(cascade_forward_meta=False), api=None, cascade=cascade)
    raw = json.dumps({"post_type": "meta_event", "meta_event_type": "heartbeat"})
    await handler.handle_event_text(raw)
    assert cascade.frames == []


async def test_notice_not_cascaded():
    cascade = _RecordingCascade()
    handler = _handler(config=_cfg(), api=None, cascade=cascade)
    raw = json.dumps({"post_type": "notice", "notice_type": "group_increase", "user_id": 1, "group_id": 42})
    await handler.handle_event_text(raw)
    assert cascade.frames == []


# ── Auth ─────────────────────────────────────────────────────────────────


def _cascade_app(
    cfg: AdapterConfig, transport: WsApiTransport | None = None,
) -> tuple[aiohttp.web.Application, CascadeWsServer]:
    server = CascadeWsServer(cfg, ws_api_transport=transport)
    app = aiohttp.web.Application()
    server.add_routes(app)
    return app, server


async def test_cascade_empty_token_rejects():
    app, server = _cascade_app(_cfg(cascade_ws_token=""))
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/onebot")
    finally:
        await server.stop()
        await ts.close()


async def test_cascade_wrong_token_rejects():
    app, server = _cascade_app(_cfg())
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/onebot?token=wrong")
    finally:
        await server.stop()
        await ts.close()


async def test_cascade_token_accepted_and_unmatched_forwarded():
    cfg = _cfg()
    app, cascade = _cascade_app(cfg)
    handler = _handler(config=cfg, api=None, cascade=cascade)
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            async with client.ws_connect("/onebot?token=sekret") as ws:
                raw = _group_raw("hello", at=False)
                await handler.handle_event_text(raw)
                data = await _receive_text(ws)
                assert json.loads(data) == json.loads(raw)
    finally:
        await cascade.stop()
        await ts.close()


# ── Downstream → OneBot passthrough ──────────────────────────────────────


async def test_inbound_api_call_forwarded_and_response_routed():
    transport = WsApiTransport()
    onebot_ws = MagicMock()
    onebot_ws.send_str = AsyncMock()
    transport.register(onebot_ws)

    cfg = _cfg()
    app, cascade = _cascade_app(cfg, transport)
    handler = _handler(config=cfg, api=None, cascade=cascade, ws_api_transport=transport)
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            async with client.ws_connect("/onebot?token=sekret") as ws:
                lifecycle = json.loads(await _receive_text(ws))
                assert lifecycle["meta_event_type"] == "lifecycle"
                assert lifecycle["sub_type"] == "connect"
                frame = {"action": "send_group_msg", "params": {"group_id": 1, "message": "x"}, "echo": "e1"}
                await ws.send_str(json.dumps(frame))
                await asyncio.wait_for(_wait_send(onebot_ws), timeout=1)
                sent = onebot_ws.send_str.await_args.args[0]
                assert json.loads(sent) == frame

                response = json.dumps({"echo": "e1", "retcode": 0, "status": "ok", "data": {"message_id": 9}})
                assert handler.intercept_api_response(response) is True
                data = await _receive_text(ws)
                assert json.loads(data)["echo"] == "e1"
                assert json.loads(data)["retcode"] == 0
    finally:
        await cascade.stop()
        await ts.close()


async def test_adapter_pending_echo_not_stolen_by_cascade():
    transport = WsApiTransport()
    onebot_ws = MagicMock()
    onebot_ws.send_json = AsyncMock()
    onebot_ws.send_str = AsyncMock()
    transport.register(onebot_ws)

    handler = _handler(config=_cfg(), api=None, ws_api_transport=transport)
    task = asyncio.create_task(transport.request("get_login_info", {}))
    await asyncio.sleep(0.05)
    echo = onebot_ws.send_json.await_args.args[0]["echo"]
    raw = json.dumps({"echo": echo, "retcode": 0, "data": {"user_id": 1}})
    assert handler.intercept_api_response(raw) is True
    result = await asyncio.wait_for(task, timeout=1)
    assert result["retcode"] == 0


async def test_inbound_without_onebot_returns_error():
    cfg = _cfg()
    app, cascade = _cascade_app(cfg, WsApiTransport())
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            async with client.ws_connect("/onebot?token=sekret") as ws:
                await ws.send_str(json.dumps({"action": "get_status", "params": {}, "echo": "e2"}))
                data = json.loads(await _receive_text(ws))
                assert data["echo"] == "e2"
                assert data["retcode"] == -1
    finally:
        await cascade.stop()
        await ts.close()


async def test_inbound_rejected_when_disabled():
    transport = WsApiTransport()
    onebot_ws = MagicMock()
    onebot_ws.send_str = AsyncMock()
    transport.register(onebot_ws)
    cfg = _cfg()
    app, cascade = _cascade_app(cfg, transport)
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            async with client.ws_connect("/onebot?token=sekret") as ws:
                await _receive_text(ws)  # lifecycle
                cascade.update_config(_cfg(cascade_ws_enabled=False))
                await ws.send_str(json.dumps({"action": "get_status", "params": {}, "echo": "off"}))
                data = json.loads(await _receive_text(ws))
                assert data["echo"] == "off"
                assert data["retcode"] == -1
                assert onebot_ws.send_str.await_count == 0
    finally:
        await cascade.stop()
        await ts.close()


async def test_handshake_rejected_when_disabled():
    cfg = _cfg()
    app, cascade = _cascade_app(cfg)
    cascade.update_config(_cfg(cascade_ws_enabled=False))
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/onebot?token=sekret")
    finally:
        await cascade.stop()
        await ts.close()


async def test_second_client_replaces_first():
    cfg = _cfg()
    app, cascade = _cascade_app(cfg)
    handler = _handler(config=cfg, api=None, cascade=cascade)
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            async with client.ws_connect("/onebot?token=sekret") as ws1:
                raw = _group_raw("hello", at=False)
                await handler.handle_event_text(raw)
                assert json.loads(await _receive_text(ws1)) == json.loads(raw)
                async with client.ws_connect("/onebot?token=sekret") as ws2:
                    close_msg = await asyncio.wait_for(ws1.receive(), timeout=1)
                    assert close_msg.type in (
                        aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                    )
                    raw2 = _group_raw("again", at=False, user_id=101)
                    await handler.handle_event_text(raw2)
                    assert json.loads(await _receive_text(ws2)) == json.loads(raw2)
    finally:
        await cascade.stop()
        await ts.close()


async def test_cascade_echo_fails_when_its_onebot_socket_drops():
    transport = WsApiTransport()
    ws_a = MagicMock()
    ws_a.send_str = AsyncMock()
    transport.register(ws_a)
    cfg = _cfg()
    app, cascade = _cascade_app(cfg, transport)
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            async with client.ws_connect("/onebot?token=sekret") as ws:
                await _receive_text(ws)  # lifecycle
                await ws.send_str(json.dumps({"action": "get_status", "params": {}, "echo": "e-drop"}))
                await asyncio.wait_for(_wait_send(ws_a), timeout=1)
                ws_b = MagicMock()
                ws_b.send_str = AsyncMock()
                transport.register(ws_b)
                transport.unregister(ws_a)
                data = json.loads(await _receive_text(ws))
                assert data["echo"] == "e-drop"
                assert data["retcode"] == -1
    finally:
        await cascade.stop()
        await ts.close()


async def test_lifecycle_connect_skipped_when_meta_disabled():
    transport = WsApiTransport()
    onebot_ws = MagicMock()
    onebot_ws.send_str = AsyncMock()
    transport.register(onebot_ws)
    app, cascade = _cascade_app(
        _cfg(self_id="999", cascade_forward_meta=False), transport,
    )
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            async with client.ws_connect("/onebot?token=sekret") as ws:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.receive(), timeout=0.2)
    finally:
        await cascade.stop()
        await ts.close()


async def test_lifecycle_connect_when_onebot_already_up():
    transport = WsApiTransport()
    onebot_ws = MagicMock()
    onebot_ws.send_str = AsyncMock()
    transport.register(onebot_ws)
    app, cascade = _cascade_app(_cfg(self_id="999"), transport)
    ts = TestServer(app)
    await ts.start_server()
    try:
        async with TestClient(ts) as client:
            async with client.ws_connect("/onebot?token=sekret") as ws:
                data = json.loads(await _receive_text(ws))
                assert data["post_type"] == "meta_event"
                assert data["meta_event_type"] == "lifecycle"
                assert data["sub_type"] == "connect"
                assert data["self_id"] == 999
    finally:
        await cascade.stop()
        await ts.close()


async def test_disconnect_cancels_out_worker_when_queue_full():
    cascade = CascadeWsServer(_cfg())
    ws = MagicMock()
    ws.closed = False
    ws.send_str = AsyncMock()
    cascade._consumer = ws
    for i in range(_OUT_QUEUE_SIZE):
        assert cascade._enqueue_out(f"f{i}") is True
    assert cascade._enqueue_out("overflow") is False
    cascade._stop_out_worker()
    await asyncio.sleep(0.05)
    task = cascade._out_task
    assert task is None or task.done()
    cascade._consumer = None
    await cascade.stop()


async def _wait_send(ws: MagicMock) -> None:
    for _ in range(50):
        if ws.send_str.await_count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("send_str was not called")

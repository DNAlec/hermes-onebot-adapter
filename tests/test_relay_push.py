"""Tests for relay push_event and ring buffer replay (text-only, no media)."""
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
from onebot_adapter.relay.protocol import NormalizedEvent


def _make_relay():
    mock_api = MagicMock()
    cfg = AdapterConfig(hermes_ws_token="testtoken", hermes_ws_path="/hermes")
    relay = HermesRelayServer(cfg, mock_api, adapter_version="0.1.0-test", onebot_connected_fn=lambda: True)
    return relay, mock_api, cfg


def _make_event(text: str = "hello", msg_type: str = "text") -> NormalizedEvent:
    return NormalizedEvent(
        message_id="1",
        chat_id="100",
        chat_type="dm",
        user_id="u1",
        user_name="Alice",
        text=text,
        message_type=msg_type,  # type: ignore[arg-type]
        chat_name="Alice",
    )


async def test_push_event_text_only():
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)  # ready
                await relay.push_event(_make_event("hello"))
                msg = await ws.receive_json(timeout=2)
                assert msg["type"] == "event"
                assert msg["text"] == "hello"
                assert msg["chat_name"] == "Alice"
    finally:
        await server.close()


async def test_ring_buffer_replay_on_reconnect():
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        await relay.push_event(_make_event("msg1"))
        await relay.push_event(_make_event("msg2"))

        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                ready = await ws.receive_json(timeout=2)
                assert ready["type"] == "ready"
                msg1 = await ws.receive_json(timeout=2)
                assert msg1["type"] == "event"
                assert msg1["text"] == "msg1"
                msg2 = await ws.receive_json(timeout=2)
                assert msg2["type"] == "event"
                assert msg2["text"] == "msg2"
    finally:
        await server.close()


async def test_ring_buffer_evicts_old_events():
    relay, _, _ = _make_relay()
    buffer_size = HermesRelayServer._RING_BUFFER_SIZE
    for i in range(buffer_size + 10):
        await relay.push_event(_make_event(f"msg{i}"))
    assert len(relay._ring_buffer) == buffer_size
    first = relay._ring_buffer[0][1]
    assert first.text == f"msg{10}"


async def test_ring_buffer_skips_old_events_on_replay():
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        await relay.push_event(_make_event("fresh_msg"))
        old_event = _make_event("old_msg")
        relay._ring_buffer.appendleft((time.monotonic() - 60.0, old_event))

        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                ready = await ws.receive_json(timeout=2)
                assert ready["type"] == "ready"
                msg = await ws.receive_json(timeout=2)
                assert msg["type"] == "event"
                assert msg["text"] == "fresh_msg"
                with pytest.raises(asyncio.TimeoutError):
                    await ws.receive_json(timeout=0.5)
    finally:
        await server.close()


async def test_push_event_no_clients_no_error():
    relay, _, _ = _make_relay()
    await relay.push_event(_make_event("orphan"))
    assert len(relay._ring_buffer) == 1


async def test_ring_buffer_skips_slash_commands():
    relay, _, _ = _make_relay()
    await relay.push_event(_make_event("/restart"))
    await relay.push_event(_make_event("hello world"))
    await relay.push_event(_make_event("/stop"))
    assert len(relay._ring_buffer) == 1
    assert relay._ring_buffer[0][1].text == "hello world"


async def test_push_event_client_disconnects_drops_client():
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
            await asyncio.sleep(0.1)
        await relay.push_event(_make_event("after disconnect"))
    finally:
        await server.close()


async def test_replay_ring_buffer_all_ok_returns_true_unit():
    relay, _, _ = _make_relay()
    relay._ring_buffer.append((time.monotonic(), _make_event("ok1")))
    relay._ring_buffer.append((time.monotonic(), _make_event("ok2")))

    mock_ws = MagicMock()
    mock_ws.send_json = AsyncMock()

    result = await relay._replay_ring_buffer(mock_ws)
    assert result is True
    assert len(relay._ring_buffer) == 2


async def test_replay_ring_buffer_purges_bad_entry():
    """_replay_ring_buffer purges an entry whose send raises and returns False."""
    relay, _, _ = _make_relay()
    relay._ring_buffer.append((time.monotonic(), _make_event("good")))
    relay._ring_buffer.append((time.monotonic(), _make_event("bad")))
    assert len(relay._ring_buffer) == 2

    mock_ws = MagicMock()
    call_count = [0]

    async def failing_send_json(frame):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise TypeError("poisoned")

    mock_ws.send_json = failing_send_json

    result = await relay._replay_ring_buffer(mock_ws)
    assert result is False
    assert len(relay._ring_buffer) == 1
    assert relay._ring_buffer[0][1].text == "good"

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


def _make_event(text: str = "hello") -> NormalizedEvent:
    return NormalizedEvent(
        message_id="1",
        chat_id="100",
        chat_type="dm",
        user_id="u1",
        user_name="Alice",
        text=text,
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


async def test_event_ack_removes_replay_entry():
    relay, _, _ = _make_relay()
    event = _make_event("acked")
    await relay.push_event(event)
    assert len(relay._ring_buffer) == 1
    relay._handle_event_ack({"delivery_ids": [event.delivery_id]})
    assert not relay._ring_buffer


async def test_rpc_connection_does_not_replay_or_become_consumer():
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        await relay.push_event(_make_event("buffered"))
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken&role=rpc") as ws:
                assert (await ws.receive_json(timeout=2))["type"] == "ready"
                assert not relay.has_clients
                with pytest.raises(asyncio.TimeoutError):
                    await ws.receive_json(timeout=0.1)
    finally:
        await server.close()


async def test_all_consumer_sends_failed_reports_dropped():
    relay, _, _ = _make_relay()
    ws = MagicMock()
    ws.send_json = AsyncMock(side_effect=ConnectionError("closed"))
    relay._clients.add(ws)
    assert await relay.push_event(_make_event("lost")) == "dropped"


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
    """_replay_ring_buffer purges an entry whose broadcast raises and returns False.

    The replay now routes events through ``_enqueue_or_broadcast`` →
    ``_broadcast_event`` (broadcast to all clients in ``_clients``), so the
    failing client must be a member of ``_clients`` and its ``send_json``
    must raise.  When the broadcast drops the client, the replay detects the
    ws was removed from ``_clients`` and treats the entry as failed.
    """
    relay, _, _ = _make_relay()
    relay._ring_buffer.append((time.monotonic(), _make_event("good")))
    relay._ring_buffer.append((time.monotonic(), _make_event("bad")))
    assert len(relay._ring_buffer) == 2

    mock_ws = MagicMock()
    mock_ws.closed = False
    call_count = [0]

    async def failing_send_json(frame):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise TypeError("poisoned")

    mock_ws.send_json = failing_send_json
    # Register the mock client so _broadcast_event actually sends to it.
    relay._clients.add(mock_ws)

    result = await relay._replay_ring_buffer(mock_ws)
    assert result is False
    # The bad entry was purged; the good one remains.
    assert len(relay._ring_buffer) == 1
    assert relay._ring_buffer[0][1].text == "good"
    # The failing client was dropped from _clients by _broadcast_event.
    assert mock_ws not in relay._clients

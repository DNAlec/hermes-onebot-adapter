"""Tests for relay push_event with media payloads and ring buffer replay."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import aiohttp
import aiohttp.web
import pytest
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.config import AdapterConfig
from onebot_adapter.relay.hermes_ws import HermesRelayServer
from onebot_adapter.relay.protocol import (
    MediaDescriptor,
    MediaPayload,
    NormalizedEvent,
)


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


def _make_media(data: bytes = b"\x89PNGtest", mime: str = "image/png") -> MediaPayload:
    return MediaPayload(
        descriptor=MediaDescriptor(id="m1", mime=mime, name="test.png", size=len(data)),
        data=data,
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
                await relay.push_event(_make_event("hello"), [])
                msg = await ws.receive_json(timeout=2)
                assert msg["type"] == "event"
                assert msg["text"] == "hello"
                assert msg["chat_name"] == "Alice"
    finally:
        await server.close()


async def test_push_event_with_media():
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)  # ready
                media = _make_media(b"\x89PNGimagedata")
                event = _make_event("see photo", "photo")
                event.media_ids = [media.descriptor.id]
                event.media_types = [media.descriptor.mime]
                await relay.push_event(event, [media])
                # First frame: media descriptor
                media_msg = await ws.receive_json(timeout=2)
                assert media_msg["type"] == "media"
                assert media_msg["id"] == "m1"
                assert media_msg["mime"] == "image/png"
                # Second frame: binary
                bin_msg = await ws.receive(timeout=2)
                assert bin_msg.type == aiohttp.WSMsgType.BINARY
                assert bin_msg.data == b"\x89PNGimagedata"
                # Third frame: event
                event_msg = await ws.receive_json(timeout=2)
                assert event_msg["type"] == "event"
                assert event_msg["message_type"] == "photo"
                assert event_msg["media_ids"] == ["m1"]
                assert event_msg["media_types"] == ["image/png"]
    finally:
        await server.close()


async def test_ring_buffer_replay_on_reconnect():
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        # Push events while no client is connected
        await relay.push_event(_make_event("msg1"), [])
        await relay.push_event(_make_event("msg2"), [])

        # Now connect — should receive ready + replayed events
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                ready = await ws.receive_json(timeout=2)
                assert ready["type"] == "ready"
                # Should receive replayed events
                msg1 = await ws.receive_json(timeout=2)
                assert msg1["type"] == "event"
                assert msg1["text"] == "msg1"
                msg2 = await ws.receive_json(timeout=2)
                assert msg2["type"] == "event"
                assert msg2["text"] == "msg2"
    finally:
        await server.close()


async def test_ring_buffer_replay_with_media():
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        # Push event with media while no client connected
        media = _make_media(b"binarydata")
        await relay.push_event(_make_event("photo msg", "photo"), [media])

        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                ready = await ws.receive_json(timeout=2)
                assert ready["type"] == "ready"
                # Replayed media descriptor
                media_msg = await ws.receive_json(timeout=2)
                assert media_msg["type"] == "media"
                # Replayed binary
                bin_msg = await ws.receive(timeout=2)
                assert bin_msg.type == aiohttp.WSMsgType.BINARY
                assert bin_msg.data == b"binarydata"
                # Replayed event
                event_msg = await ws.receive_json(timeout=2)
                assert event_msg["type"] == "event"
                assert event_msg["text"] == "photo msg"
    finally:
        await server.close()


async def test_ring_buffer_evicts_old_events():
    """Ring buffer should keep only the last _RING_BUFFER_SIZE events."""
    relay, _, _ = _make_relay()
    buffer_size = HermesRelayServer._RING_BUFFER_SIZE
    # Push more events than the buffer can hold
    for i in range(buffer_size + 10):
        await relay.push_event(_make_event(f"msg{i}"), [])
    assert len(relay._ring_buffer) == buffer_size
    # The first events should have been evicted
    first = relay._ring_buffer[0][1]
    assert first.text == f"msg{10}"


async def test_ring_buffer_skips_old_events_on_replay():
    """Events older than _RING_BUFFER_MAX_AGE should not be replayed."""
    relay, _, _ = _make_relay()
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        # Push a fresh event and an "old" event by backdating its timestamp
        await relay.push_event(_make_event("fresh_msg"), [])
        # Manually inject an old entry (simulate event from 60s ago)
        old_event = _make_event("old_msg")
        relay._ring_buffer.appendleft((time.monotonic() - 60.0, old_event, []))

        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                ready = await ws.receive_json(timeout=2)
                assert ready["type"] == "ready"
                # Should receive only the fresh event; old one skipped
                msg = await ws.receive_json(timeout=2)
                assert msg["type"] == "event"
                assert msg["text"] == "fresh_msg"
                # No more messages should arrive (old event was filtered)
                with pytest.raises(asyncio.TimeoutError):
                    await ws.receive_json(timeout=0.5)
    finally:
        await server.close()


async def test_push_event_no_clients_no_error():
    """Pushing events when no client is connected should not raise."""
    relay, _, _ = _make_relay()
    media = _make_media(b"data")
    # Should not raise
    await relay.push_event(_make_event("orphan"), [media])
    assert len(relay._ring_buffer) == 1


async def test_ring_buffer_skips_slash_commands():
    """Slash commands must not enter the ring buffer (prevents /restart loop)."""
    relay, _, _ = _make_relay()
    await relay.push_event(_make_event("/restart"), [])
    await relay.push_event(_make_event("hello world"), [])
    await relay.push_event(_make_event("/stop"), [])
    # Only the non-command event should be buffered
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
            # WS is now closed
            await asyncio.sleep(0.1)
        # Push after client disconnected — should not raise
        await relay.push_event(_make_event("after disconnect"), [])
    finally:
        await server.close()

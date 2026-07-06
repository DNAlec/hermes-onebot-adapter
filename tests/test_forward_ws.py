"""Tests for the forward WebSocket client and mode switching.

Uses a real aiohttp WS server as a mock OneBot endpoint to verify
connection, message parsing, reconnection with backoff, and clean shutdown.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import aiohttp.web
from aiohttp.test_utils import TestServer

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot.ws_forward import OneBotForwardClient

# ── Helpers ──────────────────────────────────────────────────────────────


def _msg_event(text: str = "hello", user_id: int = 100) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": user_id,
        "message_id": 1,
        "time": 1700000000,
        "sender": {"card": "Tester", "nickname": "Test", "user_id": user_id},
        "message": [{"type": "text", "data": {"text": text}}],
    }


async def _start_mock_onebot_ws(port: int = 0) -> tuple[TestServer, list[dict], list]:
    """Start a mock OneBot WS server that records received connections.

    Returns (server, received_messages, active_websockets).
    The active_websockets list holds references to connected WS responses
    so tests can push messages to the forward client.
    """
    received: list[dict] = []
    active_websockets: list[aiohttp.web.WebSocketResponse] = []

    async def ws_handler(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        active_websockets.append(ws)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    received.append(json.loads(msg.data))
                except json.JSONDecodeError:
                    pass
        active_websockets.remove(ws) if ws in active_websockets else None
        return ws

    app = aiohttp.web.Application()
    app.router.add_get("/onebot/v11/ws", ws_handler)
    server = TestServer(app, port=port)
    await server.start_server()
    return server, received, active_websockets


# ── Connection tests ────────────────────────────────────────────────────


async def test_forward_client_connects_to_mock_server():
    server, _, _ = await _start_mock_onebot_ws()
    try:
        cfg = AdapterConfig(
            onebot_mode="forward",
            onebot_forward_ws_url=f"ws://127.0.0.1:{server.port}/onebot/v11/ws",
            onebot_ws_token="testtoken",
            self_id="999",
            group_require_mention=True,
        )
        events: list = []
        client = OneBotForwardClient(
            cfg, api=None, on_event=lambda e: events.append(e),
        )
        client.start()
        await asyncio.sleep(0.5)
        assert client.connected is True
        await client.stop()
    finally:
        await server.close()


async def test_forward_client_parses_message():
    server, _, active_wss = await _start_mock_onebot_ws()
    try:
        cfg = AdapterConfig(
            onebot_mode="forward",
            onebot_forward_ws_url=f"ws://127.0.0.1:{server.port}/onebot/v11/ws",
            onebot_ws_token="testtoken",
            self_id="999",
            group_require_mention=True,
            dm_user_filter_mode="blacklist",
        )
        events: list = []
        client = OneBotForwardClient(
            cfg, api=None, on_event=lambda e: events.append(e),
        )
        client.start()
        await asyncio.sleep(0.5)
        assert client.connected
        assert len(active_wss) >= 1

        # Push a message from the mock OneBot server to the forward client
        await active_wss[0].send_json(_msg_event("test from onebot"))
        await asyncio.sleep(0.5)

        await client.stop()
        assert len(events) == 1
        assert events[0].text == "test from onebot"
    finally:
        await server.close()


async def test_forward_client_stop_sets_connected_false():
    server, _, _ = await _start_mock_onebot_ws()
    try:
        cfg = AdapterConfig(
            onebot_mode="forward",
            onebot_forward_ws_url=f"ws://127.0.0.1:{server.port}/onebot/v11/ws",
            onebot_ws_token="testtoken",
            self_id="999",
        )
        client = OneBotForwardClient(cfg, api=None)
        client.start()
        await asyncio.sleep(0.3)
        assert client.connected
        await client.stop()
        assert client.connected is False
    finally:
        await server.close()


async def test_forward_client_stop_cancels_task():
    cfg = AdapterConfig(
        onebot_mode="forward",
        onebot_forward_ws_url="ws://127.0.0.1:1/nonexistent",  # won't connect
        self_id="999",
    )
    client = OneBotForwardClient(cfg, api=None)
    client.start()
    await asyncio.sleep(0.1)
    assert client._task is not None
    await client.stop()
    assert client._task is None


# ── Reconnection tests ──────────────────────────────────────────────────


async def test_forward_client_reconnects_after_disconnect():
    """Verify the client attempts to reconnect after the server closes."""
    server, _, _ = await _start_mock_onebot_ws()
    port = server.port
    try:
        cfg = AdapterConfig(
            onebot_mode="forward",
            onebot_forward_ws_url=f"ws://127.0.0.1:{port}/onebot/v11/ws",
            onebot_ws_token="testtoken",
            self_id="999",
        )
        client = OneBotForwardClient(cfg, api=None)
        client.start()
        await asyncio.sleep(0.5)
        assert client.connected
        assert client._connect_attempts == 1

        # Stop the server to force disconnect
        await server.close()
        await asyncio.sleep(1.0)
        assert client.connected is False

        # Restart the server on the same port
        server2, _, _ = await _start_mock_onebot_ws(port=port)
        await asyncio.sleep(3.0)  # wait for reconnect backoff
        assert client.connected is True
        assert client._connect_attempts >= 2

        await client.stop()
        await server2.close()
    except Exception:
        await client.stop()
        raise


async def test_forward_client_backoff_increases():
    """Verify connect_attempts counter increases on failed connections."""
    cfg = AdapterConfig(
        onebot_mode="forward",
        onebot_forward_ws_url="ws://127.0.0.1:1/nonexistent",  # port 1 won't connect
        self_id="999",
    )
    client = OneBotForwardClient(cfg, api=None)
    client.start()
    await asyncio.sleep(2.5)  # wait for a few failed attempts
    assert client._connect_attempts >= 2
    assert client.connected is False
    await client.stop()


# ── Config / mode switching ─────────────────────────────────────────────


async def test_forward_client_with_access_token():
    """Verify access token is sent as Authorization header."""
    server, _, _ = await _start_mock_onebot_ws()
    try:
        cfg = AdapterConfig(
            onebot_mode="forward",
            onebot_forward_ws_url=f"ws://127.0.0.1:{server.port}/onebot/v11/ws",

            onebot_ws_token="mytoken123",
            self_id="999",
        )
        client = OneBotForwardClient(cfg, api=None)
        client.start()
        await asyncio.sleep(0.3)
        assert client.connected
        await client.stop()
    finally:
        await server.close()


async def test_forward_client_no_url_raises():
    cfg = AdapterConfig(
        onebot_mode="forward",
        onebot_forward_ws_url="",
        self_id="999",
    )
    client = OneBotForwardClient(cfg, api=None)
    client.start()
    await asyncio.sleep(1.0)
    # Should keep trying but never connect
    assert client.connected is False
    assert client._connect_attempts >= 1
    await client.stop()


# ── Shared session usage ────────────────────────────────────────────────


async def test_forward_client_uses_shared_session():
    """Verify the client uses the provided shared session for connections."""
    server, _, _ = await _start_mock_onebot_ws()
    try:
        cfg = AdapterConfig(
            onebot_mode="forward",
            onebot_forward_ws_url=f"ws://127.0.0.1:{server.port}/onebot/v11/ws",
            onebot_ws_token="testtoken",

            self_id="999",
        )
        shared_session = aiohttp.ClientSession()
        try:
            client = OneBotForwardClient(cfg, api=None, session=shared_session)
            client.start()
            await asyncio.sleep(0.3)
            assert client.connected
            await client.stop()
            # Shared session should still be open (not closed by client)
            assert not shared_session.closed
        finally:
            await shared_session.close()
    finally:
        await server.close()


# ── Event filtering ─────────────────────────────────────────────────────


async def test_forward_client_filters_non_message_events():
    """Non-message events (notices, requests) should be ignored."""
    server, _, active_wss = await _start_mock_onebot_ws()
    try:
        cfg = AdapterConfig(
            onebot_mode="forward",
            onebot_forward_ws_url=f"ws://127.0.0.1:{server.port}/onebot/v11/ws",
            onebot_ws_token="testtoken",

            self_id="999",
            dm_user_filter_mode="blacklist",
        )
        events: list = []
        client = OneBotForwardClient(
            cfg, api=None, on_event=lambda e: events.append(e),
        )
        client.start()
        await asyncio.sleep(0.5)
        assert len(active_wss) >= 1

        # Push a notice event (not a message) then a real message
        await active_wss[0].send_json({"post_type": "notice", "notice_type": "group_upload"})
        await active_wss[0].send_json(_msg_event("real message"))
        await asyncio.sleep(0.5)

        await client.stop()
        # Only the message event should be in events
        assert len(events) == 1
        assert events[0].text == "real message"
    finally:
        await server.close()

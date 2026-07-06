"""Tests for the OneBotAdapter plugin WS client protocol handling.

These test the protocol logic without a real Hermes gateway or adapter service.
We mock the WS layer and verify frame construction, media buffering, and result
correlation.
"""
from __future__ import annotations

import asyncio
import json
import os

# Skip entire module if Hermes base isn't importable (standalone CI without Hermes)
import sys
from unittest.mock import MagicMock

import pytest

_HERMES_AGENT_DIR = os.environ.get("HERMES_AGENT_DIR", "/home/alec/.hermes/hermes-agent")
sys.path.insert(0, _HERMES_AGENT_DIR)

try:
    from gateway.config import Platform  # noqa: F401
    from gateway.platforms.base import BasePlatformAdapter, MessageType, SendResult  # noqa: F401
    _HERMES_OK = True
except Exception:
    _HERMES_OK = False

pytestmark = pytest.mark.skipif(not _HERMES_OK, reason="Hermes base not available")

if _HERMES_OK:
    from onebot_adapter.hermes_plugin.adapter import OneBotAdapter

    # Try to register the "onebot" platform so Platform("onebot") works.
    # In a real gateway this happens during plugin load; in tests we may be
    # running without the full registry, so guard against failure.
    try:
        from unittest.mock import MagicMock as _MagicMock

        from onebot_adapter.hermes_plugin.adapter import register as _ob_register
        _ctx = _MagicMock()
        _ob_register(_ctx)
    except Exception:
        pass


class FakeWS:
    """Minimal WS mock that records sent frames and can trigger result futures."""

    def __init__(self):
        self.closed = False
        self.sent: list[dict | bytes] = []
        self._handler = None

    async def send_json(self, msg: dict):
        self.sent.append(msg)
        # If this is a send/api_call, auto-resolve with a success result
        if msg.get("type") in ("send", "api_call"):
            req_id = msg.get("req_id", "")
            # Schedule the result on the next loop iteration
            def _resolve():
                fut = self._adapter._futures.pop(req_id, None)
                if fut and not fut.done():
                    fut.set_result({"success": True, "message_id": "mock_mid", "data": {"ok": True}})
            loop = asyncio.get_event_loop()
            loop.call_soon(_resolve)

    async def send_bytes(self, data: bytes):
        self.sent.append(data)

    async def close(self):
        self.closed = True


def _make_adapter():
    config = MagicMock()
    config.extra = {"adapter_url": "ws://127.0.0.1:18810/hermes", "adapter_token": "tok"}
    adapter = OneBotAdapter.__new__(OneBotAdapter)
    # Manual init without super().__init__ (which needs Platform)
    adapter._adapter_url = "ws://127.0.0.1:18810/hermes"
    adapter._adapter_token = "tok"
    adapter._session = None
    adapter._ws = None
    adapter._recv_task = None
    adapter._futures = {}
    adapter._onebot_connected = False
    adapter._self_id = ""
    adapter._is_connected = True
    adapter.MAX_MESSAGE_LENGTH = 4500
    # Platform("onebot") requires the platform to be registered; use a mock
    # fallback if registration didn't succeed (tests don't exercise platform enum).
    try:
        adapter.platform = Platform("onebot")
    except (ValueError, TypeError):
        adapter.platform = MagicMock()
    return adapter


async def test_send_text_constructs_frame():
    adapter = _make_adapter()
    fake_ws = FakeWS()
    fake_ws._adapter = adapter
    adapter._ws = fake_ws
    result = await adapter._request("send_text", chat_id="100", content="hello")
    assert result["success"] is True
    assert result["message_id"] == "mock_mid"
    sent = fake_ws.sent[0]
    assert sent["type"] == "send"
    assert sent["action"] == "send_text"
    assert sent["chat_id"] == "100"
    assert sent["content"] == "hello"


async def test_send_media_bytes_sends_two_frames():
    adapter = _make_adapter()
    fake_ws = FakeWS()
    fake_ws._adapter = adapter
    adapter._ws = fake_ws
    result = await adapter._send_media_bytes(
        "send_image", "group:42", b"\x89PNGdata", "image/jpeg", "test.jpg",
    )
    assert result["success"] is True
    # Should have sent: send_media (json), binary, send (json)
    assert isinstance(fake_ws.sent[0], dict)
    assert fake_ws.sent[0]["type"] == "send_media"
    assert isinstance(fake_ws.sent[1], bytes)
    assert fake_ws.sent[1] == b"\x89PNGdata"
    assert isinstance(fake_ws.sent[2], dict)
    assert fake_ws.sent[2]["type"] == "send"
    assert fake_ws.sent[2]["action"] == "send_image"
    assert fake_ws.sent[2]["media_id"] == fake_ws.sent[0]["id"]


async def test_api_call_constructs_frame():
    adapter = _make_adapter()
    fake_ws = FakeWS()
    fake_ws._adapter = adapter
    adapter._ws = fake_ws
    result = await adapter._api_call("get_group_info", {"group_id": 42})
    assert result["success"] is True
    sent = fake_ws.sent[0]
    assert sent["type"] == "api_call"
    assert sent["action"] == "get_group_info"
    assert sent["params"] == {"group_id": 42}


async def test_handle_text_ready_frame():
    adapter = _make_adapter()
    await adapter._handle_text(json.dumps({
        "type": "ready",
        "onebot_connected": True,
        "adapter_version": "0.1.0",
        "self_id": "123456",
    }))
    assert adapter._onebot_connected is True
    assert adapter._self_id == "123456"


async def test_handle_text_result_resolves_future():
    adapter = _make_adapter()
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    adapter._futures["r1"] = fut
    await adapter._handle_text(json.dumps({"type": "result", "req_id": "r1", "success": True, "message_id": "99"}))
    assert fut.done()
    assert fut.result()["message_id"] == "99"
    assert "r1" not in adapter._futures


async def test_request_timeout():
    adapter = _make_adapter()
    fake_ws = FakeWS()
    # Don't auto-resolve — override send_json to just record
    async def _silent_send(msg):
        fake_ws.sent.append(msg)
    fake_ws.send_json = _silent_send
    fake_ws.closed = False
    adapter._ws = fake_ws
    # Use a very short timeout
    import onebot_adapter.hermes_plugin.adapter as mod
    old = mod._RESULT_TIMEOUT
    mod._RESULT_TIMEOUT = 0.1
    try:
        result = await adapter._request("send_text", chat_id="100", content="hi")
    finally:
        mod._RESULT_TIMEOUT = old
    assert result["success"] is False
    assert "timeout" in result["error"]


async def test_request_ws_closed():
    adapter = _make_adapter()
    adapter._ws = None
    result = await adapter._request("send_text", chat_id="100", content="hi")
    assert result["success"] is False

"""Tests for the /api/health endpoint and standalone cron sender."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore

# ── Health endpoint ──────────────────────────────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(self_id="123"))
    service = AdapterService(store)
    app = service.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    yield TestClient(server)
    await server.close()


async def test_health_returns_ok(client):
    resp = await client.get("/api/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


async def test_health_no_auth_required(client):
    # Health endpoint should be accessible without any auth
    resp = await client.get("/api/health")
    assert resp.status == 200


# ── Standalone sender (WS-based) ──────────────────────────────────────────


async def test_standalone_send_success(tmp_path, monkeypatch):
    """Test that _standalone_send connects via WS and sends a text message."""
    import aiohttp.web

    from onebot_adapter.hermes_plugin.adapter import _standalone_send
    from onebot_adapter.relay.hermes_ws import HermesRelayServer

    # Start a real relay WS server (same as the adapter's hermes_app)
    mock_api = MagicMock()
    mock_api.send_private_msg = AsyncMock(return_value={"message_id": "s1"})
    cfg = AdapterConfig(hermes_ws_token="sendtoken", hermes_ws_path="/hermes")
    relay = HermesRelayServer(cfg, mock_api, adapter_version="t", onebot_connected_fn=lambda: True)
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()

    try:
        pconfig = MagicMock()
        pconfig.extra = {}
        monkeypatch.setenv("ONEBOT_ADAPTER_URL", f"ws://127.0.0.1:{server.port}/hermes")
        monkeypatch.setenv("ONEBOT_ADAPTER_TOKEN", "sendtoken")

        result = await _standalone_send(pconfig, "100", "cron message")
        assert result["success"] is True
        assert result["message_id"] == "s1"
        mock_api.send_private_msg.assert_awaited_once()
    finally:
        await server.close()


async def test_standalone_send_error_on_connection_failure(monkeypatch):
    """Test that _standalone_send returns error when adapter is unreachable."""
    from onebot_adapter.hermes_plugin.adapter import _standalone_send

    pconfig = MagicMock()
    pconfig.extra = {}
    monkeypatch.setenv("ONEBOT_ADAPTER_URL", "ws://127.0.0.1:1/hermes")
    monkeypatch.setenv("ONEBOT_ADAPTER_TOKEN", "tok")

    result = await _standalone_send(pconfig, "100", "msg")
    assert "error" in result
    assert "standalone send failed" in result["error"]


async def test_standalone_send_unauthorized(monkeypatch):
    """Test that _standalone_send fails with wrong token."""
    import aiohttp.web

    from onebot_adapter.hermes_plugin.adapter import _standalone_send
    from onebot_adapter.relay.hermes_ws import HermesRelayServer

    mock_api = MagicMock()
    cfg = AdapterConfig(hermes_ws_token="correcttoken", hermes_ws_path="/hermes")
    relay = HermesRelayServer(cfg, mock_api, adapter_version="t", onebot_connected_fn=lambda: True)
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()

    try:
        pconfig = MagicMock()
        pconfig.extra = {}
        monkeypatch.setenv("ONEBOT_ADAPTER_URL", f"ws://127.0.0.1:{server.port}/hermes")
        monkeypatch.setenv("ONEBOT_ADAPTER_TOKEN", "wrongtoken")

        result = await _standalone_send(pconfig, "100", "msg")
        assert "error" in result
    finally:
        await server.close()

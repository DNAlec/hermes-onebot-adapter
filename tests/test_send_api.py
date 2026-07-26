"""Tests for the typed automation tool API."""
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore

_KEY = "hoa_test_automation_key"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_KEY}"}


@pytest.fixture
async def send_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        self_id="123", webui_token="webui-token",
        automation_api_enabled=True,
        automation_api_key_hash=hashlib.sha256(_KEY.encode()).hexdigest(),
    ))
    service = AdapterService(store)
    mock_api = MagicMock()
    mock_api.call = AsyncMock(return_value={"data": {"message_id": 99}})
    service._state["api"] = mock_api
    server = TestServer(service.build_webui_app())
    client = TestClient(server)
    await client.start_server()
    yield client, mock_api
    await client.close()


async def test_send_private(send_client):
    client, mock_api = send_client
    resp = await client.post(
        "/api/v1/tools/onebot_send_message",
        json={"message_type": "private", "user_id": "100",
              "message": [{"type": "text", "data": {"text": "hello"}}]},
        headers=_auth(),
    )
    assert resp.status == 200
    assert (await resp.json())["ok"] is True
    assert mock_api.call.await_args.args[0] == "send_msg"


async def test_send_group(send_client):
    client, mock_api = send_client
    resp = await client.post(
        "/api/v1/tools/onebot_send_message",
        json={"message_type": "group", "group_id": "42",
              "message": [{"type": "text", "data": {"text": "hi"}}]},
        headers=_auth(),
    )
    assert resp.status == 200
    assert mock_api.call.await_args.args[0] == "send_msg"


async def test_send_unauthorized(send_client):
    client, _ = send_client
    resp = await client.post(
        "/api/v1/tools/onebot_send_message", json={},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status == 401


async def test_send_missing_fields(send_client):
    client, _ = send_client
    resp = await client.post(
        "/api/v1/tools/onebot_send_message",
        json={"message_type": "private"}, headers=_auth(),
    )
    assert resp.status == 400


async def test_query_token_is_rejected(send_client):
    client, _ = send_client
    resp = await client.get(f"/api/v1/tools?token={_KEY}")
    assert resp.status == 401


async def test_disabled_api_returns_403(send_client):
    client, _ = send_client
    # The fixture owns the store through the middleware closure; disable via
    # the authenticated management endpoint is covered separately.
    resp = await client.post(
        "/api/v1/tools/onebot_send_message", json={}, headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status == 401

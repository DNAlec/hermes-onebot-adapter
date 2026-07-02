"""Tests for the /api/send standalone delivery endpoint."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_session_token

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore

_TOKEN = "sendtoken"
_EPOCH = 0


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_session_token(_TOKEN, _EPOCH)}"}


@pytest.fixture
async def send_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        self_id="123",
        hermes_ws_token="sendtoken",
        webui_token="sendtoken",
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
    ))
    service = AdapterService(store)
    # Inject a mock api into state so /api/send can use it
    mock_api = MagicMock()
    mock_api.send_group_msg = AsyncMock(return_value={"message_id": "g99"})
    mock_api.send_private_msg = AsyncMock(return_value={"message_id": "p99"})
    service._state["api"] = mock_api
    app = service.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    yield TestClient(server), mock_api
    await server.close()


async def test_send_private(send_client):
    client, mock_api = send_client
    resp = await client.post(
        "/api/send",
        json={"chat_id": "100", "message": "hello"},
        headers=_auth(),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["success"] is True
    assert data["message_id"] == "p99"
    mock_api.send_private_msg.assert_awaited_once()
    args = mock_api.send_private_msg.await_args.args
    assert args[0] == 100


async def test_send_group(send_client):
    client, mock_api = send_client
    resp = await client.post(
        "/api/send",
        json={"chat_id": "group:42", "message": "hi"},
        headers=_auth(),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["success"] is True
    mock_api.send_group_msg.assert_awaited_once()
    assert mock_api.send_group_msg.await_args.args[0] == 42


async def test_send_unauthorized(send_client):
    client, _ = send_client
    resp = await client.post(
        "/api/send",
        json={"chat_id": "100", "message": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status == 401


async def test_send_missing_fields(send_client):
    client, _ = send_client
    resp = await client.post("/api/send", json={"chat_id": "100"}, headers=_auth())
    assert resp.status == 400


async def test_send_token_via_query(send_client):
    client, mock_api = send_client
    tok = make_session_token(_TOKEN, _EPOCH)
    resp = await client.post(f"/api/send?token={tok}", json={"chat_id": "100", "message": "hi"})
    assert resp.status == 200
    mock_api.send_private_msg.assert_awaited_once()

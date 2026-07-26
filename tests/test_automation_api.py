import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_session_token

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore

_WEBUI = "webui-secret"
_KEY = "hoa_automation-test"


@pytest.fixture
async def automation_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "config.json"))
    allowed = tmp_path / "uploads"
    allowed.mkdir()
    cfg = AdapterConfig(
        onebot_ws_token="onebot", hermes_ws_token="hermes", webui_token=_WEBUI,
        automation_api_enabled=True,
        automation_api_key_hash=hashlib.sha256(_KEY.encode()).hexdigest(),
        automation_upload_allowed_roots=[str(allowed)],
    )
    store = ConfigStore(cfg)
    service = AdapterService(store)
    api = MagicMock()
    api.call = AsyncMock(return_value={"data": {}})
    service._state["api"] = api
    client = TestClient(TestServer(service.build_webui_app()))
    await client.start_server()
    yield client, store, api, allowed
    await client.close()


def _key_auth():
    return {"Authorization": f"Bearer {_KEY}"}


def _webui_auth():
    token = make_session_token(_WEBUI, 0)
    return {"Authorization": f"Bearer {token}"}


async def test_catalog_exposes_all_tools(automation_client):
    client, _, _, _ = automation_client
    response = await client.get("/api/v1/tools", headers=_key_auth())
    assert response.status == 200
    names = {item["name"] for item in (await response.json())["tools"]}
    assert len(names) == 41
    assert "onebot_upload_file" in names


async def test_upload_file_from_allowed_root(automation_client):
    client, _, api, allowed = automation_client
    path = allowed / "done.zip"
    path.write_bytes(b"payload")
    response = await client.post(
        "/api/v1/tools/onebot_upload_file",
        json={"message_type": "group", "group_id": 42, "file": str(path), "name": "done.zip"},
        headers=_key_auth(),
    )
    assert response.status == 200
    assert api.call.await_args.args[0] == "upload_group_file"


async def test_upload_file_outside_root_is_rejected(automation_client, tmp_path):
    client, _, _, _ = automation_client
    path = tmp_path / "secret.txt"
    path.write_text("secret")
    response = await client.post(
        "/api/v1/tools/onebot_upload_file",
        json={"message_type": "private", "user_id": 7, "file": str(path)},
        headers=_key_auth(),
    )
    assert response.status == 403


async def test_api_disabled_even_with_valid_key(automation_client):
    client, store, _, _ = automation_client
    store.update(store.config.with_overrides(automation_api_enabled=False))
    response = await client.get("/api/v1/tools", headers=_key_auth())
    assert response.status == 403


async def test_rotate_and_revoke_key(automation_client):
    client, store, _, _ = automation_client
    rotated = await client.post("/api/v1/automation/key", headers=_webui_auth())
    assert rotated.status == 200
    raw = (await rotated.json())["api_key"]
    assert raw.startswith("hoa_")
    assert raw not in store.config.automation_api_key_hash
    revoked = await client.delete("/api/v1/automation/key", headers=_webui_auth())
    assert revoked.status == 200
    assert not store.config.automation_api_enabled
    assert not store.config.automation_api_key_hash


async def test_openapi_is_public_and_contains_tool_paths(automation_client):
    client, _, _, _ = automation_client
    response = await client.get("/api/v1/openapi.json")
    assert response.status == 200
    spec = await response.json()
    assert "/api/v1/tools/onebot_upload_file" in spec["paths"]


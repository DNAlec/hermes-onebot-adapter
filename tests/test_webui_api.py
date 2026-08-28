
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_session_token

from onebot_adapter import hermes_config as hc
from onebot_adapter.app import AdapterService
from onebot_adapter.bot_blacklist import BotBlacklistStore
from onebot_adapter.config import AdapterConfig, ConfigStore, config_path

_TOKEN = "secret"
_EPOCH = 0
_RAW_AUTH = {"Authorization": f"Bearer {_TOKEN}"}  # raw token, expected to be rejected in signed mode


def _auth() -> dict[str, str]:
    """Mint a fresh signed token and wrap it in an Authorization header."""
    return {"Authorization": f"Bearer {make_session_token(_TOKEN, _EPOCH)}"}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        self_id="123", onebot_ws_token="t1", hermes_ws_token="t2", webui_token=_TOKEN,
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
    ))
    service = AdapterService(store)
    app = service.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    yield TestClient(server)
    await server.close()


@pytest.fixture
async def tool_policy_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    hermes_dir = tmp_path / "hermes"
    hermes_dir.mkdir()
    (hermes_dir / "config.yaml").write_text(
        "provider: openai\nplugins:\n  entries:\n    onebot:\n      path: /plugin.py\n",
        encoding="utf-8",
    )
    store = ConfigStore(AdapterConfig(
        onebot_ws_token="t1", hermes_ws_token="t2", webui_token=_TOKEN,
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
        hermes_install_dir=str(hermes_dir),
    ))
    service = AdapterService(store)
    server = TestServer(service.build_webui_app())
    await server.start_server()
    web_client = TestClient(server)
    yield web_client, hermes_dir
    await web_client.close()
    await server.close()


async def test_status_endpoint_requires_auth(client):
    resp = await client.get("/api/v1/status")
    assert resp.status == 401


async def test_status_endpoint_with_token(client):
    resp = await client.get("/api/v1/status", headers=_auth())
    assert resp.status == 200
    data = await resp.json()
    assert data["adapter_version"]
    assert data["onebot_mode"] == "reverse"


async def test_config_get_put(client):
    got = await (await client.get("/api/v1/config", headers=_auth())).json()
    assert got["self_id"] == "123"
    assert got["file_upload_timeout"] == 600.0
    resp = await client.patch(
        "/api/v1/config",
        json={"self_id": "999", "seq_map_size": 100, "file_upload_timeout": 480},
        headers=_auth(),
    )
    assert resp.status == 200
    updated = await resp.json()
    assert updated["seq_map_size"] == 100
    assert updated["file_upload_timeout"] == 480


async def test_config_put_audit_records_direct_client_not_untrusted_xff(client):
    resp = await client.patch(
        "/api/v1/config",
        json={"seq_map_size": 101},
        headers={**_auth(), "X-Forwarded-For": "198.51.100.10"},
    )
    assert resp.status == 200

    audit_path = config_path().parent / "logs" / "config-audit.log"
    event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["reason"] == "webui.config_patch"
    assert event["http_method"] == "PATCH"
    assert event["http_path"] == "/api/v1/config"
    assert event["client_ip"] != "198.51.100.10"
    assert event["submitted_fields"] == ["seq_map_size"]


async def test_config_put_audit_uses_trusted_xff(client):
    resp = await client.patch(
        "/api/v1/config",
        json={"webui_trust_proxy_headers": True},
        headers=_auth(),
    )
    assert resp.status == 200
    resp = await client.patch(
        "/api/v1/config",
        json={"seq_map_size": 102},
        headers={**_auth(), "X-Forwarded-For": "198.51.100.11, 10.0.0.1"},
    )
    assert resp.status == 200

    audit_path = config_path().parent / "logs" / "config-audit.log"
    event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["client_ip"] == "198.51.100.11"


async def test_bot_blacklist_list_and_delete_api(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    service = AdapterService(ConfigStore(AdapterConfig(
        onebot_ws_token="t1", hermes_ws_token="t2", webui_token=_TOKEN,
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
    )))
    blacklist = BotBlacklistStore(tmp_path / "bot_blacklist.sqlite3")
    blacklist.start()
    entry = blacklist.set(
        scope="dm", user_id="100", duration_seconds=3600,
        reason="test", created_by_user_id="200",
    )
    service._state["bot_blacklist"] = blacklist
    server = TestServer(service.build_webui_app())
    await server.start_server()
    web_client = TestClient(server)
    try:
        assert (await web_client.get("/api/v1/bot_blacklist")).status == 401
        response = await web_client.get("/api/v1/bot_blacklist", headers=_auth())
        assert response.status == 200
        payload = await response.json()
        assert payload["entries"][0]["reason"] == "test"
        response = await web_client.delete(f"/api/v1/bot_blacklist/{entry.id}", headers=_auth())
        assert response.status == 200
        assert blacklist.list() == []
    finally:
        await web_client.close()
        await server.close()
        blacklist.close()


async def test_config_get_does_not_expose_webui_token(client):
    """GET /api/v1/config must not return the raw webui_token (login password)."""
    got = await (await client.get("/api/v1/config", headers=_auth())).json()
    assert "webui_token" not in got
    # Operational tokens remain visible — the user needs to copy them.
    assert got["onebot_ws_token"] == "t1"
    assert got["hermes_ws_token"] == "t2"


async def test_config_put_does_not_expose_webui_token(client):
    """PUT /api/v1/config must not leak webui_token in the response, and changing
    it still updates the value server-side (verified via /api/v1/auth/login)."""
    resp = await client.patch("/api/v1/config", json={"webui_token": "newsecret123"}, headers=_auth())
    assert resp.status == 200
    got = await resp.json()
    assert "webui_token" not in got
    # New token authenticates; old one no longer does.
    assert (await client.post("/api/v1/auth/login", json={"token": "newsecret123"})).status == 200
    assert (await client.post("/api/v1/auth/login", json={"token": _TOKEN})).status == 401


async def test_config_get_requires_auth(client):
    resp = await client.get("/api/v1/config")
    assert resp.status == 401


async def test_config_put_requires_auth(client):
    resp = await client.patch("/api/v1/config", json={"seq_map_size": 100})
    assert resp.status == 401


async def test_rate_limit_quota_api_query_reset_and_no_store(client, caplog):
    query = await client.get(
        "/api/v1/rate_limit/quota?scope=user&target_id=123", headers=_auth(),
    )
    assert query.status == 200
    assert query.headers["Cache-Control"] == "no-store"
    data = await query.json()
    assert data["scope"] == "user"
    assert data["target_id"] == "123"
    assert data["used"] == 0
    assert data["persistence"]["failure_mode"] == "memory_fallback"

    reset = await client.post(
        "/api/v1/rate_limit/quota/reset",
        json={"scope": "global"},
        headers=_auth(),
    )
    assert reset.status == 200
    assert reset.headers["Cache-Control"] == "no-store"
    assert (await reset.json())["cleared"] is False
    assert "rate-limit quota reset" in caplog.text


async def test_rate_limit_quota_api_validates_scope_and_target(client):
    missing = await client.get("/api/v1/rate_limit/quota?scope=user", headers=_auth())
    assert missing.status == 400
    non_numeric = await client.get(
        "/api/v1/rate_limit/quota?scope=group&target_id=abc", headers=_auth(),
    )
    assert non_numeric.status == 400
    global_target = await client.get(
        "/api/v1/rate_limit/quota?scope=global&target_id=1", headers=_auth(),
    )
    assert global_target.status == 400
    unauthorized = await client.get("/api/v1/rate_limit/quota?scope=global")
    assert unauthorized.status == 401


async def test_config_rejects_invalid(client):
    resp = await client.patch("/api/v1/config", json={"onebot_mode": "bogus"}, headers=_auth())
    assert resp.status == 400
    assert "onebot_mode" in (await resp.json())["error"]


async def test_config_rejects_invalid_outbound_filter_regex(client):
    resp = await client.patch(
        "/api/v1/config",
        json={"outbound_filter_enabled": True, "outbound_filter_patterns": ["("]},
        headers=_auth(),
    )
    assert resp.status == 400
    assert "outbound_filter_patterns" in (await resp.json())["error"]


async def test_index_placeholder_no_auth_needed(client):
    resp = await client.get("/")
    assert resp.status == 200
    txt = await resp.text()
    assert "Hermes OneBot Adapter" in txt


async def test_logs_endpoint_requires_auth(client):
    resp = await client.get("/api/v1/logs")
    assert resp.status == 401


async def test_logs_endpoint_with_token(client):
    resp = await client.get("/api/v1/logs", headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert "logs" in body
    assert body["source"] == "memory"
    assert body["memory_limit"] == 500
    assert "file_enabled" in body

    resp = await client.get("/api/v1/logs/file")
    assert resp.status == 401
    resp = await client.get("/api/v1/logs/file", headers=_auth())
    assert resp.status == 200
    file_body = await resp.json()
    assert file_body["source"] == "file"
    assert file_body["logs"] == []

    resp = await client.get("/api/v1/logs/file/download")
    assert resp.status == 401
    resp = await client.get("/api/v1/logs/file/download", headers=_auth())
    assert resp.status == 404


async def test_install_plugin_requires_auth(client):
    resp = await client.post("/api/v1/install_plugin", json={})
    assert resp.status == 401


async def test_install_plugin_endpoint(client, tmp_path):
    resp = await client.post(
        "/api/v1/install_plugin",
        json={"hermes_install_dir": str(tmp_path / "hermes")},
        headers=_auth(),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["adapter_version"]
    assert "plugin_dest" in data


async def test_groups_sync_requires_auth(client):
    resp = await client.post("/api/v1/groups/sync")
    assert resp.status == 401


async def test_groups_get_requires_auth(client):
    resp = await client.get("/api/v1/groups")
    assert resp.status == 401


async def test_commands_requires_auth(client):
    resp = await client.get("/api/v1/commands")
    assert resp.status == 401


async def test_tool_policy_endpoints_require_webui_auth(tool_policy_client):
    client, _ = tool_policy_client
    assert (await client.get("/api/v1/onebot_tool_policies")).status == 401
    assert (await client.put("/api/v1/onebot_tool_policies", json={})).status == 401
    assert (await client.post("/api/v1/onebot_tool_policies/reset")).status == 401


async def test_tool_policy_get_returns_tuple_catalog_defaults(tool_policy_client):
    client, _ = tool_policy_client
    response = await client.get("/api/v1/onebot_tool_policies", headers=_auth())
    assert response.status == 200
    payload = await response.json()
    assert payload["restart_required"] is True

    normal = next(item for item in payload["catalog"] if item["name"] == "onebot_get_login_info")
    admin = next(item for item in payload["catalog"] if item["name"] == "onebot_kick_group_member")
    hidden_names = {
        item["name"] for item in payload["catalog"] if item["default_registered"] is False
    }
    default_admin_names = {
        item["name"] for item in payload["catalog"] if item["default_permission"] == "admin"
    }
    assert normal["default_registered"] is True
    assert normal["default_permission"] == "everyone"
    assert normal["category"] == "基础"
    assert normal["schema"]["name"] == "onebot_get_login_info"
    assert admin["default_permission"] == "admin"
    assert payload["policies"][normal["name"]] == {"registered": True, "permission": "everyone"}
    assert payload["policies"][admin["name"]] == {"registered": True, "permission": "admin"}
    assert hidden_names == {
        "onebot_mark_msg_as_read",
        "onebot_get_recent_contact",
        "onebot_set_friend_remark",
        "onebot_set_group_remark",
        "onebot_create_flash_task",
        "onebot_send_flash_msg",
        "onebot_get_share_link",
        "onebot_get_fileset_id",
        "onebot_get_fileset_info",
        "onebot_get_flash_file_list",
        "onebot_get_flash_file_url",
        "onebot_download_fileset",
    }
    assert payload["policies"]["onebot_mark_msg_as_read"]["registered"] is False
    from onebot_adapter.hermes_plugin.onebot_tools import _DEFAULT_ADMIN_TOOL_NAMES
    assert default_admin_names == set(_DEFAULT_ADMIN_TOOL_NAMES)
    assert payload["sparse_policies"] == {}


def test_tool_policy_catalog_supports_structured_specs(monkeypatch):
    from onebot_adapter.hermes_plugin import onebot_tools
    from onebot_adapter.webui import routes

    spec = SimpleNamespace(
        name="onebot_future",
        schema={"name": "onebot_future", "description": "future"},
        default_registered=False,
        default_permission="admin",
        category="write",
        scope="group",
        caveat="dangerous",
    )
    monkeypatch.setattr(onebot_tools, "_TOOLS", [spec])
    assert routes._onebot_tool_catalog() == [{
        "name": "onebot_future",
        "schema": spec.schema,
        "default_registered": False,
        "default_permission": "admin",
        "category": "write",
        "scope": "group",
        "packet": False,
        "caveat": "dangerous",
    }]


async def test_tool_policy_put_writes_only_non_default_fields(tool_policy_client):
    client, hermes_dir = tool_policy_client
    response = await client.put(
        "/api/v1/onebot_tool_policies",
        json={"policies": {
            "onebot_get_login_info": {"registered": False, "permission": "everyone"},
            "onebot_kick_group_member": {"registered": True, "permission": "everyone"},
            "onebot_get_group_list": {"registered": True, "permission": "everyone"},
        }},
        headers=_auth(),
    )
    assert response.status == 200
    payload = await response.json()
    assert payload["sparse_policies"] == {
        "onebot_get_login_info": {"registered": False, "permission": "everyone"},
        "onebot_kick_group_member": {"registered": True, "permission": "everyone"},
    }
    assert hc.read_onebot_tool_policies(str(hermes_dir)) == payload["sparse_policies"]
    assert payload["policies"]["onebot_get_login_info"]["registered"] is False
    assert payload["policies"]["onebot_kick_group_member"]["permission"] == "everyone"

    config = hc.read_config(str(hermes_dir))
    assert config["provider"] == "openai"
    assert config["plugins"]["entries"]["onebot"]["path"] == "/plugin.py"


@pytest.mark.parametrize(
    "policies,error_text",
    [
        ({"unknown": {}}, "unknown tool"),
        ({"onebot_get_login_info": {"enabled": False}}, "unknown policy fields"),
        ({"onebot_get_login_info": {"registered": 1}}, "must be a boolean"),
        ({"onebot_get_login_info": {"permission": False}}, "must be everyone or admin"),
        ({"onebot_get_login_info": {"permission": "owner"}}, "must be everyone or admin"),
        ({"onebot_get_login_info": False}, "must be an object"),
    ],
)
async def test_tool_policy_put_rejects_invalid_policies(tool_policy_client, policies, error_text):
    client, hermes_dir = tool_policy_client
    response = await client.put(
        "/api/v1/onebot_tool_policies", json={"policies": policies}, headers=_auth()
    )
    assert response.status == 400
    assert error_text in (await response.json())["error"]
    assert hc.read_onebot_tool_policies(str(hermes_dir)) == {}


async def test_tool_policy_reset_removes_only_policy_subtree(tool_policy_client):
    client, hermes_dir = tool_policy_client
    hc.write_onebot_tool_policies(
        str(hermes_dir), {"onebot_get_login_info": {"registered": False}}
    )
    response = await client.post("/api/v1/onebot_tool_policies/reset", headers=_auth())
    assert response.status == 200
    assert (await response.json())["sparse_policies"] == {}
    config = hc.read_config(str(hermes_dir))
    assert "tool_policies" not in config["plugins"]["entries"]["onebot"]
    assert config["plugins"]["entries"]["onebot"]["path"] == "/plugin.py"
    assert config["provider"] == "openai"


async def test_tool_policy_put_does_not_report_disk_failure(tool_policy_client, monkeypatch):
    client, _ = tool_policy_client

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(hc, "write_onebot_tool_policies", fail_write)
    response = await client.put(
        "/api/v1/onebot_tool_policies",
        json={"onebot_get_login_info": {"registered": False}},
        headers=_auth(),
    )
    assert response.status == 500
    assert "disk full" in (await response.json())["error"]


async def test_tool_policy_reset_does_not_report_disk_failure(tool_policy_client, monkeypatch):
    client, _ = tool_policy_client

    def fail_reset(*args, **kwargs):
        raise OSError("read only")

    monkeypatch.setattr(hc, "reset_onebot_tool_policies", fail_reset)
    response = await client.post("/api/v1/onebot_tool_policies/reset", headers=_auth())
    assert response.status == 500
    assert "read only" in (await response.json())["error"]


async def test_send_requires_auth(client):
    resp = await client.post("/api/v1/tools/onebot_send_message", json={})
    assert resp.status == 403


async def test_send_does_not_fallback_to_hermes_ws_token(tmp_path, monkeypatch):
    """The Hermes WS token cannot authenticate the automation tool API."""
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        self_id="123",
        onebot_ws_token="t1",
        hermes_ws_token="hermes_tok",
        webui_token=_TOKEN,
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
    ))
    service = AdapterService(store)
    app = service.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    # Sending with hermes_ws_token as Bearer should NOT authenticate
    resp = await client.post(
        "/api/v1/tools/onebot_send_message",
        json={},
        headers={"Authorization": "Bearer hermes_tok"},
    )
    assert resp.status == 403
    await server.close()


@pytest.fixture
async def no_token_client(tmp_path, monkeypatch):
    """Client without webui_token set — ensures middleware still works."""
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        self_id="123", onebot_ws_token="t1", hermes_ws_token="t2", webui_token="",
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
    ))
    service = AdapterService(store)
    app = service.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    yield TestClient(server)
    await server.close()


async def test_health_endpoint_public_without_token(no_token_client):
    """Health endpoint is always public regardless of token config."""
    resp = await no_token_client.get("/api/v1/health")
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"


async def test_health_endpoint_public_with_token(client):
    resp = await client.get("/api/v1/health")
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"


# ── Signed session token (/api/v1/auth/login) tests ──────────────────────────────


@pytest.fixture
async def signed_client(tmp_path, monkeypatch):
    """Client with lifetime>0 (signed-token mode), epoch=0."""
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        self_id="123", onebot_ws_token="t1", hermes_ws_token="t2", webui_token=_TOKEN,
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
    ))
    service = AdapterService(store)
    app = service.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    yield TestClient(server)
    await server.close()


async def test_login_wrong_token(signed_client):
    resp = await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})
    assert resp.status == 401


async def test_login_returns_signed_token(signed_client):
    resp = await signed_client.post("/api/v1/auth/login", json={"token": _TOKEN})
    assert resp.status == 200
    data = await resp.json()
    assert "session_token" in data
    assert data["expires_in"] == 24 * 3600
    # The returned token should authenticate /api/v1/status
    auth = {"Authorization": f"Bearer {data['session_token']}"}
    r2 = await signed_client.get("/api/v1/status", headers=auth)
    assert r2.status == 200


async def test_signed_mode_rejects_raw_token(signed_client):
    """In signed mode the raw webui_token must not authenticate."""
    resp = await signed_client.get("/api/v1/status", headers=_RAW_AUTH)
    assert resp.status == 401


async def test_signed_token_expired(signed_client):
    """A token whose issued_at is older than lifetime_hours is rejected."""
    old = make_session_token(_TOKEN, _EPOCH, int(time.time()) - 25 * 3600)
    resp = await signed_client.get("/api/v1/status", headers={"Authorization": f"Bearer {old}"})
    assert resp.status == 401


async def test_signed_token_bad_signature(signed_client):
    """A token with a tampered HMAC is rejected."""
    bad = make_session_token("wrong-secret", _EPOCH, int(time.time()))
    resp = await signed_client.get("/api/v1/status", headers={"Authorization": f"Bearer {bad}"})
    assert resp.status == 401


async def test_signed_token_garbage(signed_client):
    """Non-base64 / malformed tokens are rejected without crashing."""
    resp = await signed_client.get("/api/v1/status", headers={"Authorization": "Bearer !!!notb64!!!"})
    assert resp.status == 401


async def test_lifetime_below_minimum_rejected(tmp_path, monkeypatch):
    """lifetime=0 is no longer valid (minimum is 1) — config validation rejects it."""
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    cfg = AdapterConfig(
        self_id="123", onebot_ws_token="t1", hermes_ws_token="t2", webui_token=_TOKEN,
        webui_token_lifetime_hours=0,
    )
    assert "webui_token_lifetime_hours" in "; ".join(cfg.validate())


async def test_changing_lifetime_invalidates_old_sessions(tmp_path, monkeypatch):
    """Bumping lifetime via PUT /api/v1/config invalidates old signed tokens."""
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        self_id="123", onebot_ws_token="t1", hermes_ws_token="t2", webui_token=_TOKEN,
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
    ))
    service = AdapterService(store)
    app = service.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)

    # Login → get a signed token
    resp = await client.post("/api/v1/auth/login", json={"token": _TOKEN})
    session_tok = (await resp.json())["session_token"]
    auth = {"Authorization": f"Bearer {session_tok}"}
    assert (await client.get("/api/v1/status", headers=auth)).status == 200

    # Change lifetime → epoch should bump → old token invalid
    resp = await client.patch("/api/v1/config", json={"webui_token_lifetime_hours": 48}, headers=auth)
    assert resp.status == 200
    # webui_token_epoch is internal state, not exposed in the API response;
    # verify it bumped by reading the store directly.
    assert store.config.webui_token_epoch == _EPOCH + 1

    # Old session token no longer works
    assert (await client.get("/api/v1/status", headers=auth)).status == 401

    # Re-login works with the new epoch
    resp = await client.post("/api/v1/auth/login", json={"token": _TOKEN})
    new_tok = (await resp.json())["session_token"]
    new_auth = {"Authorization": f"Bearer {new_tok}"}
    assert (await client.get("/api/v1/status", headers=new_auth)).status == 200
    await server.close()


async def test_login_endpoint_no_auth_required(signed_client):
    """/api/v1/auth/login is exempt from the auth middleware (public login endpoint)."""
    resp = await signed_client.post("/api/v1/auth/login", json={"token": _TOKEN})
    assert resp.status == 200


# ── Rate limiting tests ──────────────────────────────────────────────────


async def test_login_rate_limit_blocks_after_5_failures(signed_client):
    """5 failed logins from the same IP → 6th attempt returns 429."""
    for _ in range(5):
        resp = await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})
        assert resp.status == 401
    resp = await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})
    assert resp.status == 429
    body = await resp.json()
    assert "retry_after" in body


async def test_login_rate_limit_resets_on_success(signed_client):
    """A successful login clears the failure counter for that IP."""
    for _ in range(4):
        assert (await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})).status == 401
    # 5th attempt with correct token succeeds and resets the counter
    assert (await signed_client.post("/api/v1/auth/login", json={"token": _TOKEN})).status == 200
    # After reset, 5 more failures should be allowed before ban
    for _ in range(5):
        assert (await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})).status == 401
    # 6th failure → 429
    assert (await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})).status == 429


async def test_login_rate_limit_banned_ip_rejects_even_correct_token(signed_client):
    """Once banned, even the correct token returns 429 (not 401)."""
    for _ in range(5):
        await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})
    resp = await signed_client.post("/api/v1/auth/login", json={"token": _TOKEN})
    assert resp.status == 429


async def test_login_rate_limit_unblock_after_window(signed_client, monkeypatch):
    """After the ban window elapses, the IP is unbanned."""
    import onebot_adapter.webui.routes as routes

    # Speed up time: fake "now" advances past the ban window.
    fake_now = [time.time()]

    def fake_time():
        return fake_now[0]

    monkeypatch.setattr(routes.time, "time", fake_time)

    for _ in range(5):
        assert (await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})).status == 401
    assert (await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})).status == 429

    # Advance past the ban window + the GC threshold.
    fake_now[0] += routes._LOGIN_BAN_SECONDS + 1

    # The IP's stale entry should be garbage-collected on the next request,
    # and a correct token should now succeed.
    resp = await signed_client.post("/api/v1/auth/login", json={"token": _TOKEN})
    assert resp.status == 200


async def test_login_rate_limit_different_ips_independent(signed_client):
    """Rate-limit ban on /api/v1/auth/login does not affect already-authenticated API calls.

    The rate limiter only applies to /api/v1/auth/login; a banned IP can still use a
    valid signed session token to call other endpoints (e.g. /api/v1/status)."""
    # Exhaust the login limit for this IP.
    for _ in range(5):
        await signed_client.post("/api/v1/auth/login", json={"token": "wrong"})
    # Banned on /api/v1/auth/login
    assert (await signed_client.post("/api/v1/auth/login", json={"token": _TOKEN})).status == 429
    # But an existing valid signed token still works on other endpoints.
    assert (await signed_client.get("/api/v1/status", headers=_auth())).status == 200


async def test_xff_ignored_by_default(signed_client):
    """When webui_trust_proxy_headers=False (default), X-Forwarded-For is
    ignored — a spoofed XFF header cannot bypass the rate limit by appearing
    as a new IP each time."""
    for _ in range(5):
        resp = await signed_client.post(
            "/api/v1/auth/login",
            json={"token": "wrong"},
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        assert resp.status == 401
    # 6th attempt with a *different* spoofed XFF → still banned (same real IP)
    resp = await signed_client.post(
        "/api/v1/auth/login",
        json={"token": "wrong"},
        headers={"X-Forwarded-For": "10.0.0.2"},
    )
    assert resp.status == 429


async def test_xff_trusted_when_configured(tmp_path, monkeypatch):
    """When webui_trust_proxy_headers=True, X-Forwarded-For is used for rate
    limiting, so different XFF values count as different IPs."""
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        self_id="123", onebot_ws_token="t1", hermes_ws_token="t2", webui_token=_TOKEN,
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
        webui_trust_proxy_headers=True,
    ))
    service = AdapterService(store)
    app = service.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    try:
        # 5 failures with one XFF IP → banned for that XFF
        for _ in range(5):
            assert (await client.post(
                "/api/v1/auth/login", json={"token": "wrong"},
                headers={"X-Forwarded-For": "10.0.0.1"},
            )).status == 401
        assert (await client.post(
            "/api/v1/auth/login", json={"token": "wrong"},
            headers={"X-Forwarded-For": "10.0.0.1"},
        )).status == 429
        # A different XFF IP is not banned
        assert (await client.post(
            "/api/v1/auth/login", json={"token": _TOKEN},
            headers={"X-Forwarded-For": "10.0.0.2"},
        )).status == 200
    finally:
        await server.close()

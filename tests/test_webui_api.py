
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_session_token

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore

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


async def test_status_endpoint_requires_auth(client):
    resp = await client.get("/api/status")
    assert resp.status == 401


async def test_status_endpoint_with_token(client):
    resp = await client.get("/api/status", headers=_auth())
    assert resp.status == 200
    data = await resp.json()
    assert data["adapter_version"]
    assert data["onebot_mode"] == "reverse"


async def test_config_get_put(client):
    got = await (await client.get("/api/config", headers=_auth())).json()
    assert got["self_id"] == "123"
    resp = await client.put("/api/config", json={"self_id": "999", "seq_map_size": 100}, headers=_auth())
    assert resp.status == 200
    assert (await resp.json())["seq_map_size"] == 100


async def test_config_get_does_not_expose_webui_token(client):
    """GET /api/config must not return the raw webui_token (login password)."""
    got = await (await client.get("/api/config", headers=_auth())).json()
    assert "webui_token" not in got
    # Operational tokens remain visible — the user needs to copy them.
    assert got["onebot_ws_token"] == "t1"
    assert got["hermes_ws_token"] == "t2"


async def test_config_put_does_not_expose_webui_token(client):
    """PUT /api/config must not leak webui_token in the response, and changing
    it still updates the value server-side (verified via /api/login)."""
    resp = await client.put("/api/config", json={"webui_token": "newsecret123"}, headers=_auth())
    assert resp.status == 200
    got = await resp.json()
    assert "webui_token" not in got
    # New token authenticates; old one no longer does.
    assert (await client.post("/api/login", json={"token": "newsecret123"})).status == 200
    assert (await client.post("/api/login", json={"token": _TOKEN})).status == 401


async def test_config_get_requires_auth(client):
    resp = await client.get("/api/config")
    assert resp.status == 401


async def test_config_put_requires_auth(client):
    resp = await client.put("/api/config", json={"seq_map_size": 100})
    assert resp.status == 401


async def test_config_rejects_invalid(client):
    resp = await client.put("/api/config", json={"onebot_mode": "bogus"}, headers=_auth())
    assert resp.status == 400
    assert "onebot_mode" in (await resp.json())["error"]


async def test_index_placeholder_no_auth_needed(client):
    resp = await client.get("/")
    assert resp.status == 200
    txt = await resp.text()
    assert "Hermes OneBot Adapter" in txt


async def test_logs_endpoint_requires_auth(client):
    resp = await client.get("/api/logs")
    assert resp.status == 401


async def test_logs_endpoint_with_token(client):
    resp = await client.get("/api/logs", headers=_auth())
    assert resp.status == 200
    assert "logs" in await resp.json()


async def test_install_plugin_requires_auth(client):
    resp = await client.post("/api/install_plugin", json={})
    assert resp.status == 401


async def test_install_plugin_endpoint(client, tmp_path):
    resp = await client.post(
        "/api/install_plugin",
        json={"hermes_install_dir": str(tmp_path / "hermes")},
        headers=_auth(),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["adapter_version"]
    assert "plugin_dest" in data


async def test_groups_sync_requires_auth(client):
    resp = await client.post("/api/groups/sync")
    assert resp.status == 401


async def test_groups_get_requires_auth(client):
    resp = await client.get("/api/groups")
    assert resp.status == 401


async def test_commands_requires_auth(client):
    resp = await client.get("/api/commands")
    assert resp.status == 401


async def test_send_requires_auth(client):
    resp = await client.post("/api/send", json={"chat_id": "group:1", "message": "hi"})
    assert resp.status == 401


async def test_send_does_not_fallback_to_hermes_ws_token(tmp_path, monkeypatch):
    """Only webui_token authenticates /api/send — no hermes_ws_token fallback."""
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
        "/api/send",
        json={"chat_id": "group:1", "message": "hi"},
        headers={"Authorization": "Bearer hermes_tok"},
    )
    assert resp.status == 401
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
    resp = await no_token_client.get("/api/health")
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"


async def test_health_endpoint_public_with_token(client):
    resp = await client.get("/api/health")
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"


# ── Signed session token (/api/login) tests ──────────────────────────────


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
    resp = await signed_client.post("/api/login", json={"token": "wrong"})
    assert resp.status == 401


async def test_login_returns_signed_token(signed_client):
    resp = await signed_client.post("/api/login", json={"token": _TOKEN})
    assert resp.status == 200
    data = await resp.json()
    assert "session_token" in data
    assert data["expires_in"] == 24 * 3600
    # The returned token should authenticate /api/status
    auth = {"Authorization": f"Bearer {data['session_token']}"}
    r2 = await signed_client.get("/api/status", headers=auth)
    assert r2.status == 200


async def test_signed_mode_rejects_raw_token(signed_client):
    """In signed mode the raw webui_token must not authenticate."""
    resp = await signed_client.get("/api/status", headers=_RAW_AUTH)
    assert resp.status == 401


async def test_signed_token_expired(signed_client):
    """A token whose issued_at is older than lifetime_hours is rejected."""
    old = make_session_token(_TOKEN, _EPOCH, int(time.time()) - 25 * 3600)
    resp = await signed_client.get("/api/status", headers={"Authorization": f"Bearer {old}"})
    assert resp.status == 401


async def test_signed_token_bad_signature(signed_client):
    """A token with a tampered HMAC is rejected."""
    bad = make_session_token("wrong-secret", _EPOCH, int(time.time()))
    resp = await signed_client.get("/api/status", headers={"Authorization": f"Bearer {bad}"})
    assert resp.status == 401


async def test_signed_token_garbage(signed_client):
    """Non-base64 / malformed tokens are rejected without crashing."""
    resp = await signed_client.get("/api/status", headers={"Authorization": "Bearer !!!notb64!!!"})
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
    """Bumping lifetime via PUT /api/config invalidates old signed tokens."""
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
    resp = await client.post("/api/login", json={"token": _TOKEN})
    session_tok = (await resp.json())["session_token"]
    auth = {"Authorization": f"Bearer {session_tok}"}
    assert (await client.get("/api/status", headers=auth)).status == 200

    # Change lifetime → epoch should bump → old token invalid
    resp = await client.put("/api/config", json={"webui_token_lifetime_hours": 48}, headers=auth)
    assert resp.status == 200
    # webui_token_epoch is internal state, not exposed in the API response;
    # verify it bumped by reading the store directly.
    assert store.config.webui_token_epoch == _EPOCH + 1

    # Old session token no longer works
    assert (await client.get("/api/status", headers=auth)).status == 401

    # Re-login works with the new epoch
    resp = await client.post("/api/login", json={"token": _TOKEN})
    new_tok = (await resp.json())["session_token"]
    new_auth = {"Authorization": f"Bearer {new_tok}"}
    assert (await client.get("/api/status", headers=new_auth)).status == 200
    await server.close()


async def test_login_endpoint_no_auth_required(signed_client):
    """/api/login is exempt from the auth middleware (public login endpoint)."""
    resp = await signed_client.post("/api/login", json={"token": _TOKEN})
    assert resp.status == 200


# ── Rate limiting tests ──────────────────────────────────────────────────


async def test_login_rate_limit_blocks_after_5_failures(signed_client):
    """5 failed logins from the same IP → 6th attempt returns 429."""
    for _ in range(5):
        resp = await signed_client.post("/api/login", json={"token": "wrong"})
        assert resp.status == 401
    resp = await signed_client.post("/api/login", json={"token": "wrong"})
    assert resp.status == 429
    body = await resp.json()
    assert "retry_after" in body


async def test_login_rate_limit_resets_on_success(signed_client):
    """A successful login clears the failure counter for that IP."""
    for _ in range(4):
        assert (await signed_client.post("/api/login", json={"token": "wrong"})).status == 401
    # 5th attempt with correct token succeeds and resets the counter
    assert (await signed_client.post("/api/login", json={"token": _TOKEN})).status == 200
    # After reset, 5 more failures should be allowed before ban
    for _ in range(5):
        assert (await signed_client.post("/api/login", json={"token": "wrong"})).status == 401
    # 6th failure → 429
    assert (await signed_client.post("/api/login", json={"token": "wrong"})).status == 429


async def test_login_rate_limit_banned_ip_rejects_even_correct_token(signed_client):
    """Once banned, even the correct token returns 429 (not 401)."""
    for _ in range(5):
        await signed_client.post("/api/login", json={"token": "wrong"})
    resp = await signed_client.post("/api/login", json={"token": _TOKEN})
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
        assert (await signed_client.post("/api/login", json={"token": "wrong"})).status == 401
    assert (await signed_client.post("/api/login", json={"token": "wrong"})).status == 429

    # Advance past the ban window + the GC threshold.
    fake_now[0] += routes._LOGIN_BAN_SECONDS + 1

    # The IP's stale entry should be garbage-collected on the next request,
    # and a correct token should now succeed.
    resp = await signed_client.post("/api/login", json={"token": _TOKEN})
    assert resp.status == 200


async def test_login_rate_limit_different_ips_independent(signed_client):
    """Rate-limit ban on /api/login does not affect already-authenticated API calls.

    The rate limiter only applies to /api/login; a banned IP can still use a
    valid signed session token to call other endpoints (e.g. /api/status)."""
    # Exhaust the login limit for this IP.
    for _ in range(5):
        await signed_client.post("/api/login", json={"token": "wrong"})
    # Banned on /api/login
    assert (await signed_client.post("/api/login", json={"token": _TOKEN})).status == 429
    # But an existing valid signed token still works on other endpoints.
    assert (await signed_client.get("/api/status", headers=_auth())).status == 200


async def test_xff_ignored_by_default(signed_client):
    """When webui_trust_proxy_headers=False (default), X-Forwarded-For is
    ignored — a spoofed XFF header cannot bypass the rate limit by appearing
    as a new IP each time."""
    for _ in range(5):
        resp = await signed_client.post(
            "/api/login",
            json={"token": "wrong"},
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        assert resp.status == 401
    # 6th attempt with a *different* spoofed XFF → still banned (same real IP)
    resp = await signed_client.post(
        "/api/login",
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
                "/api/login", json={"token": "wrong"},
                headers={"X-Forwarded-For": "10.0.0.1"},
            )).status == 401
        assert (await client.post(
            "/api/login", json={"token": "wrong"},
            headers={"X-Forwarded-For": "10.0.0.1"},
        )).status == 429
        # A different XFF IP is not banned
        assert (await client.post(
            "/api/login", json={"token": _TOKEN},
            headers={"X-Forwarded-For": "10.0.0.2"},
        )).status == 200
    finally:
        await server.close()

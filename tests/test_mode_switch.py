"""Tests for config hot-reload: transport mode switching via WebUI."""
from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_session_token

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore

_WT = "webtest"
_EPOCH = 0


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        onebot_reverse_ws_port=28830,
        hermes_ws_port=28831,
        webui_port=28832,
        self_id="123",
        hermes_ws_token="modetest",
        onebot_ws_token="obtest",
        onebot_mode="reverse",
    ))
    svc = AdapterService(store)
    svc._session = aiohttp.ClientSession()  # inject session manually
    svc._init_components()
    yield svc
    if svc._onebot_forward:
        await svc._onebot_forward.stop()
    if svc._session and not svc._session.closed:
        await svc._session.close()


async def test_config_change_callback_registered(service):
    """The on_change callback should be registered after hermes startup."""
    # Simulate what _on_hermes_startup does
    relay = service.build_hermes_app()
    # Manually call the startup hook
    await service._on_hermes_startup(relay)
    # The callback should be in the store's listeners
    assert len(service.store._listeners) >= 1
    await service._on_hermes_cleanup(relay)


async def test_mode_switch_reverse_to_forward_starts_forward_client(service):
    """Switching from reverse to forward should start the forward client."""

    relay = service.build_hermes_app()
    await service._on_hermes_startup(relay)
    try:
        assert service._onebot_forward is not None
        assert service._onebot_forward._task is None  # not started in reverse mode

        # Simulate config change to forward mode
        old = service.store.config
        new = old.with_overrides(onebot_mode="forward", onebot_forward_ws_url="ws://127.0.0.1:1/test")
        await service._on_config_change(old, new)

        # Forward client should now be started
        assert service._onebot_forward._task is not None
    finally:
        await service._on_hermes_cleanup(relay)


async def test_mode_switch_forward_to_reverse_stops_forward_client(service):
    """Switching from forward to reverse should stop the forward client."""

    # Start in forward mode
    service.store.patch(onebot_mode="forward", onebot_forward_ws_url="ws://127.0.0.1:1/test")
    relay = service.build_hermes_app()
    await service._on_hermes_startup(relay)
    try:
        assert service._onebot_forward._task is not None

        # Simulate config change to reverse mode
        old = service.store.config
        new = old.with_overrides(onebot_mode="reverse")
        await service._on_config_change(old, new)

        # Forward client should be stopped
        assert service._onebot_forward._task is None
    finally:
        await service._on_hermes_cleanup(relay)


async def test_mode_no_change_does_nothing(service):
    """If mode doesn't change, the callback should be a no-op."""

    relay = service.build_hermes_app()
    await service._on_hermes_startup(relay)
    try:
        old = service.store.config
        new = old.with_overrides(log_message_preview=10)  # different field, same mode
        await service._on_config_change(old, new)
        # No exception, no transport changes
        assert service._onebot_forward._task is None  # still reverse mode
    finally:
        await service._on_hermes_cleanup(relay)


async def test_log_level_hot_reload(service):
    """Changing log_level should update root logger + WebUI handler level."""
    import logging

    relay = service.build_hermes_app()
    await service._on_hermes_startup(relay)
    try:
        old = service.store.config
        # Set initial level to INFO
        logging.getLogger().setLevel(logging.INFO)
        new = old.with_overrides(log_level="DEBUG")
        await service._on_config_change(old, new)
        # Root logger level should now be DEBUG
        assert logging.getLogger().level == logging.DEBUG
    finally:
        await service._on_hermes_cleanup(relay)
        logging.getLogger().setLevel(logging.INFO)


async def test_rapid_config_updates_converge_to_latest(service):
    first = service.store.config.with_overrides(log_message_preview=10)
    service.store.update(first)
    second = first.with_overrides(log_message_preview=20)
    service.store.update(second)
    third = second.with_overrides(log_message_preview=30)
    service.store.update(third)
    for _ in range(5):
        await asyncio.sleep(0)
    assert service._relay is not None
    assert service._relay._config.log_message_preview == 30
    assert service._applied_config.log_message_preview == 30


async def test_webui_put_config_triggers_mode_switch(tmp_path, monkeypatch):
    """PUT /api/config with a new onebot_mode should trigger the on_change callback."""
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        onebot_reverse_ws_port=28840,
        hermes_ws_port=28841,
        webui_port=28842,
        self_id="123",
        hermes_ws_token="webtest",
        onebot_ws_token="obtest",
        onebot_mode="reverse",
        webui_token=_WT,
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
    ))
    svc = AdapterService(store)
    svc._session = aiohttp.ClientSession()
    svc._init_components()
    app = svc.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    try:
        # Register the on_change callback (as _on_hermes_startup would)
        callbacks_fired: list = []
        store.on_change(lambda old, new: callbacks_fired.append((old.onebot_mode, new.onebot_mode)))

        async with TestClient(server) as client:
            auth = {"Authorization": f"Bearer {make_session_token(_WT, _EPOCH)}"}
            resp = await client.put(
                "/api/config",
                json={"onebot_mode": "forward", "onebot_forward_ws_url": "ws://127.0.0.1:1/t"},
                headers=auth,
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["onebot_mode"] == "forward"

        # The on_change callback should have been called
        assert len(callbacks_fired) >= 1
        assert callbacks_fired[-1] == ("reverse", "forward")
    finally:
        await server.close()
        if svc._onebot_forward:
            await svc._onebot_forward.stop()
        if svc._session and not svc._session.closed:
            await svc._session.close()

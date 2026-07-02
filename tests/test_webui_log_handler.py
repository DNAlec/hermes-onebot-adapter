"""Tests for the WebUI log handler and status endpoint with port fields."""
from __future__ import annotations

import logging
from collections import deque

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_session_token

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore
from onebot_adapter.webui.log_handler import WebUILogHandler, attach_log_handler

_WT = "logtest"
_EPOCH = 0


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_session_token(_WT, _EPOCH)}"}

# ── WebUILogHandler ──────────────────────────────────────────────────────


def test_log_handler_buffers_records():
    buf: deque = deque(maxlen=100)
    handler = WebUILogHandler(buf)
    logger = logging.getLogger("test_log_handler_buffers")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("hello world")
    logger.error("something failed")
    assert len(buf) == 2
    assert "hello world" in buf[0]
    assert "ERROR" in buf[1]
    assert "something failed" in buf[1]
    logger.removeHandler(handler)


def test_log_handler_respects_maxlen():
    buf: deque = deque(maxlen=3)
    handler = WebUILogHandler(buf)
    logger = logging.getLogger("test_maxlen")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    handler.setLevel(logging.DEBUG)
    for i in range(5):
        logger.info("msg %d", i)
    assert len(buf) == 3
    assert "msg 4" in buf[-1]
    assert "msg 0" not in buf[0]
    logger.removeHandler(handler)


def test_log_handler_format_contains_timestamp():
    buf: deque = deque(maxlen=10)
    handler = WebUILogHandler(buf)
    logger = logging.getLogger("test_format")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("test message")
    assert len(buf) == 1
    # Format: "YYYY-MM-DD HH:MM:SS,mmm LEVEL name: message"
    assert ":" in buf[0]
    assert "INFO" in buf[0]
    assert "test_format" in buf[0]
    logger.removeHandler(handler)


def test_attach_log_handler_returns_handler():
    state: dict = {}
    handler = attach_log_handler(state, level="DEBUG")
    assert isinstance(handler, WebUILogHandler)
    assert "log_buffer" in state
    assert handler.level == logging.DEBUG
    # Clean up
    logging.getLogger().removeHandler(handler)


# ── Status endpoint with port fields ─────────────────────────────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        self_id="123",
        onebot_reverse_ws_port=18800,
        hermes_ws_port=18810,
        webui_port=18820,
        onebot_ws_token="t1",
        hermes_ws_token="t2",
        webui_token=_WT,
        webui_token_lifetime_hours=24, webui_token_epoch=_EPOCH,
    ))
    service = AdapterService(store)
    app = service.build_webui_app()
    server = TestServer(app)
    await server.start_server()
    yield TestClient(server)
    await server.close()


async def test_status_includes_all_ports(client):
    resp = await client.get("/api/status", headers=_auth())
    assert resp.status == 200
    data = await resp.json()
    assert data["onebot_ws_port"] == 18800
    assert data["hermes_ws_port"] == 18810
    assert data["webui_port"] == 18820
    assert data["adapter_version"]
    assert data["onebot_mode"] == "reverse"
    assert data["self_id"] == "123"


async def test_logs_endpoint_returns_list(client):
    resp = await client.get("/api/logs", headers=_auth())
    assert resp.status == 200
    data = await resp.json()
    assert "logs" in data
    assert isinstance(data["logs"], list)

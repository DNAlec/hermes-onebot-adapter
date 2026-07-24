"""Tests for the WebUI log handler and status endpoint with port fields."""
from __future__ import annotations

import logging
from collections import deque

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_session_token

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore
from onebot_adapter.onebot.log_format import log_recv_line
from onebot_adapter.relay.protocol import NormalizedEvent
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


def test_full_message_copy_does_not_propagate_to_webui(tmp_path):
    cfg = AdapterConfig(
        log_file_enabled=True,
        log_file_dir=str(tmp_path),
        log_message_preview=5,
        log_file_message_mode="full",
    )
    service = AdapterService(ConfigStore(cfg))
    state: dict = {}
    web_handler = attach_log_handler(state)
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    service._setup_file_logging(cfg)
    try:
        event = NormalizedEvent(
            message_id="m1",
            chat_id="42",
            chat_type="dm",
            user_id="42",
            user_name="tester",
            text="1234567890",
        )
        log_recv_line(event, preview=5, file_message_mode="full")
        web_lines = list(state["log_buffer"])
        assert len(web_lines) == 1
        assert "12345..." in web_lines[0]
        assert "1234567890" not in web_lines[0]
        file_text = (tmp_path / "adapter.log").read_text(encoding="utf-8")
        assert "1234567890" in file_text
        assert "12345..." not in file_text
    finally:
        root.removeHandler(web_handler)
        root.setLevel(old_level)
        assert service._file_handler is not None
        logging.getLogger("onebot_adapter").removeHandler(service._file_handler)
        logging.getLogger("onebot_adapter.file").removeHandler(service._file_handler)
        service._file_handler.close()


def test_file_log_rotates_at_size_limit(tmp_path):
    cfg = AdapterConfig(
        log_file_enabled=True,
        log_file_dir=str(tmp_path),
        log_file_max_bytes=1024,
    )
    service = AdapterService(ConfigStore(cfg))
    package_logger = logging.getLogger("onebot_adapter.rotation_test")
    old_level = package_logger.level
    package_logger.setLevel(logging.INFO)
    service._setup_file_logging(cfg)
    try:
        for index in range(20):
            package_logger.info("rotation-record-%d %s", index, "x" * 120)
        assert (tmp_path / "adapter.log").exists()
        assert list(tmp_path.glob("adapter.log.*"))
    finally:
        package_logger.setLevel(old_level)
        assert service._file_handler is not None
        logging.getLogger("onebot_adapter").removeHandler(service._file_handler)
        logging.getLogger("onebot_adapter.file").removeHandler(service._file_handler)
        service._file_handler.close()


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

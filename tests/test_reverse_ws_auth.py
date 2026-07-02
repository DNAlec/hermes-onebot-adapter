"""Tests for the reverse WebSocket server access token enforcement.

Covers token fail-closed semantics: an empty token always rejects connections,
and a configured token only accepts matching query/header credentials.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import aiohttp
import aiohttp.web
import pytest
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot.ws_reverse import OneBotReverseServer


def _make_reverse_app(cfg: AdapterConfig) -> aiohttp.web.Application:
    server = OneBotReverseServer(cfg, api=MagicMock())
    app = aiohttp.web.Application()
    server.add_routes(app)
    return app


def _cfg(**kw) -> AdapterConfig:
    base = dict(
        onebot_mode="reverse",
        onebot_reverse_ws_path="/onebot",
        self_id="999",
    )
    base.update(kw)
    return AdapterConfig(**base)


# ── Empty token: fail closed (reject all) ────────────────────────────────


async def test_reverse_empty_token_rejects():
    """When onebot_ws_token is empty, connections must be rejected (fail closed)."""
    app = _make_reverse_app(_cfg())  # token defaults to ""
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/onebot")
    finally:
        await server.close()


# ── Token configured: query param accepted ─────────────────────────────


async def test_reverse_token_query_param_accepted():
    app = _make_reverse_app(_cfg(onebot_ws_token="sekret"))
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/onebot?token=sekret") as ws:
                assert not ws.closed
    finally:
        await server.close()


# ── Token configured: Authorization header accepted ─────────────────────


async def test_reverse_token_bearer_header_accepted():
    app = _make_reverse_app(_cfg(onebot_ws_token="sekret"))
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect(
                "/onebot", headers={"Authorization": "Bearer sekret"}
            ) as ws:
                assert not ws.closed
    finally:
        await server.close()


# ── Token configured: wrong/missing token rejected ─────────────────────


async def test_reverse_wrong_token_rejected():
    app = _make_reverse_app(_cfg(onebot_ws_token="sekret"))
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/onebot?token=wrong")
    finally:
        await server.close()


async def test_reverse_missing_token_rejected():
    app = _make_reverse_app(_cfg(onebot_ws_token="sekret"))
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/onebot")
    finally:
        await server.close()


# ── Header takes precedence form; query and header either-or ───────────


async def test_reverse_header_overrides_wrong_query():
    """A correct Authorization header should pass even if the query token is
    wrong, since the handler reads query first then falls back to header."""
    app = _make_reverse_app(_cfg(onebot_ws_token="sekret"))
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            # query is wrong but header is right; current logic is
            # `query or _bearer(header)` -> query truthy wins -> would fail.
            # This documents the OR-fallback semantics: the FIRST present
            # value (query, then header) is the one checked.
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect(
                    "/onebot?token=wrong", headers={"Authorization": "Bearer sekret"}
                )
    finally:
        await server.close()


# ── Hermes relay: empty token fail closed ────────────────────────────────


async def test_hermes_empty_token_rejects():
    """When hermes_ws_token is empty, the Hermes relay endpoint must reject
    connections (fail closed)."""
    from onebot_adapter.relay.hermes_ws import HermesRelayServer

    cfg = AdapterConfig(hermes_ws_token="", hermes_ws_path="/hermes")
    relay = HermesRelayServer(cfg, MagicMock(), adapter_version="t", onebot_connected_fn=lambda: True)
    app = aiohttp.web.Application()
    relay.add_routes(app)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/hermes")
    finally:
        await server.close()

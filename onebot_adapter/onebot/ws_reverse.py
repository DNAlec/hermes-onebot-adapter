"""Reverse WebSocket server: OneBot dials out to this endpoint."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import aiohttp
import aiohttp.web

from onebot_adapter._async_utils import token_matches, ws_presented_token
from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot.handler import OneBotEventDispatcher, OneBotHandler
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.seq_map import SeqMap
from onebot_adapter.onebot.ws_api import WsApiTransport

logger = logging.getLogger(__name__)


class OneBotReverseServer:
    """Hosts a WebSocket endpoint that OneBot connects to."""

    def __init__(
        self,
        config: AdapterConfig,
        api: Any,
        on_event: Callable[[Any], Any] | None = None,
        on_connect: Callable[[], Any] | None = None,
        on_disconnect: Callable[[], Any] | None = None,
        on_filtered: Callable[[Any], Any] | None = None,
        is_known_command_fn: Callable[[str], bool] | None = None,
        canonical_command_name_fn: Callable[[str], str] | None = None,
        seq_map: SeqMap | None = None,
        name_resolver: NameResolver | None = None,
        ws_api_transport: WsApiTransport | None = None,
        bot_blacklist_match_fn: Callable[[str, str | None], Any] | None = None,
        friend_cache: Any | None = None,
    ) -> None:
        self._config = config
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._ws_api_transport = ws_api_transport
        self._active: set[aiohttp.web.WebSocketResponse] = set()
        self.connected = False
        self._handler = OneBotHandler(
            label="reverse",
            config=config,
            api=api,
            on_event=on_event,
            on_filtered=on_filtered,
            is_known_command_fn=is_known_command_fn,
            canonical_command_name_fn=canonical_command_name_fn,
            seq_map=seq_map,
            name_resolver=name_resolver,
            ws_api_transport=ws_api_transport,
            bot_blacklist_match_fn=bot_blacklist_match_fn,
            friend_cache=friend_cache,
        )
        self._event_dispatcher = OneBotEventDispatcher(self._handler, label="reverse")

    def update_config(self, config: AdapterConfig) -> None:
        """Hot-reload config without rebuilding the server (route stays bound)."""
        self._config = config
        self._handler.update_config(config)

    def add_routes(self, app: aiohttp.web.Application) -> None:
        app.router.add_get(self._config.onebot_reverse_ws_path, self._handler_endpoint)

    async def _handler_endpoint(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        token = ws_presented_token(request, query_keys=("access_token", "token"))
        if not token_matches(token, self._config.onebot_ws_token):
            logger.warning("OneBot reverse WS unauthorized remote=%s", request.remote)
            return aiohttp.web.json_response({"error": "unauthorized"}, status=401)
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        self._active.add(ws)
        if self._ws_api_transport is not None:
            self._ws_api_transport.register(ws)
        self.connected = True
        if self._on_connect:
            self._on_connect()
        logger.info("OneBot reverse WS connected from %s", request.remote)
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._event_dispatcher.dispatch(msg.data, ws)
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        finally:
            self._active.discard(ws)
            if self._ws_api_transport is not None:
                self._ws_api_transport.unregister(ws)
            if not self._active:
                self.connected = False
                if self._on_disconnect:
                    self._on_disconnect()
            logger.info(
                "OneBot reverse WS disconnected close_code=%s exception=%r",
                ws.close_code, ws.exception(),
            )
        return ws

    async def stop(self) -> None:
        for ws in list(self._active):
            await ws.close()
        self._active.clear()
        self.connected = False
        await self._event_dispatcher.stop()

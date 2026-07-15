"""Forward WebSocket client: adapter dials out to OneBot's WS server.

Uses exponential backoff with jitter for reconnection.  The shared
``aiohttp.ClientSession`` from the service is reused for all connections.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any

import aiohttp

from onebot_adapter._async_utils import log_task_exception as _log_task_exc
from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot.handler import OneBotHandler
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.seq_map import SeqMap
from onebot_adapter.onebot.ws_api import WsApiTransport

logger = logging.getLogger(__name__)

_INITIAL_DELAY = 1.0
_MAX_DELAY = 30.0


class OneBotForwardClient:
    """Connects to a OneBot OneBot 11 forward WS endpoint with backoff reconnect."""

    def __init__(
        self,
        config: AdapterConfig,
        api: Any,
        on_event: Callable[[Any], Any] | None = None,
        on_connect: Callable[[], Any] | None = None,
        on_disconnect: Callable[[], Any] | None = None,
        session: aiohttp.ClientSession | None = None,
        on_filtered: Callable[[Any], Any] | None = None,
        is_known_command_fn: Callable[[str], bool] | None = None,
        canonical_command_name_fn: Callable[[str], str] | None = None,
        seq_map: SeqMap | None = None,
        name_resolver: NameResolver | None = None,
        ws_api_transport: WsApiTransport | None = None,
    ) -> None:
        self._config = config
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._session = session
        self._ws_api_transport = ws_api_transport
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self._connect_attempts = 0
        self._text_tasks: set[asyncio.Task] = set()
        self._handler = OneBotHandler(
            label="forward",
            config=config,
            api=api,
            on_event=on_event,
            on_filtered=on_filtered,
            is_known_command_fn=is_known_command_fn,
            canonical_command_name_fn=canonical_command_name_fn,
            seq_map=seq_map,
            name_resolver=name_resolver,
            ws_api_transport=ws_api_transport,
        )

    def update_config(self, config: AdapterConfig) -> None:
        """Hot-reload config. The reconnect loop picks up the new token/URL
        on the next connection attempt; callers may stop()+start() to force
        an immediate reconnect with the fresh handshake token.
        """
        self._config = config
        self._handler._config = config

    def start(self) -> asyncio.Task[None]:
        self._stop.clear()
        self._connect_attempts = 0
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("OneBot forward WS: error during stop")
        self._task = None
        self.connected = False
        for task in list(self._text_tasks):
            task.cancel()
        if self._text_tasks:
            await asyncio.gather(*self._text_tasks, return_exceptions=True)
        self._text_tasks.clear()
        logger.info("OneBot forward WS client stopped")

    async def _run(self) -> None:
        delay = _INITIAL_DELAY
        while not self._stop.is_set():
            self._connect_attempts += 1
            try:
                await self._connect_once()
                delay = _INITIAL_DELAY  # reset on clean disconnect
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._connect_attempts <= 3 or self._connect_attempts % 10 == 0:
                    logger.warning(
                        "OneBot forward WS connect failed (attempt %d): %s",
                        self._connect_attempts, exc,
                    )
            finally:
                self.connected = False

            if self._stop.is_set():
                break

            # Exponential backoff with jitter
            jitter = random.uniform(0, delay * 0.3)
            wait = delay + jitter
            logger.debug("OneBot forward WS reconnecting in %.1fs", wait)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except TimeoutError:
                pass
            delay = min(_MAX_DELAY, delay * 2)

    async def _connect_once(self) -> None:
        if not self._config.onebot_forward_ws_url:
            raise ValueError("onebot_forward_ws_url is not configured")

        headers: dict[str, str] = {}
        if not self._config.onebot_ws_token:
            raise ValueError("onebot_ws_token must not be empty")
        headers["Authorization"] = f"Bearer {self._config.onebot_ws_token}"

        # Use shared session if available, otherwise create a temporary one
        if self._session and not self._session.closed:
            ws = await self._session.ws_connect(
                self._config.onebot_forward_ws_url, headers=headers, heartbeat=30,
            )
            try:
                await self._serve_ws(ws)
            finally:
                await ws.close()
        else:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.ws_connect(
                    self._config.onebot_forward_ws_url, heartbeat=30,
                ) as ws:
                    await self._serve_ws(ws)

    async def _serve_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.connected = True
        if self._ws_api_transport is not None:
            self._ws_api_transport.register(ws)
        if self._on_connect:
            self._on_connect()
        logger.info(
            "OneBot forward WS connected to %s (attempt %d)",
            self._config.onebot_forward_ws_url, self._connect_attempts,
        )
        try:
            async for msg in ws:
                if self._stop.is_set():
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    task = asyncio.create_task(self._handler.handle_text(msg.data))
                    self._text_tasks.add(task)
                    task.add_done_callback(self._text_tasks.discard)
                    task.add_done_callback(_log_task_exc)
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        finally:
            self.connected = False
            if self._ws_api_transport is not None:
                self._ws_api_transport.unregister(ws)
            if self._on_disconnect:
                self._on_disconnect()
            logger.info("OneBot forward WS disconnected")

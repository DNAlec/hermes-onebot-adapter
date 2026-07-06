"""Forward WebSocket client: adapter dials out to OneBot's WS server.

Uses exponential backoff with jitter for reconnection.  The shared
``aiohttp.ClientSession`` from the service is reused for all connections
and media downloads.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from typing import Any

import aiohttp

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot.log_format import log_recv_line
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.parser import parse_event
from onebot_adapter.onebot.seq_map import SeqMap, _seq_map_add
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
        self._api = api
        self._on_event = on_event
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._session = session
        self._on_filtered = on_filtered
        self._is_known_command_fn = is_known_command_fn
        self._canonical_command_name_fn = canonical_command_name_fn
        self._seq_map = seq_map
        self._ws_api_transport = ws_api_transport
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self._connect_attempts = 0
        self._text_tasks: set[asyncio.Task] = set()
        self._name_resolver = name_resolver or NameResolver(api)

    def update_config(self, config: AdapterConfig) -> None:
        """Hot-reload config. The reconnect loop picks up the new token/URL
        on the next connection attempt; callers may stop()+start() to force
        an immediate reconnect with the fresh handshake token.
        """
        self._config = config

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
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self.connected = False
        # Cancel and await in-flight _handle_text tasks.
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
                    task = asyncio.create_task(self._handle_text(msg.data))
                    self._text_tasks.add(task)
                    task.add_done_callback(self._text_tasks.discard)
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

    async def _handle_text(self, raw: str) -> None:
        # 先检查是否是 WS API 的响应帧（命中 echo 的 pending 请求），若是则
        # 由 WsApiTransport resolve 对应 future 并结束，不进 parser 流程。
        if self._ws_api_transport is not None and self._ws_api_transport.on_text(raw):
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("OneBot forward: non-JSON frame ignored")
            return
        logger.debug("OneBot forward recv raw: %s", raw[:2000])
        # 在 parser 之前存 real_seq → message_id 映射(与 NapCat 的 onRecvMsg 对齐,
        # 所有消息都进 FIFO,不论是否触发 bot)
        if self._seq_map is not None and data.get("post_type") == "message":
            _seq_map_add(self._seq_map, data)
        parsed = await parse_event(
            data,
            self_id=self._config.self_id,
            group_require_mention=self._config.group_require_mention,
            mention_first_only=self._config.group_mention_first_only,
            trigger_keywords=self._config.group_trigger_keywords,
            keyword_first_only=self._config.group_keyword_first_only,
            keep_mention=self._config.group_keep_mention,
            api=self._api,
            config=self._config,
            name_resolver=self._name_resolver,
            is_known_command_fn=self._is_known_command_fn,
            canonical_command_name_fn=self._canonical_command_name_fn,
        )
        if parsed is None:
            logger.debug("OneBot forward event ignored (post_type=%s)", data.get("post_type"))
            return
        # FilteredEvent → reject message via callback, don't forward to Hermes
        from onebot_adapter.relay.protocol import FilteredEvent

        if isinstance(parsed, FilteredEvent):
            logger.debug(
                "OneBot forward command filtered: chat_id=%s cmd=%s",
                parsed.chat_id, parsed.command_name,
            )
            if self._on_filtered:
                try:
                    await self._on_filtered(parsed)
                except Exception:
                    logger.exception("OneBot forward: on_filtered callback failed")
            return
        event = parsed
        log_recv_line(event, self._config.log_message_preview)
        logger.debug("OneBot forward parsed text preview: %r", (event.text or "")[:500])
        if self._on_event:
            try:
                await self._on_event(event)
            except Exception:
                logger.exception("OneBot forward: on_event callback failed")

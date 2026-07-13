"""Reverse WebSocket server: OneBot dials out to this endpoint."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp
import aiohttp.web

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot.log_format import log_recv_line
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.parser import parse_event
from onebot_adapter.onebot.seq_map import SeqMap, _seq_map_add
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
        self._active: set[aiohttp.web.WebSocketResponse] = set()
        self._text_tasks: set[asyncio.Task] = set()
        self.connected = False
        self._name_resolver = name_resolver or NameResolver(api)

    def update_config(self, config: AdapterConfig) -> None:
        """Hot-reload config without rebuilding the server (route stays bound)."""
        self._config = config

    def add_routes(self, app: aiohttp.web.Application) -> None:
        app.router.add_get(self._config.onebot_reverse_ws_path, self._handler)

    async def _handler(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        token = request.query.get("token") or _bearer(request.headers.get("Authorization", ""))
        if not self._config.onebot_ws_token or token != self._config.onebot_ws_token:
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
                    task = asyncio.create_task(self._handle_text(msg.data))
                    self._text_tasks.add(task)
                    task.add_done_callback(self._text_tasks.discard)
                    task.add_done_callback(_log_task_exc)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            self._active.discard(ws)
            if self._ws_api_transport is not None:
                self._ws_api_transport.unregister(ws)
            if not self._active:
                self.connected = False
                if self._on_disconnect:
                    self._on_disconnect()
            logger.info("OneBot reverse WS disconnected")
        return ws

    async def _handle_text(self, raw: str) -> None:
        # 先检查是否是 WS API 的响应帧（命中 echo 的 pending 请求），若是则
        # 由 WsApiTransport resolve 对应 future 并结束，不进 parser 流程。
        if self._ws_api_transport is not None and self._ws_api_transport.on_text(raw):
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("OneBot: non-JSON frame ignored")
            return
        logger.debug("OneBot reverse recv raw: %s", raw[:2000])
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
            strip_first_mention=self._config.group_strip_first_mention,
            api=self._api,
            config=self._config,
            name_resolver=self._name_resolver,
            is_known_command_fn=self._is_known_command_fn,
            canonical_command_name_fn=self._canonical_command_name_fn,
        )
        if parsed is None:
            logger.debug("OneBot reverse event ignored (post_type=%s)", data.get("post_type"))
            return
        # FilteredEvent → reject message via callback, don't forward to Hermes
        from onebot_adapter.relay.protocol import FilteredEvent

        if isinstance(parsed, FilteredEvent):
            logger.debug(
                "OneBot reverse command filtered: chat_id=%s cmd=%s",
                parsed.chat_id, parsed.command_name,
            )
            if self._on_filtered:
                try:
                    await self._on_filtered(parsed)
                except Exception:
                    logger.exception("OneBot reverse: on_filtered callback failed")
            return
        event = parsed
        log_recv_line(event, self._config.log_message_preview)
        logger.debug("OneBot reverse parsed text preview: %r", (event.text or "")[:500])
        if self._on_event:
            try:
                await self._on_event(event)
            except Exception:
                logger.exception("OneBot: on_event callback failed")

    async def stop(self) -> None:
        for ws in list(self._active):
            await ws.close()
        self._active.clear()
        self.connected = False
        # Cancel and await in-flight _handle_text tasks to avoid them using a
        # closed session after cleanup.
        for task in list(self._text_tasks):
            task.cancel()
        if self._text_tasks:
            await asyncio.gather(*self._text_tasks, return_exceptions=True)
        self._text_tasks.clear()


def _bearer(header: str) -> str:
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _log_task_exc(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("OneBot reverse background task crashed: %r", exc, exc_info=exc)

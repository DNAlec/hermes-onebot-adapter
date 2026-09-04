"""Cascade reverse WebSocket: leftover unmatched OneBot events for a downstream bot.

Downstream clients connect here as they would to a OneBot reverse WS.  The
adapter forwards group messages that missed @/keyword matching (and optional
meta_event frames) as raw OneBot 11 JSON, and writes inbound frames from this
port onto the live OneBot connection unchanged. API echo correlation lives on
``WsApiTransport.passthrough``.

Exactly one consumer is accepted at a time (same as Hermes plugin WS); a new
connection replaces the previous one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
import aiohttp.web

from onebot_adapter._async_utils import log_task_exception, token_matches, ws_presented_token
from onebot_adapter.config import AdapterConfig
from onebot_adapter.logging_utils import text_summary
from onebot_adapter.onebot.log_format import log_cascaded_event
from onebot_adapter.onebot.ws_api import WsApiTransport, echo_of, failed_response
from onebot_adapter.relay.protocol import DROP_REASON_TRIGGER, DroppedEvent

logger = logging.getLogger(__name__)

_OUT_QUEUE_SIZE = 256


class CascadeWsServer:
    """Hosts a reverse-WS endpoint that downstream OneBot clients dial into."""

    def __init__(
        self,
        config: AdapterConfig,
        ws_api_transport: WsApiTransport | None = None,
        on_connect: Any | None = None,
        on_disconnect: Any | None = None,
    ) -> None:
        self._config = config
        self._ws_api_transport = ws_api_transport
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._consumer: aiohttp.web.WebSocketResponse | None = None
        self._out_queue: asyncio.Queue[str] | None = None
        self._out_task: asyncio.Task[Any] | None = None

    @property
    def has_clients(self) -> bool:
        return self._consumer is not None

    def update_config(self, config: AdapterConfig) -> None:
        self._config = config

    def add_routes(self, app: aiohttp.web.Application) -> None:
        app.router.add_get(self._config.cascade_ws_path, self._handler_endpoint)

    def broadcast_raw(self, raw: str) -> bool:
        """Enqueue *raw* for the connected consumer. Never awaits socket I/O.

        Returns True if the frame was actually queued.
        """
        if not self._config.cascade_ws_enabled or self._consumer is None:
            return False
        return self._enqueue_out(raw)

    def observe_dropped(self, event: DroppedEvent, raw: str) -> None:
        """Handler callback: cascade trigger-misses after the handler has logged the drop."""
        if event.reason == DROP_REASON_TRIGGER and self.broadcast_raw(raw):
            log_cascaded_event(event)

    def observe_ignored(self, raw: str, data: dict[str, Any]) -> None:
        """Handler callback: optionally forward meta_event frames."""
        if data.get("post_type") == "meta_event" and self._config.cascade_forward_meta:
            self.broadcast_raw(raw)

    async def _handler_endpoint(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        token = ws_presented_token(request, query_keys=("access_token", "token"))
        if not token_matches(token, self._config.cascade_ws_token):
            logger.warning("Cascade WS unauthorized remote=%s", request.remote)
            return aiohttp.web.json_response({"error": "unauthorized"}, status=401)
        if not self._config.cascade_ws_enabled:
            logger.warning("Cascade WS rejected (disabled) remote=%s", request.remote)
            return aiohttp.web.json_response({"error": "cascade forwarding is disabled"}, status=503)
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        old = self._consumer
        self._consumer = ws
        self._stop_out_worker()
        if old is not None and old is not ws:
            await old.close(code=1001, message=b"replaced by new consumer")
        if self._on_connect:
            self._on_connect()
        logger.info("Cascade WS connected from %s", request.remote)
        if (
            self._config.cascade_forward_meta
            and self._ws_api_transport is not None
            and self._ws_api_transport.has_active
        ):
            self._enqueue_out(self._lifecycle_connect_frame(), dest=ws)
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_inbound(msg.data, ws)
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        finally:
            if self._ws_api_transport is not None:
                self._ws_api_transport.drop_owner(ws)
            if self._consumer is ws:
                self._consumer = None
                self._stop_out_worker()
                if self._on_disconnect:
                    self._on_disconnect()
            logger.info(
                "Cascade WS disconnected close_code=%s exception=%r",
                ws.close_code, ws.exception(),
            )
        return ws

    def _lifecycle_connect_frame(self) -> str:
        raw_id = self._config.self_id
        try:
            self_id: Any = int(raw_id) if raw_id else 0
        except ValueError:
            self_id = raw_id or 0
        return json.dumps(
            {
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
                "time": int(time.time()),
                "self_id": self_id,
            },
            ensure_ascii=False,
        )

    async def _handle_inbound(self, raw: str, ws: aiohttp.web.WebSocketResponse) -> None:
        echo = echo_of(raw)
        if not self._config.cascade_ws_enabled:
            logger.debug("cascade inbound dropped (disabled): %s", text_summary(raw))
            self._enqueue_error(ws, echo, "cascade forwarding is disabled")
            return
        transport = self._ws_api_transport
        if transport is None or not transport.has_active:
            logger.debug("cascade inbound dropped (no OneBot): %s", text_summary(raw))
            self._enqueue_error(ws, echo, "no active OneBot WS connection")
            return
        try:
            await transport.passthrough(
                raw,
                lambda resp, dest=ws: self._enqueue_out(resp, dest=dest),
                owner=ws,
                timeout=self._config.file_upload_timeout,
            )
        except RuntimeError:
            logger.debug("cascade inbound dropped (no OneBot): %s", text_summary(raw))
            self._enqueue_error(ws, echo, "no active OneBot WS connection")
        except Exception:
            logger.exception("cascade failed to forward inbound frame to OneBot")
            self._enqueue_error(ws, echo, "failed to forward to OneBot")

    def _enqueue_error(self, ws: aiohttp.web.WebSocketResponse, echo: Any, message: str) -> None:
        if echo is None:
            return
        self._enqueue_out(failed_response(echo, message), dest=ws)

    def _enqueue_out(self, raw: str, *, dest: Any = None) -> bool:
        ws = self._consumer
        if ws is None or (dest is not None and dest is not ws):
            return False
        q = self._out_queue
        if q is None:
            q = asyncio.Queue(maxsize=_OUT_QUEUE_SIZE)
            self._out_queue = q
            task = asyncio.create_task(self._out_loop(ws, q), name="cascade-out")
            self._out_task = task
            task.add_done_callback(self._on_out_task_done)
            task.add_done_callback(log_task_exception)
        try:
            q.put_nowait(raw)
            return True
        except asyncio.QueueFull:
            logger.warning("cascade outbound queue full; dropping frame")
            return False

    async def _out_loop(self, ws: Any, q: asyncio.Queue[str]) -> None:
        try:
            while True:
                raw = await q.get()
                await self._send_str(ws, raw)
        except asyncio.CancelledError:
            raise
        finally:
            if self._out_queue is q:
                self._out_queue = None

    def _on_out_task_done(self, task: asyncio.Task[Any]) -> None:
        if self._out_task is task:
            self._out_task = None

    def _stop_out_worker(self) -> None:
        task = self._out_task
        self._out_task = None
        self._out_queue = None
        if task is not None and not task.done():
            task.cancel()

    async def _send_str(self, ws: aiohttp.web.WebSocketResponse, raw: str) -> None:
        if ws.closed:
            return
        try:
            await ws.send_str(raw)
        except Exception:
            logger.debug("cascade: send to client failed", exc_info=True)

    async def stop(self) -> None:
        ws = self._consumer
        self._consumer = None
        if ws is not None:
            await ws.close()
        task = self._out_task
        self._stop_out_worker()
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

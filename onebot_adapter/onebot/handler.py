"""Shared handler for OneBot text frames (used by both reverse and forward WS).

Both ``OneBotReverseServer`` and ``OneBotForwardClient`` receive OneBot 11
event frames over a WebSocket and run the same pipeline: WS API response
interception → SeqMap population → event parsing → command filtering →
callback dispatch.  This module factors that pipeline out of the two
transport-specific modules so it stays in sync.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from onebot_adapter.config import AdapterConfig
from onebot_adapter.logging_utils import safe_json, text_summary
from onebot_adapter.onebot.log_format import log_dropped_event, log_recv_line
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.parser import parse_event
from onebot_adapter.onebot.seq_map import SeqMap, seq_map_add
from onebot_adapter.onebot.ws_api import WsApiTransport, bind_request_ws, reset_request_ws
from onebot_adapter.relay.protocol import DroppedEvent, FilteredEvent

logger = logging.getLogger(__name__)

_DEFAULT_EVENT_QUEUE_SIZE = 1024
_DROP_LOG_INTERVAL = 5.0


class OnDropped(Protocol):
    def __call__(self, event: DroppedEvent, raw: str, /) -> Any: ...


class OnIgnored(Protocol):
    def __call__(self, raw: str, data: dict[str, Any], /) -> Any: ...


class OneBotHandler:
    """Shared state + pipeline for processing OneBot text frames.

    Reverse and forward WS transports receive an injected handler and call
    ``handle_text(raw)`` for every inbound text frame.  The handler owns no
    transport-specific state — it only knows how to parse and dispatch.
    """

    def __init__(
        self,
        *,
        label: str,
        config: AdapterConfig,
        api: Any,
        on_event: Callable[..., Any] | None = None,
        on_filtered: Callable[..., Any] | None = None,
        on_dropped: OnDropped | None = None,
        on_ignored: OnIgnored | None = None,
        is_known_command_fn: Any | None = None,
        canonical_command_name_fn: Any | None = None,
        seq_map: SeqMap | None = None,
        name_resolver: NameResolver | None = None,
        ws_api_transport: WsApiTransport | None = None,
        bot_blacklist_match_fn: Any | None = None,
        friend_cache: Any | None = None,
    ) -> None:
        self.label = label
        self._config = config
        self._api = api
        self._on_event = on_event
        self._on_filtered = on_filtered
        self._on_dropped = on_dropped
        self._on_ignored = on_ignored
        self._is_known_command_fn = is_known_command_fn
        self._canonical_command_name_fn = canonical_command_name_fn
        self._seq_map = seq_map
        self._name_resolver = name_resolver or NameResolver(api)
        self._ws_api_transport = ws_api_transport
        self._bot_blacklist_match_fn = bot_blacklist_match_fn
        self._friend_cache = friend_cache

    def update_config(self, config: AdapterConfig) -> None:
        """Hot-reload config without rebuilding the handler."""
        self._config = config

    async def handle_text(self, raw: str) -> None:
        """Process a single OneBot text frame end-to-end."""
        # 先检查是否是 WS API 的响应帧（命中 echo 的 pending 请求），若是则
        # 由 WsApiTransport resolve 对应 future 并结束，不进 parser 流程。
        if self.intercept_api_response(raw):
            return
        await self.handle_event_text(raw)

    def intercept_api_response(self, raw: str) -> bool:
        """Resolve a WS API response without scheduling event parsing.

        Transports call this directly from their receive loop.  Keeping this
        fast path outside the ordered event worker is essential: event parsing
        may itself await an API response carried by the same WebSocket.
        """
        return self._ws_api_transport is not None and self._ws_api_transport.on_text(raw)

    def record_inbound_seq(self, raw: str) -> None:
        """Populate SeqMap on the receive loop, before the bounded event queue.

        Must run even for frames that are later dropped when the queue is full,
        otherwise ``real_seq`` lookups miss the messages an LLM is most likely
        to cite under load.
        """
        if self._seq_map is None:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict) and data.get("post_type") == "message":
            seq_map_add(self._seq_map, data)

    async def handle_event_text(self, raw: str) -> None:
        """Parse and dispatch a frame already known not to be an API response."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("OneBot %s: non-JSON frame ignored", self.label)
            return
        logger.debug("OneBot %s recv frame: %s", self.label, safe_json(data))
        # 在 parser 之前存 real_seq → message_id 映射(与 NapCat 的 onRecvMsg 对齐,
        # 所有消息都进 FIFO,不论是否触发 bot)
        if self._seq_map is not None and data.get("post_type") == "message":
            seq_map_add(self._seq_map, data)
        if self._friend_cache is not None and isinstance(data, dict):
            self._friend_cache.observe_event(data)
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
            bot_blacklist_match_fn=self._bot_blacklist_match_fn,
            is_friend_fn=self._friend_cache.is_friend if self._friend_cache is not None else None,
        )
        if parsed is None:
            logger.debug("OneBot %s event ignored (post_type=%s)", self.label, data.get("post_type"))
            await self._invoke(self._on_ignored, raw, data)
            return
        if isinstance(parsed, DroppedEvent):
            log_dropped_event(parsed)
            await self._invoke(self._on_dropped, parsed, raw)
            return
        # FilteredEvent → reject message via callback, don't forward to Hermes
        if isinstance(parsed, FilteredEvent):
            log_dropped_event(parsed)
            logger.debug(
                "OneBot %s event filtered: type=%s chat_id=%s cmd=%s",
                self.label, parsed.filter_type, parsed.chat_id, parsed.command_name,
            )
            await self._invoke(self._on_filtered, parsed)
            return
        event = parsed
        log_recv_line(
            event,
            self._config.log_message_preview,
            self._config.log_file_message_mode,
        )
        logger.debug("OneBot %s parsed text=%s", self.label, text_summary(event.text))
        await self._invoke(self._on_event, event)

    async def _invoke(self, callback: Any, *args: Any) -> None:
        if callback is None:
            return
        try:
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("OneBot %s: callback failed", self.label)


class OneBotEventDispatcher:
    """Bounded, ordered processor for non-response OneBot frames.

    The receive loop always handles correlated API responses immediately,
    then submits ordinary event frames here.  A single worker preserves the
    order observed on the WebSocket.  Overflow is explicit and bounded rather
    than allowing an unbounded number of parsing tasks to accumulate.
    """

    def __init__(
        self,
        handler: OneBotHandler,
        *,
        label: str,
        max_queue_size: int = _DEFAULT_EVENT_QUEUE_SIZE,
    ) -> None:
        self._handler = handler
        self._label = label
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        self._worker: asyncio.Task[None] | None = None
        self._dropped = 0
        self._last_drop_log = 0.0

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        return self._dropped

    def dispatch(self, raw: str, ws: Any = None) -> bool:
        """Handle a response immediately or enqueue an event.

        Returns ``False`` only when the bounded event queue is full and the
        event has to be dropped.  The receive loop must remain non-blocking so
        it can continue resolving API responses needed by the active worker.

        *ws* is the connection that carried this frame; parse-time API calls
        are pinned to it so multi-instance reverse WS does not mix sockets.
        """
        if self._handler.intercept_api_response(raw):
            return True
        record = getattr(self._handler, "record_inbound_seq", None)
        if record is not None:
            record(raw)
        self._ensure_worker()
        try:
            self._queue.put_nowait((raw, ws))
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            now = time.monotonic()
            if now - self._last_drop_log >= _DROP_LOG_INTERVAL:
                logger.error(
                    "OneBot %s event queue full; dropped=%d queued=%d",
                    self._label,
                    self._dropped,
                    self._queue.qsize(),
                )
                self._last_drop_log = now
            return False

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(), name=f"onebot-{self._label}-event-worker"
            )

    async def _run(self) -> None:
        while True:
            raw, ws = await self._queue.get()
            token = bind_request_ws(ws) if ws is not None else None
            try:
                await self._handler.handle_event_text(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OneBot %s event worker failed; continuing", self._label)
            finally:
                if token is not None:
                    reset_request_ws(token)
                self._queue.task_done()

    async def stop(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()

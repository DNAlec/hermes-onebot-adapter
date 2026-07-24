"""Shared handler for OneBot text frames (used by both reverse and forward WS).

Both ``OneBotReverseServer`` and ``OneBotForwardClient`` receive OneBot 11
event frames over a WebSocket and run the same pipeline: WS API response
interception → SeqMap population → event parsing → command filtering →
callback dispatch.  This module factors that pipeline out of the two
transport-specific modules so it stays in sync.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from onebot_adapter.config import AdapterConfig
from onebot_adapter.logging_utils import safe_json
from onebot_adapter.onebot.log_format import log_recv_line
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.parser import parse_event
from onebot_adapter.onebot.seq_map import SeqMap, seq_map_add
from onebot_adapter.onebot.ws_api import WsApiTransport
from onebot_adapter.relay.protocol import FilteredEvent

logger = logging.getLogger(__name__)


class OneBotHandler:
    """Shared state + pipeline for processing OneBot text frames.

    Both reverse and forward WS transports construct one of these and call
    ``handle_text(raw)`` for every inbound text frame.  The handler owns no
    transport-specific state — it only knows how to parse and dispatch.
    """

    def __init__(
        self,
        *,
        label: str,
        config: AdapterConfig,
        api: Any,
        on_event: Any | None = None,
        on_filtered: Any | None = None,
        is_known_command_fn: Any | None = None,
        canonical_command_name_fn: Any | None = None,
        seq_map: SeqMap | None = None,
        name_resolver: NameResolver | None = None,
        ws_api_transport: WsApiTransport | None = None,
        bot_blacklist_match_fn: Any | None = None,
    ) -> None:
        self.label = label
        self._config = config
        self._api = api
        self._on_event = on_event
        self._on_filtered = on_filtered
        self._is_known_command_fn = is_known_command_fn
        self._canonical_command_name_fn = canonical_command_name_fn
        self._seq_map = seq_map
        self._name_resolver = name_resolver or NameResolver(api)
        self._ws_api_transport = ws_api_transport
        self._bot_blacklist_match_fn = bot_blacklist_match_fn

    def update_config(self, config: AdapterConfig) -> None:
        """Hot-reload config without rebuilding the handler."""
        self._config = config

    async def handle_text(self, raw: str) -> None:
        """Process a single OneBot text frame end-to-end."""
        # 先检查是否是 WS API 的响应帧（命中 echo 的 pending 请求），若是则
        # 由 WsApiTransport resolve 对应 future 并结束，不进 parser 流程。
        if self._ws_api_transport is not None and self._ws_api_transport.on_text(raw):
            return
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
        )
        if parsed is None:
            logger.debug("OneBot %s event ignored (post_type=%s)", self.label, data.get("post_type"))
            return
        # FilteredEvent → reject message via callback, don't forward to Hermes
        if isinstance(parsed, FilteredEvent):
            logger.debug(
                "OneBot %s event filtered: type=%s chat_id=%s cmd=%s",
                self.label, parsed.filter_type, parsed.chat_id, parsed.command_name,
            )
            if self._on_filtered:
                try:
                    await self._on_filtered(parsed)
                except Exception:
                    logger.exception("OneBot %s: on_filtered callback failed", self.label)
            return
        event = parsed
        log_recv_line(
            event,
            self._config.log_message_preview,
            self._config.log_file_message_mode,
        )
        logger.debug("OneBot %s parsed text preview: %r", self.label, (event.text or "")[:500])
        if self._on_event:
            try:
                await self._on_event(event)
            except Exception:
                logger.exception("OneBot %s: on_event callback failed", self.label)

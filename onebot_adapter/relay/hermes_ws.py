"""WebSocket server endpoint the Hermes plugin connects to.

The plugin authenticates with a bearer token (``Authorization`` header,
falling back to query param ``token``). Inbound OneBot events are pushed here; plugin
send / api-call requests are dispatched to the OneBot API.

All frames on this WS are JSON text frames — no binary frames. Media is
passed as file paths / URLs in the JSON payload (path passthrough), and
the adapter forwards them to OneBot/NapCat which reads the local files
or downloads URLs itself.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import aiohttp
import aiohttp.web

from onebot_adapter._async_utils import log_task_exception as _log_task_exception
from onebot_adapter._async_utils import token_matches, ws_presented_token
from onebot_adapter.config import AdapterConfig
from onebot_adapter.logging_utils import safe_json
from onebot_adapter.onebot import api as ob
from onebot_adapter.onebot.log_format import outbound_log_req_id
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.seq_map import SeqMap
from onebot_adapter.outbound_filter import (
    API_SEND_ACTIONS,
    extract_api_message_text,
    extract_send_frame_text,
    matching_pattern,
)
from onebot_adapter.relay.protocol import (
    NormalizedEvent,
    error_message,
    event_message,
    parse_chat_id,
    pong_message,
    ready_message,
    result_message,
)

logger = logging.getLogger(__name__)


# Send actions eligible for deduplication.  When the Hermes gateway's
# ``send_text`` (or any send) times out waiting for the adapter's ``result``
# frame, it retries with a fresh ``req_id`` but identical payload — we hash the
# payload (excluding ``req_id``) and short-circuit retries within the TTL.
_DEDUP_ACTIONS = frozenset({"send_text", "send_image", "send_voice", "send_video", "send_document"})


def _send_fingerprint(action: str, data: dict[str, Any]) -> str:
    """Stable content fingerprint for a send frame.

    Gateway retries reuse the same action + content fields but issue a new
    ``req_id``, so we hash the payload (excluding ``req_id``) to recognise
    duplicate send attempts.  Returns a 16-char hex digest.
    """
    if action == "send_text":
        raw = str(data.get("content", ""))
    elif action == "send_image":
        raw = f"{data.get('image_url', '')}|{data.get('caption', '')}"
    elif action == "send_voice":
        raw = f"{data.get('audio_path', '')}|{data.get('caption', '')}"
    elif action == "send_video":
        raw = f"{data.get('video_path', '')}|{data.get('caption', '')}"
    elif action == "send_document":
        raw = f"{data.get('file_path', '')}|{data.get('filename', '')}"
    else:  # defensive: never expected since caller filters by _DEDUP_ACTIONS
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _append_reply_segment(segs: list[dict], reply_to: Any) -> None:
    if not reply_to:
        return
    try:
        segs.append(ob.reply_segment(int(reply_to)))
    except (ValueError, TypeError):
        pass


def _build_send_segments(action: str, data: dict[str, Any]) -> list[dict]:
    """Build OneBot segments for ordinary message send actions."""
    segs: list[dict] = []
    _append_reply_segment(segs, data.get("reply_to"))
    if action == "send_text":
        segs.append(ob.text_segment(data.get("content", "")))
        return segs

    media_fields: dict[str, tuple[str, Callable[[str], dict]]] = {
        "send_image": ("image_url", ob.image_segment),
        "send_voice": ("audio_path", ob.record_segment),
        "send_video": ("video_path", ob.video_segment),
    }
    media_spec = media_fields.get(action)
    if media_spec is None:
        raise ValueError(f"unknown action {action!r}")
    field_name, segment_factory = media_spec
    file_ref = str(data.get(field_name, ""))
    if not file_ref:
        raise ValueError(f"no {field_name} provided")
    segs.append(segment_factory(file_ref))
    if data.get("caption"):
        segs.append(ob.text_segment(data["caption"]))
    return segs


class HermesRelayServer:
    _RING_BUFFER_SIZE = 50
    _RING_BUFFER_MAX_AGE = 30.0  # seconds; skip older events on replay
    _WATCHDOG_INTERVAL = 30.0    # seconds between busy-timeout sweeps
    _STOP_IDLE_DELAY = 3.0      # seconds to wait after /stop before force-clearing busy
    # Hermes gateway built-in commands that interrupt an in-flight turn by
    # bumping the session generation, causing the stale run's idle callback
    # to be popped without firing.  Hardcoded because they are gateway
    # internals, not plugin commands — the ``commands_snapshot`` registry
    # only carries plugin/builtin slash commands, not generation-bumping ones.
    _INTERRUPT_COMMANDS = frozenset({"stop", "new", "reset"})
    _SESSION_RESET_COMMANDS = frozenset({"new", "reset"})
    # Max concurrent OneBot API send calls (send_group_msg / upload_*_file).
    # NapCat serializes API requests on a single WS, so unbounded concurrency
    # queues up at NapCat and inflates latency past the plugin's 30s
    # _RESULT_TIMEOUT, triggering Gateway retries and a death spiral.
    # Aligned with the plugin-side _MAX_INFLIGHT_SENDS=2.
    _MAX_CONCURRENT_SENDS = 2
    _MAX_INFLIGHT_PLUGIN_FRAMES = 64
    # Send-dedup cache hard cap.  The cache is also TTL-evicted lazily on
    # lookup, but sends that are never retried (the common case) would
    # otherwise accumulate forever.  Opportunistic eviction on insert keeps
    # the dict bounded without a background sweep.
    _SEND_CACHE_MAX = 4096

    def __init__(
        self,
        config: AdapterConfig,
        api: Any,
        adapter_version: str,
        onebot_connected_fn: Callable[[], bool],
        on_connect: Callable[[], Any] | None = None,
        on_disconnect: Callable[[], Any] | None = None,
        on_dispatch: Callable[[NormalizedEvent], Awaitable[None]] | None = None,
        seq_map: SeqMap | None = None,
        name_resolver: NameResolver | None = None,
        local_api_call: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self._config = config
        self._api = api
        self._adapter_version = adapter_version
        self._onebot_connected_fn = onebot_connected_fn
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_dispatch = on_dispatch
        self._seq_map = seq_map
        self._name_resolver = name_resolver
        self._local_api_call = local_api_call
        self._clients: set[aiohttp.web.WebSocketResponse] = set()
        self._ring_buffer: deque[tuple[float, NormalizedEvent]] = deque(
            maxlen=self._RING_BUFFER_SIZE,
        )
        # Slash-command registry pushed by the Hermes plugin.  Maps lowercase
        # command name → command metadata dict.  Empty until the first
        # ``commands_snapshot`` frame is received.
        self._commands: dict[str, dict[str, Any]] = {}
        self._commands_aliases: dict[str, str] = {}  # alias → canonical name
        # Tasks spawned to handle client text frames / send dispatches.
        # Tracked in a single global set so ``stop()`` can cancel every
        # in-flight task (broadcast, dequeue, seq_map populate, …) regardless
        # of which client originated it.  Per-client cancellation is handled
        # via the local ``my_tasks`` set in ``_handler``.
        self._text_tasks: set[asyncio.Task] = set()
        # Send-dedup cache: (chat_id, action, fingerprint, reply_to) ->
        # (monotonic_ts, message_id).  Guards against Gateway send retries
        # (plugin reissues the same payload with a fresh req_id when the
        # adapter's result frame times out).  Lazy TTL eviction on lookup.
        self._send_cache: dict[tuple[str, str, str, str], tuple[float, str]] = {}
        # ── 群聊排队 ──────────────────────────────────────────────────────
        # Hermes 顶层 group_sessions_per_user 由插件上报(hermes_mode_report 帧)。
        # True  → Hermes 每个群成员独立 session,无需排队(默认值,安全)
        # False → 全群共享 session,需排队防止不同成员互相打断
        # 排队生效条件:per_user=False AND config.event_queue_enabled=True
        self._hermes_group_sessions_per_user: bool = True
        self._busy_groups: dict[str, tuple[str, float]] = {}
        # Incremented when a group claims or is handed a new busy slot.
        # Send-path timestamp refresh must not change this; /stop delayed
        # cleanup uses (user_id, epoch) so a "stopped" confirmation cannot
        # cancel the force-clear.
        self._busy_epoch: dict[str, int] = {}
        self._queues: dict[str, deque[NormalizedEvent]] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._event_push_lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task[None] | None = None
        # Limit concurrent OneBot API send calls to prevent NapCat WS
        # serialization from inflating latency past _RESULT_TIMEOUT.
        self._send_api_semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_SENDS)
        self._plugin_version: str | None = None
        self._version_mismatch: bool = True
        self._latest_plugin_status: dict[str, Any] | None = None

    def update_config(self, config: AdapterConfig) -> None:
        """Hot-reload config without rebuilding the server (route stays bound)."""
        # Clear the dedup cache only when the dedup policy actually changes —
        # an unrelated hot-reload (e.g. log_level) must not wipe the cache and
        # turn a subsequent legit Gateway retry into a duplicate send.
        old = self._config
        if (
            old.send_dedup_enabled != config.send_dedup_enabled
            or old.send_dedup_ttl_seconds != config.send_dedup_ttl_seconds
        ):
            self._send_cache.clear()
        # Trim any per-chat queues that exceed a newly-lowered cap.
        new_cap = config.event_queue_max_per_chat
        for gid, q in list(self._queues.items()):
            while len(q) > new_cap:
                q.popleft()
                logger.warning("relay queue trimmed (config hot-reload): gid=%s", gid)
        # 排队总开关从 True→False:清空所有 busy/queue,立即放行。
        if old.event_queue_enabled and not config.event_queue_enabled:
            if self._busy_groups:
                logger.info(
                    "relay: event_queue_enabled disabled, clearing %d busy group(s)",
                    len(self._busy_groups),
                )
            self._busy_groups.clear()
            self._busy_epoch.clear()
            self._queues.clear()
        # 插件运行时配置变化:广播 fresh ready 让插件实时切换。
        # broadcast_self_id 复用 ready 帧机制,这里用新 config 广播。
        self._config = config
        if (
            old.media_delivery_mode != config.media_delivery_mode
            or old.file_upload_timeout != config.file_upload_timeout
        ):
            logger.info(
                "relay: plugin runtime config changed, broadcasting fresh ready",
            )
            task = asyncio.create_task(self.broadcast_self_id(config.self_id))
            self._text_tasks.add(task)
            task.add_done_callback(self._text_tasks.discard)
            task.add_done_callback(_log_task_exception)

    def _maybe_evict_send_cache(self) -> None:
        """Opportunistic cap on the send-dedup cache.

        The cache is normally TTL-evicted lazily on lookup, but a send that is
        never retried (the common case) inserts a key that is never looked up
        again.  To keep the dict from growing without bound we evict a small
        batch of the oldest entries whenever the cache exceeds its cap.  We
        pick the entries with the smallest monotonic timestamp, which is a
        best-effort FIFO eviction without a separate deque.
        """
        if len(self._send_cache) <= self._SEND_CACHE_MAX:
            return
        # Evict ~10% of the cap (the oldest by timestamp) to amortise the
        # O(n) scan over many inserts.
        excess = len(self._send_cache) - self._SEND_CACHE_MAX + max(1, self._SEND_CACHE_MAX // 10)
        # sorted() is O(n log n); n is capped (~4k) so this is cheap enough
        # and only fires when over the cap, not on every insert.
        for key, _ts in sorted(self._send_cache.items(), key=lambda kv: kv[1][0])[:excess]:
            self._send_cache.pop(key, None)

    @property
    def commands(self) -> list[dict[str, Any]]:
        """Return the current slash-command registry as a list of dicts."""
        return list(self._commands.values())

    @property
    def hermes_group_sessions_per_user(self) -> bool:
        """Hermes 顶层 group_sessions_per_user,由插件通过 hermes_mode_report 帧上报。

        True=每个群成员独立 session(默认,安全);False=全群共享 session,排队有意义。
        """
        return self._hermes_group_sessions_per_user

    @property
    def plugin_version(self) -> str | None:
        """插件上报的版本号,插件未连接时为 None。"""
        return self._plugin_version

    @property
    def version_mismatch(self) -> bool:
        """插件版本与适配器版本是否不匹配。

        True 表示版本不一致(含插件未连接/未上报),WebUI 应提示重新安装插件。
        """
        return self._version_mismatch

    @property
    def latest_plugin_status(self) -> dict[str, Any] | None:
        """Latest bounded health/error summary reported by the Hermes plugin."""
        return dict(self._latest_plugin_status) if self._latest_plugin_status else None

    def _store_hermes_mode(self, group_sessions_per_user: bool) -> None:
        """缓存插件上报的 Hermes group_sessions_per_user 值。"""
        old = self._hermes_group_sessions_per_user
        self._hermes_group_sessions_per_user = bool(group_sessions_per_user)
        if old != self._hermes_group_sessions_per_user:
            logger.info(
                "relay: hermes group_sessions_per_user updated: %s -> %s",
                old, self._hermes_group_sessions_per_user,
            )
            # 从隔离→不隔离,排队可能突然生效;从非隔离→隔离,清空 busy/queue
            if self._hermes_group_sessions_per_user:
                if self._busy_groups:
                    logger.info(
                        "relay: per_user became True, clearing %d busy group(s)",
                        len(self._busy_groups),
                    )
                self._busy_groups.clear()
                self._busy_epoch.clear()
                self._queues.clear()

    def _store_plugin_version(self, plugin_version: str) -> None:
        """缓存插件上报的版本号并比对。"""
        self._plugin_version = plugin_version
        self._version_mismatch = plugin_version != self._adapter_version
        if self._version_mismatch:
            logger.warning(
                "relay: plugin version mismatch — adapter=%s plugin=%s",
                self._adapter_version, plugin_version,
            )
        else:
            logger.info("relay: plugin version matches adapter (%s)", plugin_version)

    def is_known_command(self, name: str) -> bool:
        """Check whether *name* (lowercase, without "/") is a registered
        slash command or an alias of one."""
        if not name:
            return False
        if name == "clean" and self._config.event_queue_clean_command_enabled:
            return True
        return name in self._commands or name in self._commands_aliases

    def canonical_command_name(self, name: str) -> str:
        """Resolve *name* (possibly an alias) to its canonical command name."""
        if name == "clean" and self._config.event_queue_clean_command_enabled:
            return name
        if name in self._commands:
            return name
        return self._commands_aliases.get(name, name)

    def _store_commands(self, commands: list[dict[str, Any]]) -> None:
        """Replace the cached command registry from a commands_snapshot."""
        self._commands.clear()
        self._commands_aliases.clear()
        for cmd in commands or []:
            name = str(cmd.get("name", "")).lower().strip().lstrip("/")
            if not name:
                continue
            self._commands[name] = cmd
            for alias in cmd.get("aliases", []) or []:
                alias_l = str(alias).lower().strip().lstrip("/")
                if alias_l and alias_l not in self._commands:
                    self._commands_aliases[alias_l] = name
        logger.debug(
            "relay: stored commands_snapshot (%d commands, %d aliases)",
            len(self._commands), len(self._commands_aliases),
        )

    def add_routes(self, app: aiohttp.web.Application) -> None:
        app.router.add_get(self._config.hermes_ws_path, self._handler)

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    async def stop(self) -> None:
        # Cancel in-flight tasks first so they stop touching the WebSocket
        # before we close it — otherwise pending sends would log misleading
        # "send failed" errors against the closing ws.
        snap = list(self._text_tasks)
        for task in snap:
            task.cancel()
        if snap:
            await asyncio.gather(*snap, return_exceptions=True)
        self._text_tasks.clear()
        # Now close the WebSocket responses (idempotent if already closed).
        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()
        # Stop the queue watchdog and clear all queue state.
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("relay: watchdog task raised on cancel", exc_info=True)
            self._watchdog_task = None
        self._busy_groups.clear()
        self._busy_epoch.clear()
        self._queues.clear()

    async def broadcast_commands_refresh(self) -> None:
        """Send a ``commands_refresh`` frame to every connected plugin client,
        asking it to re-collect and push a fresh commands_snapshot."""
        from onebot_adapter.relay.protocol import commands_refresh_message

        for ws in list(self._clients):
            try:
                await ws.send_json(commands_refresh_message())
            except Exception:
                logger.warning("relay: failed to send commands_refresh to a client")
                self._clients.discard(ws)

    async def broadcast_mode_refresh(self) -> None:
        """Send a ``mode_refresh`` frame to every connected plugin client,
        asking it to re-read Hermes config and push a fresh
        ``hermes_mode_report`` (current group_sessions_per_user)."""
        from onebot_adapter.relay.protocol import mode_refresh_message

        for ws in list(self._clients):
            try:
                await ws.send_json(mode_refresh_message())
            except Exception:
                logger.warning("relay: failed to send mode_refresh to a client")
                self._clients.discard(ws)

    async def broadcast_self_id(self, self_id: str) -> None:
        """Push an updated self_id (and media_delivery_mode) to every connected
        plugin client by sending a fresh ``ready`` frame.

        Called after _probe_self_id succeeds so that plugins that connected
        before the probe completes see the new self_id.  Also called when
        ``media_delivery_mode`` changes via hot-reload — the fresh ready frame
        carries the new mode so plugins can switch caching strategy without
        reconnecting.
        """
        msg = ready_message(
            onebot_connected=self._onebot_connected_fn(),
            adapter_version=self._adapter_version,
            self_id=self_id,
            media_delivery_mode=self._config.media_delivery_mode,
            file_upload_timeout=self._config.file_upload_timeout,
        )
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                logger.warning("relay: failed to send self_id update to a client")
                self._clients.discard(ws)

    async def _handler(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        token = ws_presented_token(request, query_keys=("token",))
        if not token_matches(token, self._config.hermes_ws_token):
            logger.warning("Hermes WS unauthorized remote=%s", request.remote)
            return aiohttp.web.json_response({"error": "unauthorized"}, status=401)
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        role = request.query.get("role", "consumer")
        is_consumer = role != "rpc"
        if is_consumer:
            # The gateway has exactly one event-consumer connection.  Closing
            # an overlapping stale connection prevents duplicate event
            # delivery during reconnect races.
            for old_ws in list(self._clients):
                await old_ws.close(code=1001, message=b"replaced by new consumer")
            self._clients.add(ws)
        my_tasks: set[asyncio.Task] = set()
        if is_consumer and self._on_connect:
            self._on_connect()
        logger.info("Hermes %s WS connected from %s", role, request.remote)
        await ws.send_json(
            ready_message(
                onebot_connected=self._onebot_connected_fn(),
                adapter_version=self._adapter_version,
                self_id=self._config.self_id,
                media_delivery_mode=self._config.media_delivery_mode,
                file_upload_timeout=self._config.file_upload_timeout,
            )
        )
        # Replay buffered events so a reconnecting plugin doesn't miss messages.
        replay_ok = await self._replay_ring_buffer(ws) if is_consumer else True
        if not replay_ok:
            logger.warning("relay: closing plugin WS after ring buffer replay failure")
            await ws.close()
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if len(my_tasks) >= self._MAX_INFLIGHT_PLUGIN_FRAMES:
                        await asyncio.wait(my_tasks, return_when=asyncio.FIRST_COMPLETED)
                    task = asyncio.create_task(self._handle_text(ws, msg.data))
                    self._text_tasks.add(task)
                    my_tasks.add(task)
                    task.add_done_callback(self._text_tasks.discard)
                    task.add_done_callback(my_tasks.discard)
                    task.add_done_callback(_log_task_exception)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            if is_consumer:
                self._clients.discard(ws)
            if is_consumer and self._on_disconnect:
                self._on_disconnect()
            # Cancel this client's in-flight tasks: they hold references to
            # the now-closed ws and would otherwise log misleading "send
            # failed" errors on every pending send, plus hold the send
            # semaphore while waiting for a result that will never arrive.
            for task in list(my_tasks):
                task.cancel()
            if my_tasks:
                await asyncio.gather(*my_tasks, return_exceptions=True)
            my_tasks.clear()
            # When the last plugin client disconnects, clear all queue state:
            # no one remains to fire idle frames, so busy slots would otherwise
            # hang until the watchdog times them out.  Clearing immediately
            # lets a reconnecting plugin start fresh.
            if is_consumer and not self._clients:
                if self._busy_groups:
                    logger.info(
                        "relay: last plugin disconnected, clearing %d busy group(s)",
                        len(self._busy_groups),
                    )
                self._busy_groups.clear()
                self._busy_epoch.clear()
                self._queues.clear()
                self._plugin_version = None
                self._version_mismatch = True
                self._latest_plugin_status = None
            logger.info(
                "Hermes %s WS disconnected close_code=%s exception=%r",
                role, ws.close_code, ws.exception(),
            )
        return ws

    # ── Inbound push (adapter -> plugin) ───────────────────────────────

    async def push_event(self, event: NormalizedEvent) -> str:
        """Push a OneBot event toward the Hermes plugin.

        Writes to the ring buffer (skipping /commands) then routes through
        the queue policy.  Returns one of:

        - ``"broadcast"``  — the event was delivered to plugin client(s) now.
        - ``"queued"``     — the event was enqueued for a later idle frame.
        - ``"handled"``    — an adapter-local command handled the event.
        - ``"dropped"``    — the event could not be delivered (queue full or
          zero clients) and was discarded; the caller should NOT react as if
          it had been delivered.
        """
        # Skip slash commands from the ring buffer — control commands like
        # /restart, /stop, /update must not be replayed to a reconnecting
        # plugin, otherwise they create an infinite restart loop.
        async with self._event_push_lock:
            if not event.delivery_id:
                event.delivery_id = uuid.uuid4().hex
            if not event.delivery_ids:
                event.delivery_ids = [event.delivery_id]
            logger.debug(
                "relay push: chat_id=%s message_id=%s delivery_id=%s clients=%d text_len=%d",
                event.chat_id, event.message_id, event.delivery_id, len(self._clients), len(event.text or ""),
            )
            if not (event.text or "").startswith("/"):
                self._ring_buffer.append((time.monotonic(), event))
            return await self._enqueue_or_broadcast(event)

    async def _enqueue_or_broadcast(self, event: NormalizedEvent) -> str:
        """Apply the per-chat queue policy then broadcast.

        排队生效条件(全部满足):
        - chat_id 是群聊(``group:<gid>``);私聊直接放行
        - Hermes 不隔离群成员(``group_sessions_per_user=False``,由插件上报)
        - 适配器排队总开关打开(``event_queue_enabled=True``)
        - 非 /命令(/命令绕过排队,与 ring buffer 同思路)

        排队规则:群未 busy → 标记 busy 并广播;群 busy 时若发送者就是
        当前 busy 用户且队列为空 → 直接广播(刷新 busy 时间戳,不入队,
        仍算同一 session);否则一律入队 FIFO(含 busy 用户自身,避免插队)。
        idle 表示该群 Hermes session 已空闲,一帧即可出队。

        Returns ``"broadcast"``, ``"queued"``, ``"handled"`` or ``"dropped"`` (see push_event).
        """
        gid = self._group_id_of(event)
        command = self._command_name(event.text)
        if command == "clean" and self._config.event_queue_clean_command_enabled:
            cleared = 0
            released_busy = False
            if gid is not None:
                cleared = await self._clear_group_queue(gid)
                released_busy = await self._release_group_busy(gid)
            if gid is None:
                message = "当前会话没有群消息队列。"
            elif released_busy:
                message = f"已清空当前群聊的排队状态，共 {cleared} 条排队消息，并释放 busy。"
            else:
                message = f"已清空当前群聊的消息队列，共 {cleared} 条。"
            await self.send_direct_message(event.chat_id, message, reply_to=event.message_id or None)
            return "handled"
        if gid is None:
            # 私聊 — 不排队
            return await self._broadcast_with_status(event)
        # /命令绕过排队
        if (event.text or "").startswith("/"):
            if (
                command in self._SESSION_RESET_COMMANDS
                and self._config.event_queue_clear_on_session_reset
            ):
                await self._clear_group_queue(gid)
            busy_user = None
            busy_epoch = None
            async with self._get_group_lock(gid):
                busy = self._busy_groups.get(gid)
                if busy is not None:
                    busy_user = busy[0]
                    busy_epoch = self._busy_epoch.get(gid)
            result = await self._broadcast_with_status(event)
            if result == "broadcast" and command in self._INTERRUPT_COMMANDS and busy_user is not None:
                task = asyncio.create_task(
                    self._delayed_stop_cleanup(gid, busy_user, busy_epoch)
                )
                self._text_tasks.add(task)
                task.add_done_callback(self._text_tasks.discard)
                task.add_done_callback(_log_task_exception)
            return result
        # Hermes 隔离群成员 或 适配器总开关关 → 不排队
        if self._hermes_group_sessions_per_user or not self._config.event_queue_enabled:
            return await self._broadcast_with_status(event)
        lock = self._get_group_lock(gid)
        async with lock:
            busy = self._busy_groups.get(gid)
            if busy is None:
                # Idle — claim the group and broadcast immediately.
                self._claim_busy(gid, event.user_id)
                self._ensure_watchdog()
                result = await self._broadcast_with_status(event)
                if result == "dropped":
                    # No client received the event — roll back the busy claim so
                    # the next message (or reconnect replay) can try again.
                    self._busy_groups.pop(gid, None)
                    self._busy_epoch.pop(gid, None)
                    self._schedule_group_lock_cleanup(gid)
                return result
            # Group busy.
            busy_user_id, _ = busy
            q = self._queues.get(gid)
            if event.user_id == busy_user_id and not q:
                # Same sender, nothing waiting: deliver now instead of
                # queueing behind the in-flight turn.  Refresh the busy
                # timestamp so the watchdog follows the latest follow-up.
                # This is still the same Hermes session (redirect/steer/pending
                # drain), so do not treat it as a second turn — one idle
                # frame means the session is free.
                self._busy_groups[gid] = (busy_user_id, time.monotonic())
                logger.info(
                    "relay same-user bypass: gid=%s busy_user=%s delivery_id=%s text_len=%d",
                    gid, busy_user_id, event.delivery_id, len(event.text or ""),
                )
                return await self._broadcast_with_status(event)
            q = self._queues.setdefault(gid, deque())
            cap = self._config.event_queue_max_per_chat
            if len(q) >= cap:
                logger.warning(
                    "relay queue full gid=%s cap=%d delivery_id=%s text_len=%d",
                    gid, cap, event.delivery_id, len(event.text or ""),
                )
                return "dropped"
            q.append(event)
            logger.info(
                "relay enqueue: gid=%s queued=%d busy_user=%s new_user=%s delivery_id=%s text_len=%d",
                gid, len(q), busy_user_id, event.user_id, event.delivery_id, len(event.text or ""),
            )
            return "queued"

    async def _broadcast_with_status(self, event: NormalizedEvent) -> str:
        """Broadcast *event* and return ``"broadcast"`` when there is at
        least one connected client, ``"dropped"`` when there are zero
        clients (the event is logged but not delivered).  The ring buffer
        still holds the event for a future reconnect replay.
        """
        if not self._clients:
            logger.warning(
                "relay broadcast: 0 plugin clients connected — event dropped "
                "chat_id=%s delivery_id=%s text_len=%d",
                event.chat_id, event.delivery_id, len(event.text or ""),
            )
            return "dropped"
        delivered = await self._broadcast_event(event)
        return "dropped" if delivered is False else "broadcast"

    def _get_group_lock(self, gid: str) -> asyncio.Lock:
        """Get or create a per-group lock for queue state protection.

        Serialises access to ``_busy_groups`` / ``_busy_epoch`` and ``_queues``
        for a single group so that ``_enqueue_or_broadcast``, ``_handle_idle``,
        and the watchdog cannot interleave on the same group.
        """
        lock = self._group_locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._group_locks[gid] = lock
        return lock

    def _schedule_group_lock_cleanup(self, gid: str) -> None:
        """Drop idle per-group locks after the current critical section exits."""
        def _cleanup() -> None:
            lock = self._group_locks.get(gid)
            if (
                lock is not None
                and not lock.locked()
                and gid not in self._busy_groups
                and not self._queues.get(gid)
            ):
                self._group_locks.pop(gid, None)

        asyncio.get_running_loop().call_soon(_cleanup)

    @staticmethod
    def _group_id_of(event: NormalizedEvent) -> str | None:
        """Return the bare numeric group id when *event* belongs to a group
        chat (``chat_id`` == ``group:<gid>``).  Returns ``None`` for DMs.

        The returned string preserves leading zeros (e.g. ``"042"``) so that
        busy-slot keys are consistent across ``push_event`` / ``_handle_idle``
        / ``_handle_send`` — never use ``str(int(...))`` for busy-slot keys.
        """
        cid = event.chat_id
        if not cid.startswith("group:"):
            return None
        gid = cid[len("group:"):]
        return gid or None

    @staticmethod
    def _command_name(text: str) -> str | None:
        """Return a normalized leading slash-command name, without ``/``."""
        if not text.startswith("/"):
            return None
        token = text.split(maxsplit=1)[0][1:].lower()
        if "@" in token:
            token = token.split("@", 1)[0]
        if not token or "/" in token:
            return None
        return token

    async def _clear_group_queue(self, gid: str) -> int:
        """Discard pending events for one group without interrupting its active turn."""
        async with self._get_group_lock(gid):
            queued = list(self._queues.pop(gid, ()))
            if not queued:
                self._schedule_group_lock_cleanup(gid)
                return 0

            queued_object_ids = {id(event) for event in queued}
            queued_delivery_ids = {
                delivery_id
                for event in queued
                for delivery_id in (event.delivery_ids or ([event.delivery_id] if event.delivery_id else []))
            }
            for entry in list(self._ring_buffer):
                buffered = entry[1]
                buffered_ids = set(
                    buffered.delivery_ids
                    or ([buffered.delivery_id] if buffered.delivery_id else [])
                )
                if id(buffered) in queued_object_ids or buffered_ids.intersection(queued_delivery_ids):
                    self._ring_buffer.remove(entry)
            logger.info("relay queue cleared: gid=%s discarded=%d", gid, len(queued))
        self._schedule_group_lock_cleanup(gid)
        return len(queued)

    def _claim_busy(self, gid: str, user_id: str) -> None:
        """Mark *gid* busy for *user_id* and bump the slot epoch."""
        self._busy_groups[gid] = (user_id, time.monotonic())
        self._busy_epoch[gid] = self._busy_epoch.get(gid, 0) + 1

    async def _release_group_busy(self, gid: str) -> bool:
        """Drop the busy slot for *gid* without dispatching the next queued event."""
        async with self._get_group_lock(gid):
            released = gid in self._busy_groups
            self._busy_groups.pop(gid, None)
            self._busy_epoch.pop(gid, None)
        self._schedule_group_lock_cleanup(gid)
        return released

    def _ensure_watchdog(self) -> None:
        """Start the busy-timeout watchdog if it isn't already running."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        """Periodically reap busy slots whose idle signal never arrived.

        Guards against plugin crashes / lost idle frames that would otherwise
        leave a group permanently stuck in busy state.  When a busy slot
        exceeds ``event_queue_idle_timeout`` seconds, it is force-cleared and
        the next queued message (if any) is dispatched.
        """
        while True:
            await asyncio.sleep(self._WATCHDOG_INTERVAL)
            try:
                now = time.monotonic()
                timeout = self._config.event_queue_idle_timeout
                for gid in list(self._busy_groups.keys()):
                    async with self._get_group_lock(gid):
                        busy = self._busy_groups.get(gid)
                        if busy is None:
                            continue
                        busy_user, since = busy
                        if now - since < timeout:
                            continue
                        logger.warning(
                            "relay busy watchdog: gid=%s busy_user=%s timeout=%.0fs — force-clearing",
                            gid, busy_user, now - since,
                        )
                        self._dequeue_and_dispatch(gid)
                    self._schedule_group_lock_cleanup(gid)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let the watchdog die — a stuck busy slot is worse
                # than a skipped sweep.  Log and continue.
                logger.exception("relay watchdog loop iteration crashed; continuing")

    def _dequeue_and_dispatch(self, gid: str) -> None:
        """Clear the busy slot for *gid* and, if a queued message exists,
        mark it busy with the queued message's user and broadcast it.

        Synchronous by design — called from idle handler, watchdog, and
        plugin-disconnect cleanup.  Broadcast is scheduled as a task so the
        caller doesn't have to await it.

        If no plugin client is currently connected, the busy slot is cleared
        but the queue is left intact.  The watchdog will retry on its next
        sweep (every ``_WATCHDOG_INTERVAL`` seconds) once a plugin reconnects.
        Note: ``_replay_ring_buffer`` clears queue/busy state on reconnect, so
        queued events from a prior session are discarded there — the ring
        buffer replays the recent events from scratch.
        """
        self._busy_groups.pop(gid, None)
        if not self._clients:
            # No plugin connected — leave the queue in place; the watchdog
            # will retry on its next sweep once a plugin reconnects.
            return
        q = self._queues.get(gid)
        if not q:
            self._busy_epoch.pop(gid, None)
            if q is not None:
                self._queues.pop(gid, None)
            return
        nxt = q.popleft()
        # Queue entries also live in the replay buffer.  Never mutate the
        # original object while constructing a merged dispatch.
        nxt = replace(
            nxt,
            media_items=list(nxt.media_items),
            delivery_ids=list(nxt.delivery_ids or ([nxt.delivery_id] if nxt.delivery_id else [])),
        )
        # Merge consecutive messages from the same user
        merged_count = 1
        while (
            q
            and q[0].user_id == nxt.user_id
            and not nxt.media_items
            and not q[0].media_items
            and not nxt.reply_to_message_id
            and not q[0].reply_to_message_id
            and not nxt.is_system_notice
            and not q[0].is_system_notice
        ):
            next_msg = q.popleft()
            if next_msg.text:
                nxt.text = (nxt.text or "") + "\n\n" + next_msg.text
            nxt.timestamp = next_msg.timestamp
            nxt.message_id = next_msg.message_id
            nxt.real_seq = next_msg.real_seq
            nxt.delivery_ids.extend(
                next_msg.delivery_ids
                or ([next_msg.delivery_id] if next_msg.delivery_id else [])
            )
            merged_count += 1
        if merged_count > 1:
            logger.info(
                "relay dequeue merge: gid=%s user=%s merged=%d delivery_ids=%s text_len=%d",
                gid, nxt.user_id, merged_count, nxt.delivery_ids, len(nxt.text or ""),
            )
        if not q:
            self._queues.pop(gid, None)
        self._claim_busy(gid, nxt.user_id)
        self._ensure_watchdog()
        # Schedule the broadcast + on_dispatch as a single tracked task so
        # stop() can cancel it and _on_dispatch only fires when the event
        # was actually delivered (at least one client received it).
        async def _dispatch_nxt() -> None:
            delivered = await self._broadcast_event(nxt)
            if delivered is not False and self._on_dispatch is not None:
                await self._on_dispatch(nxt)
            elif delivered is False:
                async with self._get_group_lock(gid):
                    current = self._busy_groups.get(gid)
                    if current and current[0] == nxt.user_id:
                        self._busy_groups.pop(gid, None)
                        self._busy_epoch.pop(gid, None)
                self._schedule_group_lock_cleanup(gid)

        task = asyncio.create_task(_dispatch_nxt())
        self._text_tasks.add(task)
        task.add_done_callback(self._text_tasks.discard)
        task.add_done_callback(_log_task_exception)
        logger.info(
            "relay dequeue: gid=%s remaining=%d new_busy_user=%s merged=%d delivery_ids=%s text_len=%d",
            gid, len(self._queues.get(gid, ())), nxt.user_id, merged_count, nxt.delivery_ids, len(nxt.text or ""),
        )

    async def _delayed_stop_cleanup(
        self, gid: str, busy_user: str, busy_epoch: int | None
    ) -> None:
        """Force-clear a busy slot if the gateway does not fire an idle frame
        within ``_STOP_IDLE_DELAY`` seconds after an interrupting command
        (``/stop``, ``/new``, ``/reset``).

        Hermes bumps the session generation when these commands interrupt an
        in-flight turn, which causes the stale run's ``post_delivery_callback``
        to be popped *without* firing (see gateway ``run.py:11099-11112``).
        Without the idle frame the adapter's busy slot is never cleared and
        the queue stalls permanently — the watchdog eventually handles it
        after ``event_queue_idle_timeout`` (default 300 s), but that is far
        too long for interactive use.

        Identity is ``(busy_user, epoch)``, not the busy timestamp.  A bot
        ``send`` (including the "/stop" confirmation) refreshes the
        timestamp via ``_touch_busy_group`` and must not cancel this cleanup.

        This method runs after a short delay.  If the gateway *does* fire
        idle in the meantime, ``_handle_idle`` dequeues first (new epoch or
        no busy slot) and this is a no-op.  The per-group lock prevents the
        two from racing on ``_dequeue_and_dispatch``.
        """
        await asyncio.sleep(self._STOP_IDLE_DELAY)
        async with self._get_group_lock(gid):
            busy = self._busy_groups.get(gid)
            if (
                busy is None
                or busy[0] != busy_user
                or self._busy_epoch.get(gid) != busy_epoch
            ):
                return
            logger.info(
                "relay stop cleanup: gid=%s — no idle after /stop, force-clearing",
                gid,
            )
            self._dequeue_and_dispatch(gid)
        self._schedule_group_lock_cleanup(gid)

    async def _handle_idle(self, data: dict[str, Any]) -> None:
        """Handle an ``idle`` frame from the Hermes plugin.

        The plugin fires this from ``on_processing_complete`` when a
        shared-group session has no pending/debounce follow-up left.  Treat
        it as session-idle: clear busy and dispatch the next queued message.
        """
        gid = str(data.get("group_id", ""))
        if not gid:
            # Fall back to parsing chat_id if group_id absent (defensive).
            # Use the raw substring (preserving leading zeros) so the busy-slot
            # key matches what _group_id_of / _handle_send used when claiming.
            cid = str(data.get("chat_id", ""))
            if cid.startswith("group:"):
                gid = cid[len("group:"):]
            else:
                logger.warning("relay idle frame without group_id, ignoring: %s", data)
                return
        if not gid:
            logger.warning("relay idle frame without group_id, ignoring: %s", data)
            return
        lock = self._get_group_lock(gid)
        async with lock:
            if gid not in self._busy_groups:
                logger.info("relay idle for non-busy gid=%s (already cleared by watchdog/replay/disconnect)", gid)
                return
            logger.info("relay idle: gid=%s — dispatching next queued", gid)
            self._dequeue_and_dispatch(gid)
        self._schedule_group_lock_cleanup(gid)

    async def _broadcast_event(self, event: NormalizedEvent) -> bool:
        """Broadcast *event* to all connected plugin clients.

        Returns ``True`` if at least one client received the event,
        ``False`` if there were zero clients or all sends failed.
        """
        n_clients = len(self._clients)
        if n_clients == 0:
            logger.warning(
                "relay broadcast: 0 plugin clients connected — event dropped "
                "chat_id=%s delivery_id=%s text_len=%d",
                event.chat_id, event.delivery_id, len(event.text or ""),
            )
            return False
        logger.debug("relay broadcast: sending to %d client(s)", n_clients)
        frame = event_message(event)
        logger.debug("relay broadcast event frame: %s", safe_json(frame))
        delivered = False
        for ws in list(self._clients):
            try:
                await ws.send_json(frame)
                delivered = True
            except Exception:
                logger.exception("push_event failed; dropping client")
                self._clients.discard(ws)
        return delivered

    async def _replay_ring_buffer(self, ws: aiohttp.web.WebSocketResponse) -> bool:
        """Send buffered events to a newly-connected plugin.

        Skips events older than ``_RING_BUFFER_MAX_AGE`` seconds so that a
        full gateway restart (which takes longer than a brief WS blip) does
        not replay stale commands like ``/restart`` — which would otherwise
        create an infinite restart loop.

        The replay routes each event through ``_enqueue_or_broadcast`` so that
        shared-group messages serialize: only the first broadcasts, the rest
        enqueue behind a fresh busy slot.  Any pre-existing queue/busy state
        from a prior session is cleared first so the replay rebuilds state
        from scratch — queued events from the dropped session are discarded
        because they're already in the ring buffer and will be re-evaluated.

        Returns ``True`` if every buffered entry was processed without a send
        failure.  Returns ``False`` if a send failed mid-entry: in that case
        the offending entry is purged from the ring buffer and the caller must
        close the now-dirty WebSocket.
        """
        async with self._event_push_lock:
            if not self._ring_buffer:
                return True
            return await self._replay_ring_buffer_locked(ws)

    async def _replay_ring_buffer_locked(self, ws: aiohttp.web.WebSocketResponse) -> bool:
        # Clear any leftover queue state from the previous (now-dead) session
        # so the replay rebuilds busy/queue state cleanly from the buffer.
        self._busy_groups.clear()
        self._busy_epoch.clear()
        self._queues.clear()
        now = time.monotonic()
        cutoff = now - self._RING_BUFFER_MAX_AGE
        # Snapshot before any concurrent push can append; entries added after
        # this point are handled by push_event once the replay lock is released.
        snapshot = list(self._ring_buffer)
        for entry in snapshot:
            ts, event = entry
            if ts < cutoff:
                continue
            # Route through the queue policy so a reconnecting plugin doesn't
            # receive a burst of shared-group messages all at once — only the
            # first one broadcasts, the rest enqueue behind the busy slot.
            # We detect send failures by checking the client set state before
            # vs after: if the ws was dropped during broadcast, the entry is
            # treated as failed and purged.
            ws_before = ws in self._clients
            try:
                await self._enqueue_or_broadcast(event)
            except Exception:
                logger.warning(
                    "ring buffer replay failed; purging entry delivery_id=%s text_len=%d",
                    event.delivery_id, len(event.text or ""),
                    exc_info=True,
                )
                try:
                    self._ring_buffer.remove(entry)
                except ValueError:
                    pass  # already gone (concurrent replay on same ws)
                return False
            # If the ws was dropped during the broadcast (send failure inside
            # _broadcast_event drops the client), treat as failed replay.
            if ws_before and ws not in self._clients:
                logger.warning(
                    "ring buffer replay ws dropped mid-send; purging entry delivery_id=%s text_len=%d",
                    event.delivery_id, len(event.text or ""),
                )
                try:
                    self._ring_buffer.remove(entry)
                except ValueError:
                    pass
                return False
        return True

    # ── Outbound dispatch (plugin -> adapter) ──────────────────────────

    async def _handle_text(self, ws: aiohttp.web.WebSocketResponse, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send_json(error_message("bad_json", "invalid JSON frame"))
            return
        mtype = data.get("type")
        logger.debug(
            "relay recv from plugin: type=%s action=%s frame=%s",
            mtype, data.get("action", ""), safe_json(data),
        )
        if mtype == "ping":
            await ws.send_json(pong_message())
            return
        if mtype == "send":
            await self._handle_send(ws, data)
            return
        if mtype == "api_call":
            await self._handle_api_call(ws, data)
            return
        if mtype == "commands_snapshot":
            self._store_commands(data.get("commands", []) or [])
            return
        if mtype == "hermes_mode_report":
            self._store_hermes_mode(bool(data.get("group_sessions_per_user", True)))
            return
        if mtype == "idle":
            await self._handle_idle(data)
            return
        if mtype == "plugin_info":
            self._store_plugin_version(str(data.get("plugin_version", "")))
            return
        if mtype == "plugin_status":
            level = str(data.get("level", "info")).lower()
            try:
                status_timestamp = float(data.get("timestamp", time.time()))
            except (TypeError, ValueError):
                status_timestamp = time.time()
            status = {
                "level": level if level in {"info", "warning", "error"} else "info",
                "event": str(data.get("event", "unknown"))[:80],
                "message": str(data.get("message", ""))[:500],
                "timestamp": status_timestamp,
            }
            self._latest_plugin_status = status
            log_fn = logger.error if status["level"] == "error" else (
                logger.warning if status["level"] == "warning" else logger.info
            )
            log_fn(
                "Hermes plugin status level=%s event=%s message=%s",
                status["level"], status["event"], status["message"],
            )
            return
        if mtype == "event_ack":
            self._handle_event_ack(data)
            return
        await ws.send_json(error_message("unknown_type", f"unknown type {mtype!r}"))

    def _handle_event_ack(self, data: dict[str, Any]) -> None:
        """Remove successfully processed deliveries from the replay buffer."""
        acked = {str(x) for x in (data.get("delivery_ids") or []) if x}
        single = str(data.get("delivery_id", "") or "")
        if single:
            acked.add(single)
        if not acked:
            return
        for entry in list(self._ring_buffer):
            event = entry[1]
            ids = set(event.delivery_ids or ([event.delivery_id] if event.delivery_id else []))
            if ids and ids.issubset(acked):
                try:
                    self._ring_buffer.remove(entry)
                except ValueError:
                    pass

    async def _handle_send(self, ws: aiohttp.web.WebSocketResponse, data: dict[str, Any]) -> None:
        req_id = data.get("req_id", str(uuid.uuid4()))
        action = data.get("action")
        chat_id = data.get("chat_id", "")
        try:
            is_group, num_id = parse_chat_id(chat_id)
            # ── 去重:Gateway send 超时重试时,插件带新 req_id 重发同样内容 ──
            # 命中且未过期则直接回缓存结果,跳过实际发送/SeqMap/log。
            dedup_hit, dedup_key = await self._check_send_dedup(
                ws, req_id, chat_id, str(action), data,
            )
            if dedup_hit:
                return

            if self._drop_filtered_send(action, data, is_group, num_id):
                if dedup_key is not None:
                    self._send_cache[dedup_key] = (time.monotonic(), "")
                    self._maybe_evict_send_cache()
                await self._touch_busy_group(chat_id, is_group)
                await ws.send_json(result_message(req_id, True, data={"filtered": True}))
                return

            req_token = outbound_log_req_id.set(str(req_id))
            try:
                if action == "send_document":
                    await self._send_document(data, req_id, chat_id, is_group, num_id)
                    if dedup_key is not None:
                        self._send_cache[dedup_key] = (time.monotonic(), "")
                        self._maybe_evict_send_cache()
                    await self._touch_busy_group(chat_id, is_group)
                    await ws.send_json(result_message(req_id, True))
                    return

                segs = _build_send_segments(str(action), data)

                if is_group:
                    async with self._send_api_semaphore:
                        resp = await self._api.send_group_msg(num_id, segs)
                else:
                    async with self._send_api_semaphore:
                        resp = await self._api.send_private_msg(num_id, segs)
                msg_id = str(resp.get("message_id", ""))
                # 写入去重缓存:在 SeqMap/log 之前,确保后续步骤异常时重试仍能命中。
                if dedup_key is not None:
                    self._send_cache[dedup_key] = (time.monotonic(), msg_id)
                    self._maybe_evict_send_cache()
                logger.debug(
                    "relay send response: action=%s chat_id=%s msg_id=%s resp=%s",
                    action, chat_id, msg_id, safe_json(resp, 1000),
                )
            finally:
                outbound_log_req_id.reset(req_token)
            # result frame 必须先回 plugin,SeqMap 补写后置为 fire-and-forget。
            # 原因:get_msg 走同一条 OneBot WS,NapCat 串行处理 API 请求,
            # 多人并发 send 时 get_msg 排队累积延迟会拖慢 result frame 回传,
            # 触发 plugin _RESULT_TIMEOUT(30s)→ Gateway _send_with_retry 重试 →
            # dedup TTL 过期 → 群里重复发送(刷屏)。
            await ws.send_json(result_message(req_id, True, message_id=msg_id))
            # Hermes 发出的任意消息(send_text / 长任务心跳等)都说明 agent 仍在活跃,
            # 顺便刷新该群 busy 槽的时间戳,防止看门狗误判超时。busy 槽用原始群号
            # 字符串作 key(保留前导零),与 _group_id_of / _handle_idle 保持一致。
            await self._touch_busy_group(chat_id, is_group)
            if self._seq_map is not None and is_group and msg_id:
                task = asyncio.create_task(
                    self._populate_seq_map(str(num_id), msg_id),
                    name=f"seq_map_populate:{num_id}:{msg_id}",
                )
                self._text_tasks.add(task)
                task.add_done_callback(self._text_tasks.discard)
                task.add_done_callback(_log_task_exception)
        except ob.UploadOutcomeUnknownError as exc:
            logger.warning("send outcome unknown and must not be retried automatically: %s", exc)
            await ws.send_json(result_message(req_id, False, error=str(exc), retryable=False))
        except Exception as exc:
            logger.exception("send failed")
            await ws.send_json(result_message(req_id, False, error=str(exc)))

    async def _check_send_dedup(
        self,
        ws: aiohttp.web.WebSocketResponse,
        req_id: str,
        chat_id: str,
        action: str,
        data: dict[str, Any],
    ) -> tuple[bool, tuple[str, str, str, str] | None]:
        if not self._config.send_dedup_enabled or action not in _DEDUP_ACTIONS:
            return False, None
        key = (
            chat_id,
            action,
            _send_fingerprint(action, data),
            str(data.get("reply_to", "")),
        )
        cached = self._send_cache.get(key)
        if cached is None:
            return False, key
        cached_ts, cached_msg_id = cached
        age = time.monotonic() - cached_ts
        if age > self._config.send_dedup_ttl_seconds:
            self._send_cache.pop(key, None)
            return False, key
        logger.info(
            "relay dedup hit: action=%s chat_id=%s cached_msg_id=%s age=%.1fs",
            action, chat_id, cached_msg_id, age,
        )
        await ws.send_json(result_message(req_id, True, message_id=cached_msg_id or None))
        return True, key

    async def _send_document(
        self,
        data: dict[str, Any],
        req_id: str,
        chat_id: str,
        is_group: bool,
        num_id: int,
    ) -> None:
        file_ref = str(data.get("file_path", ""))
        if not file_ref:
            raise ValueError("no file_path provided")
        filename = data.get("filename") or file_ref.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        async with self._send_api_semaphore:
            if is_group:
                await self._api.upload_group_file(num_id, file_ref, filename)
            else:
                await self._api.upload_private_file(num_id, file_ref, filename)

        caption_segs: list[dict] = []
        _append_reply_segment(caption_segs, data.get("reply_to"))
        if data.get("caption"):
            caption_segs.append(ob.text_segment(data["caption"]))
        if not caption_segs:
            return
        async with self._send_api_semaphore:
            if is_group:
                await self._api.send_group_msg(num_id, caption_segs)
            else:
                await self._api.send_private_msg(num_id, caption_segs)

    async def _touch_busy_group(self, chat_id: str, is_group: bool) -> None:
        if not is_group or not chat_id.startswith("group:"):
            return
        raw_gid = chat_id[len("group:"):]
        lock = self._get_group_lock(raw_gid)
        async with lock:
            busy = self._busy_groups.get(raw_gid)
            if busy is None:
                return
            busy_user, _ = busy
            self._busy_groups[raw_gid] = (busy_user, time.monotonic())

    async def _populate_seq_map(self, group_id: str, msg_id: str) -> None:
        """Fire-and-forget: fetch real_seq for a bot-sent group message and
        populate the SeqMap so later tool calls can resolve real_seq → message_id.

        Runs after the ``result`` frame has already been sent to the plugin so
        that a slow/queued ``get_msg`` on NapCat's serial WS cannot delay the
        result frame and trigger a Gateway send retry (which caused the
        multi-user flood).  Errors are logged at warning level (vs the
        previous debug) because this task runs detached and silent failures
        would mask a persistent NapCat issue.
        """
        try:
            got = await self._api.get_msg(int(msg_id))
            rs = str(got.get("real_seq", "") or "")
            if rs:
                self._seq_map.add(group_id, int(rs), msg_id)
                logger.debug("seq_map populated: group=%s msg_id=%s real_seq=%s", group_id, msg_id, rs)
            else:
                logger.debug("seq_map populate: no real_seq in get_msg response (msg_id=%s)", msg_id)
        except Exception as exc:
            logger.warning("seq_map populate failed (group=%s msg_id=%s): %s", group_id, msg_id, exc)

    async def _handle_api_call(self, ws: aiohttp.web.WebSocketResponse, data: dict[str, Any]) -> None:
        req_id = data.get("req_id", str(uuid.uuid4()))
        action = data.get("action", "")
        params = data.get("params", {}) or {}
        logger.debug("relay api_call: action=%s req_id=%s", action, req_id)
        logger.debug("relay api_call params: %s", safe_json(params))
        req_token = outbound_log_req_id.set(str(req_id))
        try:
            if self._local_api_call is not None and action.startswith("adapter_"):
                try:
                    result = await self._local_api_call(action, params)
                    await ws.send_json(result_message(req_id, True, data=result))
                except Exception as exc:
                    logger.warning("local api_call %s failed: %s", action, exc)
                    await ws.send_json(result_message(req_id, False, error=str(exc)))
                return
            # 拦截 real_seq → message_id 转换(适配器侧 SeqMap 查询)
            params = self._resolve_seq_params(action, params)
            if action in API_SEND_ACTIONS and self._drop_filtered_api_call(params):
                await ws.send_json(result_message(req_id, True, data={"filtered": True}))
                return
            try:
                result = await self._api.call(action, params)
                logger.debug(
                    "relay api_call result: action=%s ok=True data=%s",
                    action, safe_json(result.get("data")),
                )
                await ws.send_json(result_message(req_id, True, data=result.get("data")))
            except ob.UploadOutcomeUnknownError as exc:
                logger.warning("api_call %s outcome unknown: %s", action, exc)
                await ws.send_json(result_message(req_id, False, error=str(exc), retryable=False))
            except Exception as exc:
                logger.warning("api_call %s failed: %s", action, exc)
                await ws.send_json(result_message(req_id, False, error=str(exc)))
        finally:
            outbound_log_req_id.reset(req_token)

    def _drop_filtered_send(
        self, action: str, data: dict[str, Any], is_group: bool, num_id: int,
    ) -> bool:
        """True when a Hermes ``send`` frame should be silently dropped."""
        group_id = str(num_id) if is_group else None
        if not self._config.resolve_outbound_filter_enabled(group_id):
            return False
        text = extract_send_frame_text(str(action), data)
        pattern = matching_pattern(text, self._config.resolve_outbound_filter_patterns(group_id))
        if pattern is None:
            return False
        logger.info(
            "relay outbound filter hit: action=%s group_id=%s pattern=%s preview=%s",
            action, group_id, pattern, text[: self._config.log_message_preview],
        )
        return True

    def _drop_filtered_api_call(self, params: dict[str, Any]) -> bool:
        """True when a Hermes ``send_msg``-family API call should be dropped."""
        raw_gid = params.get("group_id")
        group_id = str(raw_gid) if raw_gid not in (None, "") else None
        if not self._config.resolve_outbound_filter_enabled(group_id):
            return False
        text = extract_api_message_text(params)
        pattern = matching_pattern(text, self._config.resolve_outbound_filter_patterns(group_id))
        if pattern is None:
            return False
        logger.info(
            "relay outbound filter hit: api_call group_id=%s pattern=%s preview=%s",
            group_id, pattern, text[: self._config.log_message_preview],
        )
        return True

    def _resolve_seq_params(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """拦截需要 real_seq→message_id 转换的 action。

        插件侧工具传的是 ``real_seq`` + ``group_id``(群聊)或 ``user_id``(私聊),
        这里查 SeqMap 转成 ``message_id`` 再调 OneBot API。查不到时透传 real_seq
        当 message_id(兼容 go-cqhttp/Lagrange,它们前缀显示的就是 message_id)。

        ``group_id``/``user_id`` 原样保留传给 OneBot API(``mark_msg_as_read`` 等
        需要 group_id 上下文定位会话)。
        """
        if self._seq_map is None:
            return params
        seq_actions = {
            "cancel_group_todo",
            "complete_group_todo",
            "delete_essence_msg",
            "delete_msg",
            "fetch_emoji_like",
            "forward_friend_single_msg",
            "forward_group_single_msg",
            "get_emoji_likes",
            "get_msg",
            "mark_msg_as_read",
            "set_essence_msg",
            "set_group_todo",
            "set_msg_emoji_like",
        }
        if action not in seq_actions:
            return params
        real_seq = params.pop("real_seq", None)
        if real_seq is None:
            return params  # mark_msg_as_read 可能不传 real_seq(标记全部已读)
        # scope_id:群聊用 group_id,私聊用 user_id(SeqMap 存储约定)
        group_id = params.get("group_id")
        user_id = params.get("user_id")
        scope_id = str(group_id) if group_id else (str(user_id) if user_id else "")
        try:
            seq_int = int(real_seq)
        except (ValueError, TypeError):
            # 非数字 real_seq:放回 real_seq 让 OneBot 报错,而非静默丢数据
            params["real_seq"] = real_seq
            return params
        mid = self._seq_map.lookup(scope_id, seq_int) if scope_id else None
        if mid is not None:
            try:
                params["message_id"] = int(mid)
            except (ValueError, TypeError):
                params["message_id"] = mid
            logger.debug("seq_map resolved: scope=%s seq=%d -> msg_id=%s", scope_id, seq_int, params["message_id"])
        else:
            # 透传:real_seq 当 message_id(go-cqhttp/Lagrange 兼容)
            params["message_id"] = seq_int
            logger.debug("seq_map miss: scope=%s seq=%d -> passthrough as message_id", scope_id, seq_int)
        return params

    async def send_direct_message(self, chat_id: str, message: str, reply_to: str | None = None) -> bool:
        """Send a message directly via OneBot, bypassing the Hermes plugin."""
        try:
            is_group, num_id = parse_chat_id(chat_id)
            segs: list[dict] = []
            if reply_to:
                try:
                    segs.append(ob.reply_segment(int(reply_to)))
                except (ValueError, TypeError):
                    pass
            segs.append(ob.text_segment(message))
            if is_group:
                async with self._send_api_semaphore:
                    resp = await self._api.send_group_msg(num_id, segs)
            else:
                async with self._send_api_semaphore:
                    resp = await self._api.send_private_msg(num_id, segs)
            logger.debug(
                "relay send_direct_message: chat_id=%s ok=True msg_id=%s",
                chat_id, resp.get("message_id", ""),
            )
            return True
        except Exception:
            logger.exception("relay send_direct_message failed chat_id=%s", chat_id)
            return False

    async def send_reject_message(self, chat_id: str, message: str, reply_to: str | None = None) -> bool:
        """Send an adapter-filter rejection directly to the originating chat."""
        return await self.send_direct_message(chat_id, message, reply_to)

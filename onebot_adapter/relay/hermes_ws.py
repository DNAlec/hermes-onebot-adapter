"""WebSocket server endpoint the Hermes plugin connects to.

The plugin authenticates with a bearer token (query param ``token`` or
``Authorization`` header). Inbound OneBot events are pushed here; plugin
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
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import aiohttp.web

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot import api as ob
from onebot_adapter.onebot.log_format import log_send_line
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.seq_map import SeqMap
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

_AT_MARKER_PATTERN = re.compile(r"\{@(\d{5,11})\}")

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
        raw = f"{data.get('audio_path', '')}"
    elif action == "send_video":
        raw = f"{data.get('video_path', '')}|{data.get('caption', '')}"
    elif action == "send_document":
        raw = f"{data.get('file_path', '')}|{data.get('filename', '')}"
    else:  # defensive: never expected since caller filters by _DEDUP_ACTIONS
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse_at_markers(text: str) -> list[dict]:
    """Split text on ``{@QQ号}`` markers, producing at + text segments.

    Example: ``"hello {@123} world"`` →
    ``[text_segment("hello "), at_segment("123"), text_segment(" world")]``
    """
    segs: list[dict] = []
    last_end = 0
    for match in _AT_MARKER_PATTERN.finditer(text):
        if match.start() > last_end:
            segs.append(ob.text_segment(text[last_end:match.start()]))
        segs.append(ob.at_segment(match.group(1)))
        last_end = match.end()
    if last_end < len(text):
        segs.append(ob.text_segment(text[last_end:]))
    if not segs:
        segs.append(ob.text_segment(text))
    return segs


class HermesRelayServer:
    _RING_BUFFER_SIZE = 50
    _RING_BUFFER_MAX_AGE = 30.0  # seconds; skip older events on replay
    _WATCHDOG_INTERVAL = 30.0    # seconds between busy-timeout sweeps

    def __init__(
        self,
        config: AdapterConfig,
        api: Any,
        adapter_version: str,
        onebot_connected_fn: Callable[[], bool],
        on_connect: Callable[[], Any] | None = None,
        on_disconnect: Callable[[], Any] | None = None,
        on_filtered: Callable[[Any], Any] | None = None,
        on_dispatch: Callable[[NormalizedEvent], Awaitable[None]] | None = None,
        seq_map: SeqMap | None = None,
        name_resolver: NameResolver | None = None,
    ) -> None:
        self._config = config
        self._api = api
        self._adapter_version = adapter_version
        self._onebot_connected_fn = onebot_connected_fn
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_filtered = on_filtered
        self._on_dispatch = on_dispatch
        self._seq_map = seq_map
        self._name_resolver = name_resolver
        self._clients: set[aiohttp.web.WebSocketResponse] = set()
        self._ring_buffer: deque[tuple[float, NormalizedEvent]] = deque(
            maxlen=self._RING_BUFFER_SIZE,
        )
        # Slash-command registry pushed by the Hermes plugin.  Maps lowercase
        # command name → CommandInfo dict.  Empty until the first
        # ``commands_snapshot`` frame is received.
        self._commands: dict[str, dict[str, Any]] = {}
        self._commands_aliases: dict[str, str] = {}  # alias → canonical name
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
        self._queues: dict[str, deque[NormalizedEvent]] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        self._plugin_version: str | None = None
        self._version_mismatch: bool = True

    def update_config(self, config: AdapterConfig) -> None:
        """Hot-reload config without rebuilding the server (route stays bound)."""
        # Clear the dedup cache when config changes: a toggled enable flag or
        # shortened TTL could otherwise leave stale entries that no longer
        # match the new policy.
        self._send_cache.clear()
        # Trim any per-chat queues that exceed a newly-lowered cap.
        new_cap = config.event_queue_max_per_chat
        for gid, q in list(self._queues.items()):
            while len(q) > new_cap:
                q.popleft()
                logger.warning("relay queue trimmed (config hot-reload): gid=%s", gid)
        # 排队总开关从 True→False:清空所有 busy/queue,立即放行。
        old_enabled = self._config.event_queue_enabled if hasattr(self, "_config") else True
        if old_enabled and not config.event_queue_enabled:
            if self._busy_groups:
                logger.info(
                    "relay: event_queue_enabled disabled, clearing %d busy group(s)",
                    len(self._busy_groups),
                )
            self._busy_groups.clear()
            self._queues.clear()
        self._config = config

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
        return name in self._commands or name in self._commands_aliases

    def canonical_command_name(self, name: str) -> str:
        """Resolve *name* (possibly an alias) to its canonical command name."""
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
        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()
        # Cancel and await in-flight _handle_text tasks.
        for task in list(self._text_tasks):
            task.cancel()
        if self._text_tasks:
            await asyncio.gather(*self._text_tasks, return_exceptions=True)
        self._text_tasks.clear()
        # Stop the queue watchdog and clear all queue state.
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        self._busy_groups.clear()
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
        """Push an updated self_id to every connected plugin client by sending
        a fresh ``ready`` frame.  Called after _probe_self_id succeeds so that
        plugins that connected before the probe completes see the new self_id."""
        msg = ready_message(
            onebot_connected=self._onebot_connected_fn(),
            adapter_version=self._adapter_version,
            self_id=self_id,
        )
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                logger.warning("relay: failed to send self_id update to a client")
                self._clients.discard(ws)

    async def _handler(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        token = request.query.get("token") or _bearer(request.headers.get("Authorization", ""))
        if not self._config.hermes_ws_token or token != self._config.hermes_ws_token:
            return aiohttp.web.json_response({"error": "unauthorized"}, status=401)
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        if self._on_connect:
            self._on_connect()
        logger.info("Hermes plugin WS connected from %s", request.remote)
        await ws.send_json(
            ready_message(
                onebot_connected=self._onebot_connected_fn(),
                adapter_version=self._adapter_version,
                self_id=self._config.self_id,
            )
        )
        # Replay buffered events so a reconnecting plugin doesn't miss messages.
        replay_ok = await self._replay_ring_buffer(ws)
        if not replay_ok:
            logger.warning("relay: closing plugin WS after ring buffer replay failure")
            await ws.close()
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    task = asyncio.create_task(self._handle_text(ws, msg.data))
                    self._text_tasks.add(task)
                    task.add_done_callback(self._text_tasks.discard)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            self._clients.discard(ws)
            if self._on_disconnect:
                self._on_disconnect()
            # When the last plugin client disconnects, clear all queue state:
            # no one remains to fire idle frames, so busy slots would otherwise
            # hang until the watchdog times them out.  Clearing immediately
            # lets a reconnecting plugin start fresh.
            if not self._clients:
                if self._busy_groups:
                    logger.info(
                        "relay: last plugin disconnected, clearing %d busy group(s)",
                        len(self._busy_groups),
                    )
                self._busy_groups.clear()
                self._queues.clear()
                self._plugin_version = None
                self._version_mismatch = True
            logger.info("Hermes plugin WS disconnected")
        return ws

    # ── Inbound push (adapter -> plugin) ───────────────────────────────

    async def push_event(self, event: NormalizedEvent) -> bool:
        """Push a OneBot event toward the Hermes plugin.

        Writes to the ring buffer (skipping /commands) then routes through
        the queue policy.  Returns ``True`` if the event was enqueued (not
        yet delivered to the plugin), ``False`` if broadcast immediately.
        """
        logger.debug(
            "relay push: chat_id=%s clients=%d text_preview=%r",
            event.chat_id, len(self._clients),
            (event.text or "")[:500],
        )
        # Skip slash commands from the ring buffer — control commands like
        # /restart, /stop, /update must not be replayed to a reconnecting
        # plugin, otherwise they create an infinite restart loop.
        if not event.text.startswith("/"):
            self._ring_buffer.append((time.monotonic(), event))
        return await self._enqueue_or_broadcast(event)

    async def _enqueue_or_broadcast(self, event: NormalizedEvent) -> bool:
        """Apply the per-chat queue policy then broadcast.

        排队生效条件(全部满足):
        - chat_id 是群聊(``group:<gid>``);私聊直接放行
        - Hermes 不隔离群成员(``group_sessions_per_user=False``,由插件上报)
        - 适配器排队总开关打开(``event_queue_enabled=True``)
        - 非 /命令(/命令绕过排队,与 ring buffer 同思路)

        排队规则:群未 busy → 标记 busy 并广播;群 busy 且新消息来自同人 → 放行
        (可补充当前任务);群 busy 且来自不同人 → 入队 FIFO。

        Returns ``True`` when the event was enqueued, ``False`` when broadcast.
        """
        gid = self._group_id_of(event)
        if gid is None:
            # 私聊 — 不排队
            await self._broadcast_event(event)
            return False
        # /命令绕过排队
        if event.text.startswith("/"):
            await self._broadcast_event(event)
            return False
        # Hermes 隔离群成员 或 适配器总开关关 → 不排队
        if self._hermes_group_sessions_per_user or not self._config.event_queue_enabled:
            await self._broadcast_event(event)
            return False
        busy = self._busy_groups.get(gid)
        if busy is None:
            # Idle — claim the group and broadcast immediately.
            self._busy_groups[gid] = (event.user_id, time.monotonic())
            self._ensure_watchdog()
            await self._broadcast_event(event)
            return False
        busy_user_id, _since = busy
        if event.user_id == busy_user_id:
            # Same user as the in-flight turn — let it through.
            await self._broadcast_event(event)
            return False
        # Different user while busy — enqueue.
        q = self._queues.setdefault(gid, deque())
        cap = self._config.event_queue_max_per_chat
        if len(q) >= cap:
            logger.warning(
                "relay queue full (gid=%s cap=%d), dropping incoming text_preview=%r",
                gid, cap, (event.text or "")[:120],
            )
            return False
        q.append(event)
        logger.info(
            "relay enqueue: gid=%s queued=%d busy_user=%s new_user=%s text_preview=%r",
            gid, len(q), busy_user_id, event.user_id,
            (event.text or "")[:120],
        )
        return True

    @staticmethod
    def _group_id_of(event: NormalizedEvent) -> str | None:
        """Return the bare numeric group id when *event* belongs to a group
        chat (``chat_id`` == ``group:<gid>``).  Returns ``None`` for DMs.
        """
        cid = event.chat_id
        if not cid.startswith("group:"):
            return None
        gid = cid[len("group:"):]
        return gid or None

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
            now = time.monotonic()
            timeout = self._config.event_queue_idle_timeout
            for gid in list(self._busy_groups.keys()):
                busy_user, since = self._busy_groups.get(gid, ("", now))
                if now - since < timeout:
                    continue
                logger.warning(
                    "relay busy watchdog: gid=%s busy_user=%s timeout=%.0fs — force-clearing",
                    gid, busy_user, now - since,
                )
                self._dequeue_and_dispatch(gid)

    def _dequeue_and_dispatch(self, gid: str) -> None:
        """Clear the busy slot for *gid* and, if a queued message exists,
        mark it busy with the queued message's user and broadcast it.

        Synchronous by design — called from idle handler, watchdog, and
        plugin-disconnect cleanup.  Broadcast is scheduled as a task so the
        caller doesn't have to await it.
        """
        self._busy_groups.pop(gid, None)
        q = self._queues.get(gid)
        if not q:
            if q is not None:
                self._queues.pop(gid, None)
            return
        nxt = q.popleft()
        if not q:
            self._queues.pop(gid, None)
        self._busy_groups[gid] = (nxt.user_id, time.monotonic())
        self._ensure_watchdog()
        asyncio.create_task(self._broadcast_event(nxt))
        if self._on_dispatch is not None:
            asyncio.create_task(self._on_dispatch(nxt))
        logger.info(
            "relay dequeue: gid=%s remaining=%d new_busy_user=%s text_preview=%r",
            gid, len(self._queues.get(gid, ())), nxt.user_id,
            (nxt.text or "")[:120],
        )

    async def _handle_idle(self, data: dict[str, Any]) -> None:
        """Handle an ``idle`` frame from the Hermes plugin.

        The plugin fires this via its ``register_post_delivery_callback``
        hook after a shared-group turn finishes.  We clear the busy slot for
        the group and dispatch the next queued message (if any).
        """
        gid = str(data.get("group_id", ""))
        if not gid:
            # Fall back to parsing chat_id if group_id absent (defensive).
            cid = str(data.get("chat_id", ""))
            try:
                _, num_id = parse_chat_id(cid)
                gid = str(num_id)
            except (ValueError, TypeError):
                logger.warning("relay idle frame without group_id, ignoring: %s", data)
                return
        if gid not in self._busy_groups:
            logger.debug("relay idle for non-busy gid=%s (already cleared)", gid)
            return
        logger.debug("relay idle: gid=%s — dispatching next queued", gid)
        self._dequeue_and_dispatch(gid)

    async def _broadcast_event(self, event: NormalizedEvent) -> None:
        logger.debug("relay broadcast: sending to %d client(s)", len(self._clients))
        frame = event_message(event)
        logger.debug("relay broadcast event frame: %s", json.dumps(frame, ensure_ascii=False)[:2000])
        for ws in list(self._clients):
            try:
                await ws.send_json(frame)
            except Exception:
                logger.exception("push_event failed; dropping client")
                self._clients.discard(ws)

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
        if not self._ring_buffer:
            return True
        # Clear any leftover queue state from the previous (now-dead) session
        # so the replay rebuilds busy/queue state cleanly from the buffer.
        self._busy_groups.clear()
        self._queues.clear()
        now = time.monotonic()
        cutoff = now - self._RING_BUFFER_MAX_AGE
        # Iterate over a snapshot: we may purge an entry below.
        for entry in list(self._ring_buffer):
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
                    "ring buffer replay failed; purging corrupted entry "
                    "(text_preview=%r)",
                    (event.text or "")[:120],
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
                    "ring buffer replay ws dropped mid-send; purging entry "
                    "(text_preview=%r)",
                    (event.text or "")[:120],
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
        logger.debug("relay recv from plugin: type=%s action=%s raw=%s", mtype, data.get("action", ""), raw[:2000])
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
        await ws.send_json(error_message("unknown_type", f"unknown type {mtype!r}"))

    async def _handle_send(self, ws: aiohttp.web.WebSocketResponse, data: dict[str, Any]) -> None:
        req_id = data.get("req_id", str(uuid.uuid4()))
        action = data.get("action")
        chat_id = data.get("chat_id", "")
        try:
            is_group, num_id = parse_chat_id(chat_id)
            segs: list[dict] = []
            # ── 去重:Gateway send 超时重试时,插件带新 req_id 重发同样内容 ──
            # 命中且未过期则直接回缓存结果,跳过实际发送/SeqMap/log。
            dedup_key: tuple[str, str, str, str] | None = None
            if self._config.send_dedup_enabled and action in _DEDUP_ACTIONS:
                dedup_key = (
                    chat_id, action,
                    _send_fingerprint(action, data),
                    str(data.get("reply_to", "")),
                )
                cached = self._send_cache.get(dedup_key)
                if cached is not None:
                    cached_ts, cached_msg_id = cached
                    age = time.monotonic() - cached_ts
                    if age <= self._config.send_dedup_ttl_seconds:
                        logger.info(
                            "relay dedup hit: action=%s chat_id=%s cached_msg_id=%s age=%.1fs",
                            action, chat_id, cached_msg_id, age,
                        )
                        await ws.send_json(
                            result_message(req_id, True, message_id=cached_msg_id or None)
                        )
                        return
                    self._send_cache.pop(dedup_key, None)  # expired, fall through

            if action == "send_text":
                if data.get("reply_to"):
                    try:
                        segs.append(ob.reply_segment(int(data["reply_to"])))
                    except (ValueError, TypeError):
                        pass
                content = data.get("content", "")
                segs.extend(_parse_at_markers(content))

            elif action == "send_image":
                file_ref = str(data.get("image_url", ""))
                if not file_ref:
                    raise ValueError("no image_url provided")
                segs.append(ob.image_segment(file_ref))
                if data.get("caption"):
                    segs.append(ob.text_segment(data["caption"]))

            elif action == "send_voice":
                file_ref = str(data.get("audio_path", ""))
                if not file_ref:
                    raise ValueError("no audio_path provided")
                segs.append(ob.record_segment(file_ref))

            elif action == "send_video":
                file_ref = str(data.get("video_path", ""))
                if not file_ref:
                    raise ValueError("no video_path provided")
                segs.append(ob.video_segment(file_ref))
                if data.get("caption"):
                    segs.append(ob.text_segment(data["caption"]))

            elif action == "send_document":
                file_ref = str(data.get("file_path", ""))
                if not file_ref:
                    raise ValueError("no file_path provided")
                filename = data.get("filename") or file_ref.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                group_name = ""
                if is_group and self._name_resolver:
                    try:
                        group_name = await self._name_resolver.resolve_group_name(str(num_id))
                    except Exception:
                        pass
                await log_send_line(
                    chat_id=chat_id,
                    segs=[{"type": "file", "data": {"name": filename}}],
                    is_group=is_group, group_name=group_name,
                    reply_to=data.get("reply_to"),
                    preview=self._config.log_message_preview,
                    name_resolver=self._name_resolver,
                )
                if is_group:
                    await self._api.upload_group_file(num_id, file_ref, filename)
                else:
                    await self._api.upload_private_file(num_id, file_ref, filename)
                # 写入去重缓存(send_document 无 message_id,缓存空串;命中时回 None)。
                if dedup_key is not None:
                    self._send_cache[dedup_key] = (time.monotonic(), "")
                if is_group and str(num_id) in self._busy_groups:
                    busy_user, _ = self._busy_groups[str(num_id)]
                    self._busy_groups[str(num_id)] = (busy_user, time.monotonic())
                await ws.send_json(result_message(req_id, True))
                return

            else:
                await ws.send_json(result_message(req_id, False, error=f"unknown action {action!r}"))
                return

            if is_group:
                resp = await self._api.send_group_msg(num_id, segs)
            else:
                resp = await self._api.send_private_msg(num_id, segs)
            msg_id = str(resp.get("message_id", ""))
            # 写入去重缓存:在 SeqMap/log 之前,确保后续步骤异常时重试仍能命中。
            if dedup_key is not None:
                self._send_cache[dedup_key] = (time.monotonic(), msg_id)
            logger.debug(
                "relay send response: action=%s chat_id=%s msg_id=%s resp=%s",
                action, chat_id, msg_id, json.dumps(resp, ensure_ascii=False)[:1000],
            )
            group_name = ""
            if is_group and self._name_resolver:
                try:
                    group_name = await self._name_resolver.resolve_group_name(str(num_id))
                except Exception:
                    pass
            await log_send_line(
                chat_id=chat_id, segs=segs, is_group=is_group,
                group_name=group_name, reply_to=data.get("reply_to"),
                preview=self._config.log_message_preview,
                name_resolver=self._name_resolver,
            )
            # result frame 必须先回 plugin,SeqMap 补写后置为 fire-and-forget。
            # 原因:get_msg 走同一条 OneBot WS,NapCat 串行处理 API 请求,
            # 多人并发 send 时 get_msg 排队累积延迟会拖慢 result frame 回传,
            # 触发 plugin _RESULT_TIMEOUT(30s)→ Gateway _send_with_retry 重试 →
            # dedup TTL 过期 → 群里重复发送(刷屏)。
            await ws.send_json(result_message(req_id, True, message_id=msg_id))
            # Hermes 发出的任意消息(send_text / 长任务心跳等)都说明 agent 仍在活跃,
            # 顺便刷新该群 busy 槽的时间戳,防止看门狗误判超时。
            gid_str = str(num_id)
            if is_group and gid_str in self._busy_groups:
                busy_user, _ = self._busy_groups[gid_str]
                self._busy_groups[gid_str] = (busy_user, time.monotonic())
            if self._seq_map is not None and is_group and msg_id:
                task = asyncio.create_task(
                    self._populate_seq_map(str(num_id), msg_id),
                    name=f"seq_map_populate:{num_id}:{msg_id}",
                )
                self._text_tasks.add(task)
                task.add_done_callback(self._text_tasks.discard)
        except Exception as exc:
            logger.exception("send failed")
            await ws.send_json(result_message(req_id, False, error=str(exc)))

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
        logger.debug("relay api_call params: %s", json.dumps(params, ensure_ascii=False)[:2000])
        # 拦截 real_seq → message_id 转换(适配器侧 SeqMap 查询)
        params = self._resolve_seq_params(action, params)
        try:
            result = await self._api.call(action, params)
            logger.debug(
                "relay api_call result: action=%s ok=True data=%s",
                action, json.dumps(result.get("data"), ensure_ascii=False)[:2000],
            )
            await ws.send_json(result_message(req_id, True, data=result.get("data")))
        except Exception as exc:
            logger.warning("api_call %s failed: %s", action, exc)
            await ws.send_json(result_message(req_id, False, error=str(exc)))

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
        seq_actions = {"get_msg", "delete_msg", "set_msg_emoji_like", "mark_msg_as_read"}
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

    async def send_reject_message(self, chat_id: str, message: str, reply_to: str | None = None) -> bool:
        """Send a reject reply directly via the OneBot HTTP API (bypassing
        the Hermes plugin).  Used by the command filter to notify users that
        their /command was denied.  Returns True on success."""
        try:
            is_group, num_id = parse_chat_id(chat_id)
            segs: list[dict] = []
            if reply_to:
                try:
                    segs.append(ob.reply_segment(int(reply_to)))
                except (ValueError, TypeError):
                    pass
            segs.extend(_parse_at_markers(message))
            if is_group:
                resp = await self._api.send_group_msg(num_id, segs)
            else:
                resp = await self._api.send_private_msg(num_id, segs)
            logger.debug(
                "relay send_reject_message: chat_id=%s ok=True msg_id=%s",
                chat_id, resp.get("message_id", ""),
            )
            return True
        except Exception:
            logger.exception("relay send_reject_message failed chat_id=%s", chat_id)
            return False


def _bearer(header: str) -> str:
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""

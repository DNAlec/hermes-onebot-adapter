"""WebSocket server endpoint the Hermes plugin connects to.

The plugin authenticates with a bearer token (query param ``token`` or
``Authorization`` header). Inbound OneBot events are pushed here; plugin
send / api-call requests are dispatched to the OneBot HTTP API.

Binary media flow (plugin -> adapter):
  1. Plugin sends ``send_media`` text frame announcing id/mime/name.
  2. Plugin sends one binary frame with the bytes.
  3. Plugin sends a ``send`` frame referencing ``media_id``.
The adapter writes the bytes to a temp file and passes a ``file://`` URI to
OneBot's OneBot HTTP API.
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
from collections.abc import Callable
from typing import Any

import aiohttp
import aiohttp.web

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot import api as ob
from onebot_adapter.onebot.log_format import log_send_line
from onebot_adapter.onebot.media import cleanup_temp_uri, write_temp_media
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.seq_map import SeqMap
from onebot_adapter.relay.protocol import (
    MediaDescriptor,
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
        raw = f"{data.get('image_url', '')}|{data.get('media_id', '')}|{data.get('caption', '')}"
    elif action == "send_voice":
        raw = f"{data.get('audio_path', '')}|{data.get('media_id', '')}"
    elif action == "send_video":
        raw = f"{data.get('video_path', '')}|{data.get('media_id', '')}|{data.get('caption', '')}"
    elif action == "send_document":
        raw = f"{data.get('file_path', '')}|{data.get('media_id', '')}|{data.get('filename', '')}"
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


class _Connection:
    """Per-WS-connection state, tracking pending binary media frames."""

    def __init__(self, ws: aiohttp.web.WebSocketResponse) -> None:
        self.ws = ws
        self._buffer: dict[str, bytes] = {}
        self._pending: deque[str] = deque()

    def announce_media(self, desc: MediaDescriptor) -> None:
        self._buffer[desc.id] = b""
        self._pending.append(desc.id)

    def receive_binary(self, data: bytes) -> None:
        if not self._pending:
            logger.warning("relay: unexpected binary frame, dropping")
            return
        mid = self._pending.popleft()
        self._buffer[mid] = data

    def take_media(self, media_id: str) -> bytes | None:
        return self._buffer.pop(media_id, None)


class HermesRelayServer:
    _RING_BUFFER_SIZE = 50
    _RING_BUFFER_MAX_AGE = 30.0  # seconds; skip older events on replay

    def __init__(
        self,
        config: AdapterConfig,
        api: Any,
        adapter_version: str,
        onebot_connected_fn: Callable[[], bool],
        on_connect: Callable[[], Any] | None = None,
        on_disconnect: Callable[[], Any] | None = None,
        on_filtered: Callable[[Any], Any] | None = None,
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
        self._seq_map = seq_map
        self._name_resolver = name_resolver
        self._clients: dict[aiohttp.web.WebSocketResponse, _Connection] = {}
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

    def update_config(self, config: AdapterConfig) -> None:
        """Hot-reload config without rebuilding the server (route stays bound)."""
        # Clear the dedup cache when config changes: a toggled enable flag or
        # shortened TTL could otherwise leave stale entries that no longer
        # match the new policy.
        self._send_cache.clear()
        self._config = config

    @property
    def commands(self) -> list[dict[str, Any]]:
        """Return the current slash-command registry as a list of dicts."""
        return list(self._commands.values())

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

    async def broadcast_commands_refresh(self) -> None:
        """Send a ``commands_refresh`` frame to every connected plugin client,
        asking it to re-collect and push a fresh commands_snapshot."""
        from onebot_adapter.relay.protocol import commands_refresh_message

        for ws in list(self._clients):
            try:
                await ws.send_json(commands_refresh_message())
            except Exception:
                logger.warning("relay: failed to send commands_refresh to a client")
                self._clients.pop(ws, None)

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
                self._clients.pop(ws, None)

    async def _handler(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        token = request.query.get("token") or _bearer(request.headers.get("Authorization", ""))
        if not self._config.hermes_ws_token or token != self._config.hermes_ws_token:
            return aiohttp.web.json_response({"error": "unauthorized"}, status=401)
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        conn = _Connection(ws)
        self._clients[ws] = conn
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
        # If replay hits a corrupted buffer entry, the WS is already dirty —
        # close it so the message loop exits cleanly instead of thrashing;
        # the bad entry has been purged, so the next reconnect replays clean.
        replay_ok = await self._replay_ring_buffer(ws)
        if not replay_ok:
            logger.warning("relay: closing plugin WS after ring buffer replay failure")
            await ws.close()
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    task = asyncio.create_task(self._handle_text(conn, msg.data))
                    self._text_tasks.add(task)
                    task.add_done_callback(self._text_tasks.discard)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    conn.receive_binary(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            self._clients.pop(ws, None)
            if self._on_disconnect:
                self._on_disconnect()
            logger.info("Hermes plugin WS disconnected")
        return ws

    # ── Inbound push (adapter -> plugin) ───────────────────────────────

    async def push_event(self, event: NormalizedEvent) -> None:
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
        await self._broadcast_event(event)

    async def _broadcast_event(self, event: NormalizedEvent) -> None:
        logger.debug("relay broadcast: sending to %d client(s)", len(self._clients))
        frame = event_message(event)
        logger.debug("relay broadcast event frame: %s", json.dumps(frame, ensure_ascii=False)[:2000])
        for ws in list(self._clients):
            try:
                await ws.send_json(frame)
            except Exception:
                logger.exception("push_event failed; dropping client")
                self._clients.pop(ws, None)

    async def _replay_ring_buffer(self, ws: aiohttp.web.WebSocketResponse) -> bool:
        """Send buffered events to a newly-connected plugin.

        Skips events older than ``_RING_BUFFER_MAX_AGE`` seconds so that a
        full gateway restart (which takes longer than a brief WS blip) does
        not replay stale commands like ``/restart`` — which would otherwise
        create an infinite restart loop.

        Returns ``True`` if every buffered entry was sent successfully.
        Returns ``False`` if a send failed mid-entry: in that case the
        offending entry is purged from the ring buffer and the caller must
        close the now-dirty WebSocket.
        """
        if not self._ring_buffer:
            return True
        now = time.monotonic()
        cutoff = now - self._RING_BUFFER_MAX_AGE
        # Iterate over a snapshot: we may purge an entry below.
        for entry in list(self._ring_buffer):
            ts, event = entry
            if ts < cutoff:
                continue
            try:
                await ws.send_json(event_message(event))
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
        return True

    # ── Outbound dispatch (plugin -> adapter) ──────────────────────────

    async def _handle_text(self, conn: _Connection, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await conn.ws.send_json(error_message("bad_json", "invalid JSON frame"))
            return
        mtype = data.get("type")
        logger.debug("relay recv from plugin: type=%s action=%s raw=%s", mtype, data.get("action", ""), raw[:2000])
        if mtype == "ping":
            await conn.ws.send_json(pong_message())
            return
        if mtype == "send_media":
            desc = MediaDescriptor(
                id=data.get("id", str(uuid.uuid4())),
                mime=data.get("mime", "application/octet-stream"),
                name=data.get("name", ""),
                size=int(data.get("size", 0)),
            )
            conn.announce_media(desc)
            return
        if mtype == "send":
            await self._handle_send(conn, data)
            return
        if mtype == "api_call":
            await self._handle_api_call(conn, data)
            return
        if mtype == "commands_snapshot":
            self._store_commands(data.get("commands", []) or [])
            return
        await conn.ws.send_json(error_message("unknown_type", f"unknown type {mtype!r}"))

    async def _handle_send(self, conn: _Connection, data: dict[str, Any]) -> None:
        req_id = data.get("req_id", str(uuid.uuid4()))
        action = data.get("action")
        chat_id = data.get("chat_id", "")
        temp_uris: list[str] = []
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
                        await conn.ws.send_json(
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
                file_ref = await self._resolve_file_ref(conn, data, temp_uris, default_ext=".jpg")
                segs.append(ob.image_segment(file_ref))
                if data.get("caption"):
                    segs.append(ob.text_segment(data["caption"]))

            elif action == "send_voice":
                file_ref = await self._resolve_file_ref(conn, data, temp_uris, default_ext=".wav")
                segs.append(ob.record_segment(file_ref))

            elif action == "send_video":
                file_ref = await self._resolve_file_ref(conn, data, temp_uris, default_ext=".mp4")
                segs.append(ob.video_segment(file_ref))
                if data.get("caption"):
                    segs.append(ob.text_segment(data["caption"]))

            elif action == "send_document":
                file_ref, filename = await self._resolve_file_ref_with_name(conn, data, temp_uris)
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
                    await self._api.upload_group_file(num_id, _local_path(file_ref), filename)
                else:
                    await self._api.upload_private_file(num_id, _local_path(file_ref), filename)
                # 写入去重缓存(send_document 无 message_id,缓存空串;命中时回 None)。
                if dedup_key is not None:
                    self._send_cache[dedup_key] = (time.monotonic(), "")
                await conn.ws.send_json(result_message(req_id, True))
                return

            else:
                await conn.ws.send_json(result_message(req_id, False, error=f"unknown action {action!r}"))
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
            # bot 自发消息也存入 SeqMap:调 get_msg 拿 real_seq
            if self._seq_map is not None and is_group and msg_id:
                try:
                    got = await self._api.get_msg(int(msg_id))
                    rs = str(got.get("real_seq", "") or "")
                    if rs:
                        self._seq_map.add(str(num_id), int(rs), msg_id)
                except Exception as exc:
                    logger.debug("get_msg for seq_map failed (msg_id=%s): %s", msg_id, exc)
            await conn.ws.send_json(result_message(req_id, True, message_id=msg_id))
        except Exception as exc:
            logger.exception("send failed")
            await conn.ws.send_json(result_message(req_id, False, error=str(exc)))
        finally:
            for uri in temp_uris:
                cleanup_temp_uri(uri)

    async def _resolve_file_ref(
        self, conn: _Connection, data: dict[str, Any], temp_uris: list[str], *, default_ext: str
    ) -> str:
        """Return a file reference for OneBot segments: URL, path, or temp file URI."""
        if data.get("image_url") or data.get("audio_path") or data.get("video_path"):
            return str(data.get("image_url") or data.get("audio_path") or data.get("video_path"))
        media_id = data.get("media_id")
        if media_id:
            raw = conn.take_media(media_id)
            if raw is None:
                raise ValueError(f"media_id {media_id!r} not found")
            name = data.get("filename") or data.get("name") or f"upload{default_ext}"
            uri = await write_temp_media(raw, name, data.get("mime", ""))
            temp_uris.append(uri)
            return uri
        raise ValueError("no image_url / audio_path / video_path / media_id provided")

    async def _resolve_file_ref_with_name(
        self, conn: _Connection, data: dict[str, Any], temp_uris: list[str]
    ) -> tuple[str, str]:
        if data.get("file_path"):
            name = data.get("filename") or data["file_path"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            return str(data["file_path"]), name
        media_id = data.get("media_id")
        if media_id:
            raw = conn.take_media(media_id)
            if raw is None:
                raise ValueError(f"media_id {media_id!r} not found")
            name = data.get("filename") or data.get("name") or "document"
            uri = await write_temp_media(raw, name, data.get("mime", ""))
            temp_uris.append(uri)
            return uri, name
        raise ValueError("no file_path / media_id provided")

    async def _handle_api_call(self, conn: _Connection, data: dict[str, Any]) -> None:
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
            await conn.ws.send_json(result_message(req_id, True, data=result.get("data")))
        except Exception as exc:
            logger.warning("api_call %s failed: %s", action, exc)
            await conn.ws.send_json(result_message(req_id, False, error=str(exc)))

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


def _local_path(uri: str) -> str:
    if uri.startswith("file://"):
        return uri[7:]
    return uri

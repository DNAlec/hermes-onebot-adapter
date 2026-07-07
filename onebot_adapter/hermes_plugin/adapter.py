"""OneBot platform plugin for Hermes Agent.

Connects to the standalone ``onebot_adapter`` service via WebSocket.  The
adapter service handles all OneBot interaction; this plugin only
translates between the adapter's normalized protocol and Hermes'
``BasePlatformAdapter`` interface.

Configuration (env vars or config.yaml ``platforms.onebot.extra``):

    ONEBOT_ADAPTER_URL   ws://host:port/hermes
    ONEBOT_ADAPTER_TOKEN  bearer token (shown in the adapter WebUI)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from .markdown import strip_markdown

logger = logging.getLogger(__name__)

# ── Lazy imports from the Hermes host ────────────────────────────────────
# These live in the main Hermes repo and are only available when the plugin
# is loaded inside a running gateway.  We import at module level inside a
# try/except so the file is still importable for standalone tooling.

try:
    from gateway.config import Platform
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    from gateway.session import SessionSource, build_session_key
    _BASE_AVAILABLE = True
except ImportError:
    _BASE_AVAILABLE = False
    BasePlatformAdapter = object  # type: ignore[misc,assignment]
    MessageEvent = None  # type: ignore[assignment]
    MessageType = None  # type: ignore[assignment]
    SendResult = None  # type: ignore[assignment]
    Platform = None  # type: ignore[assignment]
    SessionSource = None  # type: ignore[assignment]
    build_session_key = None  # type: ignore[assignment]

_QQ_TEXT_LIMIT = 4500
_RESULT_TIMEOUT = 30.0
_RECONNECT_INITIAL_DELAY = 1.0
_RECONNECT_MAX_DELAY = 30.0
# Maximum concurrent in-flight send requests (send_text/send_image/...).
# Each send awaits a ``result`` frame from the adapter with a 30s timeout.
# Without a limit, Gateway ``_send_with_retry`` retries pile up as parallel
# ``_request`` coroutines, all hitting the serial OneBot WS and amplifying
# congestion (death spiral).  Limiting to 2 keeps retries orderly: one
# in-flight + one retry max, matching the adapter-side semaphore.
_MAX_INFLIGHT_SENDS = 2

_PLUGIN_YAML_PATH = Path(__file__).parent / "plugin.yaml"
_VERSION_RE = re.compile(r"^version:\s*[\"']?([^\"'\n#]+)[\"']?", re.MULTILINE)


def _read_plugin_version() -> str:
    try:
        text = _PLUGIN_YAML_PATH.read_text(encoding="utf-8")
        m = _VERSION_RE.search(text)
        if m:
            return m.group(1).strip().strip("\"'")
    except (OSError, FileNotFoundError):
        pass
    return "unknown"


class OneBotAdapter(BasePlatformAdapter):  # type: ignore[misc]
    """Hermes platform adapter for OneBot 11 via the adapter service."""

    MAX_MESSAGE_LENGTH = _QQ_TEXT_LIMIT

    @property
    def authorization_is_upstream(self) -> bool:
        """The adapter service enforces all access control (DM/group allowlists,
        command permissions) before forwarding events over its token-authenticated
        WebSocket. The gateway can trust messages that reach it here.
        """
        return True

    def __init__(self, config) -> None:
        super().__init__(config, Platform("onebot"))
        extra = getattr(config, "extra", {}) or {}

        self._adapter_url = (
            os.getenv("ONEBOT_ADAPTER_URL")
            or extra.get("adapter_url", "")
        ).rstrip("/")
        self._adapter_token = (
            os.getenv("ONEBOT_ADAPTER_TOKEN")
            or extra.get("adapter_token", "")
        )

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Background tasks spawned by _handle_event to run handle_message off
        # the receive loop (avoids the bypass-command self-deadlock).  Tracked
        # so disconnect() can cancel them cleanly.
        self._event_tasks: set[asyncio.Task[None]] = set()
        # Limit concurrent in-flight send requests to prevent retry storms
        # from overwhelming the serial OneBot WS API.  See _MAX_INFLIGHT_SENDS.
        self._send_semaphore = asyncio.Semaphore(_MAX_INFLIGHT_SENDS)
        self._onebot_connected = False
        self._self_id = ""
        self._current_is_admin = False
        self._current_group_id = ""
        self._current_user_id = ""
        self._plugin_version = _read_plugin_version()

        # Inject self into onebot_tools so tool handlers can call _api_call
        try:
            from .onebot_tools import set_adapter
            set_adapter(self)
        except ImportError:
            pass

    @property
    def name(self) -> str:
        return "OneBot"

    # ── Connection lifecycle ─────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # ``is_reconnect`` is part of the BasePlatformAdapter.connect contract
        # (gateway/run.py reconnect watcher passes is_reconnect=True when
        # re-establishing a platform after an outage). OneBot has no
        # server-side update queue — dropped WS events are gone, and the
        # adapter service's ring buffer already replays recent events to a
        # reconnecting plugin regardless of this flag — so we ignore it,
        # matching RelayAdapter (gateway/relay/adapter.py:126-135).
        if not self._adapter_url:
            logger.error("OneBot: ONEBOT_ADAPTER_URL not configured")
            self._set_fatal_error(
                "config_missing",
                "ONEBOT_ADAPTER_URL must be set",
                retryable=False,
            )
            return False

        self._session = aiohttp.ClientSession()
        try:
            await self._ws_connect()
        except Exception as exc:
            logger.error("OneBot: failed to connect to adapter service: %s", exc)
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False

        self._stop_event.clear()
        self._recv_task = asyncio.create_task(self._receive_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog())
        self._is_connected = True
        logger.info("OneBot: connected to adapter service at %s", self._adapter_url)
        # Push the current slash-command registry to the adapter service so it
        # can filter /commands before forwarding messages.  Sent after the
        # receive loop is started so any commands_refresh request from the
        # adapter can also be handled.
        asyncio.create_task(self._push_commands_snapshot())
        # Push Hermes' group_sessions_per_user so the adapter can decide
        # whether shared-group queueing is needed.
        asyncio.create_task(self._push_hermes_mode_report())
        # Push installed plugin version so the adapter can detect mismatches.
        asyncio.create_task(self._push_plugin_info())
        return True

    async def _ws_connect(self) -> None:
        assert self._session is not None
        url = self._adapter_url
        if "?" in url:
            url += f"&token={self._adapter_token}"
        else:
            url += f"?token={self._adapter_token}"
        # heartbeat=30 enables aiohttp-level PING/PONG keepalive so that a
        # silent network drop (NAT idle reaping, suspend/resume, Wi-Fi roam)
        # is detected within ~30s rather than waiting minutes for OS TCP
        # keepalive.  Without this, an idle plugin↔relay WS can sit dead and
        # undetected until the next send attempt, at which point the watchdog
        # kicks off a 1→30s exponential backoff reconnect (~2-3 min total).
        self._ws = await self._session.ws_connect(url, heartbeat=30)

    async def disconnect(self) -> None:
        self._is_connected = False
        self._stop_event.set()
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        # Cancel any in-flight event dispatch tasks so a lingering
        # handle_message doesn't try to send on a closing WS.
        for task in list(self._event_tasks):
            task.cancel()
        if self._event_tasks:
            await asyncio.gather(*self._event_tasks, return_exceptions=True)
        self._event_tasks.clear()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        for fut in list(self._futures.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("adapter disconnected"))
        self._futures.clear()
        logger.info("OneBot: disconnected")

    # ── Reconnect watchdog ───────────────────────────────────────────────

    async def _watchdog(self) -> None:
        """Monitor ``_recv_task`` and reconnect after WS drops.

        Uses exponential backoff with jitter (1s → 2s → 4s → … → 30s cap).
        Runs alongside ``_receive_loop``; exits when ``_stop_event`` is set
        (i.e. ``disconnect()`` was called).
        """
        delay = _RECONNECT_INITIAL_DELAY
        while not self._stop_event.is_set():
            # Wait for the current receive loop to end (WS close / crash / cancel).
            recv = self._recv_task
            if recv and not recv.done():
                try:
                    await recv
                except (asyncio.CancelledError, Exception):
                    pass

            if self._stop_event.is_set():
                break

            self._is_connected = False
            logger.warning("OneBot: connection lost, reconnecting in %.1fs", delay)

            # Interruptible backoff sleep.
            jitter = random.uniform(0, delay * 0.3)
            wait = delay + jitter
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                break  # stop_event was set during sleep
            except TimeoutError:
                pass

            # Attempt reconnect.
            try:
                # Clean up the old WS before opening a new one.
                if self._ws and not self._ws.closed:
                    await self._ws.close()
                # Recreate the session if it was closed.
                if not self._session or self._session.closed:
                    self._session = aiohttp.ClientSession()
                # Fail any pending futures from the dead connection.
                for fut in list(self._futures.values()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("adapter reconnecting"))
                self._futures.clear()

                await self._ws_connect()
                self._recv_task = asyncio.create_task(self._receive_loop())
                self._is_connected = True
                logger.info("OneBot: reconnected to adapter service at %s", self._adapter_url)
                # Re-push commands snapshot after reconnect so the adapter has
                # the latest registry even across WS drops.
                asyncio.create_task(self._push_commands_snapshot())
                # Re-push Hermes mode so the adapter has the current
                # group_sessions_per_user value.
                asyncio.create_task(self._push_hermes_mode_report())
                # Re-push plugin version.
                asyncio.create_task(self._push_plugin_info())
                delay = _RECONNECT_INITIAL_DELAY  # reset backoff on success
            except Exception as exc:
                logger.warning("OneBot: reconnect failed: %s", exc)
                delay = min(_RECONNECT_MAX_DELAY, delay * 2)

        logger.info("OneBot: reconnect watchdog stopped")

    # ── Inbound receive loop ─────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        await self._handle_text(msg.data)
                    except Exception:
                        logger.exception("OneBot: error handling text frame, continuing")
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OneBot: receive loop crashed")
        finally:
            self._is_connected = False
            logger.warning("OneBot: receive loop ended")

    async def _handle_text(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("OneBot: non-JSON frame ignored")
            return
        mtype = data.get("type")
        logger.debug("OneBot plugin recv from adapter: type=%s raw=%s", mtype, raw[:2000])

        if mtype == "ready":
            self._onebot_connected = data.get("onebot_connected", False)
            self._self_id = data.get("self_id", "")
            logger.debug(
                "OneBot: adapter ready (onebot=%s self_id=%s)",
                self._onebot_connected, self._self_id or "?",
            )
            return

        if mtype == "event":
            await self._handle_event(data)
            return

        if mtype == "result":
            req_id = data.get("req_id", "")
            fut = self._futures.pop(req_id, None)
            if fut and not fut.done():
                fut.set_result(data)
            return

        if mtype == "pong":
            return

        if mtype == "commands_refresh":
            logger.info("OneBot: commands_refresh requested by adapter, re-pushing snapshot")
            asyncio.create_task(self._push_commands_snapshot())
            return

        if mtype == "mode_refresh":
            logger.info("OneBot: mode_refresh requested by adapter, re-pushing hermes_mode_report")
            asyncio.create_task(self._push_hermes_mode_report())
            return

        logger.debug("OneBot: unhandled frame type %s", mtype)

    async def _handle_event(self, data: dict[str, Any]) -> None:
        event = data.get("event")
        if event != "message":
            return

        logger.debug(
            "OneBot plugin recv event: chat_id=%s text_len=%d",
            data.get("chat_id", ""),
            len(data.get("text", "") or ""),
        )
        logger.debug("OneBot plugin event text preview: %r", (data.get("text", "") or "")[:500])

        text = data.get("text", "")

        # Set admin context for tool gating — from event (computed by adapter)
        self._current_is_admin = data.get("is_admin", False)
        # Set current group_id/user_id for tool param (real_seq→message_id 转换在适配器侧)
        chat_id = data.get("chat_id", "")
        self._current_group_id = ""
        self._current_user_id = str(data.get("user_id", ""))
        if chat_id.startswith("group:"):
            # group:42 或 group:42:user:100 → 取群号
            self._current_group_id = chat_id.split(":")[1]

        timestamp = datetime.fromtimestamp(data["timestamp"]) if data.get("timestamp") else datetime.now()

        source = self.build_source(
            chat_id=data.get("chat_id", ""),
            chat_name=data.get("chat_name", "") or data.get("user_name", ""),
            chat_type=data.get("chat_type", "dm"),
            user_id=data.get("user_id", ""),
            user_name=data.get("user_name", ""),
            message_id=data.get("message_id", ""),
        )

        message_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
            message_id=data.get("message_id", ""),
            media_urls=[],
            media_types=[],
            reply_to_message_id=data.get("reply_to_message_id"),
            reply_to_text=data.get("reply_to_text"),
            timestamp=timestamp,
            channel_prompt=data.get("channel_prompt"),
        )
        logger.debug(
            "OneBot plugin → Hermes: chat_id=%s",
            message_event.source.chat_id,
        )
        # 群聊排队:shared 群聊(group:<gid> 无 :user: 且 Hermes group_sessions_per_user=False)
        # 才注册 post_delivery callback,处理完后发 idle 帧给适配器,适配器 dequeue 下一条。
        # per_user 群聊 / 私聊 / Hermes per_user 隔离时不注册(无需排队)。
        self._maybe_register_idle_callback(data, message_event)
        # Dispatch handle_message as a background task so the receive loop is
        # NOT blocked.  Previously this was ``await self.handle_message(...)``
        # which deadlocked when handle_message hit the bypass-active-session
        # path (/approve, /deny, /status, …): that path inline-awaits
        # ``_send_with_retry`` → ``self.send()`` → ``self._request()``, which
        # sends a ``send`` frame and awaits the matching ``result`` frame.
        # The ``result`` frame arrives on this same WS and is processed by
        # ``_receive_loop`` — but ``_receive_loop`` was blocked here, so the
        # result frame could never be processed, the 30s timeout fired, the
        # gateway retried with an identical payload, and the adapter
        # (unaware of the deadlock) sent the message again — producing
        # duplicate messages in the chat.  Scheduling as a fire-and-forget
        # task keeps the receive loop free to process result frames, breaking
        # the self-deadlock.  This mirrors the normal-message path
        # (``_start_session_processing`` → ``create_task``) which was never
        # affected.
        task = asyncio.create_task(self._dispatch_event(message_event))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    async def _dispatch_event(self, message_event: Any) -> None:
        """Run ``handle_message`` off the receive loop.

        Exceptions are logged instead of propagating — ``_receive_loop``
        already swallows per-frame exceptions, and a background task that
        raises would otherwise surface as "Task exception was never
        retrieved".
        """
        try:
            await self.handle_message(message_event)
        except Exception:
            logger.exception("OneBot: handle_message raised in background task")

    def _maybe_register_idle_callback(
        self, data: dict[str, Any], message_event: Any
    ) -> None:
        """Register a post_delivery callback that fires an ``idle`` frame to
        the adapter service after a shared-group turn finishes.

        Conditions (all must hold):
        - chat_id is a group chat (``group:<gid>``).  DMs don't queue.
        - Hermes ``group_sessions_per_user`` is False — read from
          ``self.config.extra`` exactly like ``BasePlatformAdapter.handle_message``
          does at base.py:4606, so the plugin's notion of "shared" matches
          Hermes' actual session-key construction.  When True, Hermes gives
          each participant their own session and queueing is pointless.
        - The host exposes ``register_post_delivery_callback`` (older Hermes
          builds don't — we silently skip and the adapter's busy-timeout
          watchdog handles any stuck slot).
        """
        if not _BASE_AVAILABLE or build_session_key is None or SessionSource is None:
            return
        chat_id = data.get("chat_id", "")
        if not chat_id.startswith("group:"):
            return  # DM — no queueing.
        try:
            group_sessions_per_user = self.config.extra.get("group_sessions_per_user", True)
        except Exception:
            group_sessions_per_user = True
        if group_sessions_per_user:
            return  # Hermes isolates per-user; queueing pointless.
        if not hasattr(self, "register_post_delivery_callback"):
            return  # host too old — skip; adapter watchdog covers stuck slots.
        # Compute the same session_key base.py:handle_message will compute so
        # the callback registered here is the one base.py pops after the turn.
        try:
            session_key = build_session_key(
                message_event.source,
                group_sessions_per_user=group_sessions_per_user,
                thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            )
        except Exception:
            logger.debug("OneBot: build_session_key failed, skipping idle callback")
            return
        if not session_key:
            return
        # generation ties the callback to the current gateway run so stale
        # runs cannot fire (and clear) a fresher run's idle slot.
        generation = None
        active = getattr(self, "_active_sessions", {}).get(session_key)
        if active is not None:
            generation = getattr(active, "_hermes_run_generation", None)
        gid = chat_id[len("group:"):]
        if self._ws is None or self._ws.closed:
            return

        async def _fire_idle() -> None:
            # Re-fetch self._ws at fire time instead of closing over the value
            # captured at registration.  If the plugin reconnected between
            # register_post_delivery_callback and the post-delivery callback
            # firing, the stale ws would be closed and the idle frame would
            # be lost silently — leaving the adapter's busy slot stuck until
            # the watchdog reaps it (default 300s).
            ws = self._ws
            if ws is None or ws.closed:
                logger.warning("OneBot: idle frame dropped — ws closed at fire time (gid=%s)", gid)
                return
            try:
                await ws.send_json({"type": "idle", "v": 1, "chat_id": chat_id, "group_id": gid})
                logger.info("OneBot: fired idle frame gid=%s chat_id=%s", gid, chat_id)
            except Exception:
                logger.warning("OneBot: idle frame send failed (ws closed?) gid=%s", gid, exc_info=True)

        try:
            self.register_post_delivery_callback(session_key, _fire_idle, generation=generation)
        except Exception:
            logger.warning("OneBot: register_post_delivery_callback failed gid=%s", gid, exc_info=True)

    # ── Slash-command registry push ─────────────────────────────────────

    def _collect_commands(self) -> list[dict[str, Any]]:
        """Collect all slash commands registered in Hermes.

        Combines the builtin ``COMMAND_REGISTRY`` (central registry in
        ``hermes_cli.commands``) with plugin-registered commands from
        ``hermes_cli.plugins.get_plugin_commands()``.  Returns a list of
        plain dicts matching :class:`CommandInfo` shape.  Returns an empty
        list if the Hermes host APIs are unavailable (standalone mode).
        """
        try:
            from hermes_cli.commands import COMMAND_REGISTRY
        except ImportError:
            logger.debug("OneBot: hermes_cli.commands not available, skipping builtin commands")
            COMMAND_REGISTRY = ()  # type: ignore[assignment]

        try:
            from hermes_cli.plugins import get_plugin_commands
        except ImportError:
            get_plugin_commands = None  # type: ignore[assignment]

        cmds: list[dict[str, Any]] = []

        for cmd in COMMAND_REGISTRY:
            # Skip CLI-only commands unless a config gate overrides them.
            if cmd.cli_only and not cmd.gateway_config_gate:
                continue
            cmds.append({
                "name": cmd.name,
                "description": cmd.description or "",
                "source": "builtin",
                "aliases": list(cmd.aliases or ()),
                "args_hint": cmd.args_hint or "",
            })

        if get_plugin_commands is not None:
            try:
                plugin_cmds = get_plugin_commands()
            except Exception:
                logger.exception("OneBot: get_plugin_commands() failed")
                plugin_cmds = {}
            for name, meta in plugin_cmds.items():
                cmds.append({
                    "name": name,
                    "description": (meta or {}).get("description", "") or "",
                    "source": (meta or {}).get("plugin", "plugin") or "plugin",
                    "aliases": [],
                    "args_hint": (meta or {}).get("args_hint", "") or "",
                })

        logger.debug("OneBot: collected %d slash commands from Hermes", len(cmds))
        return cmds

    async def _push_commands_snapshot(self) -> None:
        """Collect the current Hermes slash-command registry and push it to
        the adapter service as a ``commands_snapshot`` frame."""
        if not self._ws or self._ws.closed:
            return
        try:
            cmds = self._collect_commands()
            await self._ws.send_json({
                "type": "commands_snapshot",
                "v": 1,
                "commands": cmds,
            })
            logger.debug("OneBot: pushed commands_snapshot (%d commands) to adapter", len(cmds))
        except Exception:
            logger.exception("OneBot: failed to push commands_snapshot")

    async def _push_plugin_info(self) -> None:
        """Push the installed plugin version to the adapter service.
        Sent once on initial connect so the adapter can detect mismatches."""
        if not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({
                "type": "plugin_info",
                "v": 1,
                "plugin_version": self._plugin_version,
            })
            logger.debug("OneBot: pushed plugin_info (version=%s) to adapter", self._plugin_version)
        except Exception:
            logger.exception("OneBot: failed to push plugin_info")

    async def _push_hermes_mode_report(self) -> None:
        """Read Hermes' ``group_sessions_per_user`` config and push it to the
        adapter service as a ``hermes_mode_report`` frame.

        Read from ``self.config.extra`` exactly like
        ``BasePlatformAdapter.handle_message`` (base.py:4606) — Hermes injects
        the top-level value into platform ``extra`` via setdefault in
        ``_create_adapter`` (run.py:8355-8363).  Default True when missing.
        """
        if not self._ws or self._ws.closed:
            return
        try:
            group_sessions_per_user = self.config.extra.get("group_sessions_per_user", True)
        except Exception:
            group_sessions_per_user = True
        try:
            await self._ws.send_json({
                "type": "hermes_mode_report",
                "v": 1,
                "group_sessions_per_user": bool(group_sessions_per_user),
            })
            logger.debug(
                "OneBot: pushed hermes_mode_report (group_sessions_per_user=%s) to adapter",
                group_sessions_per_user,
            )
        except Exception:
            logger.exception("OneBot: failed to push hermes_mode_report")

    # ── Outbound send helpers ────────────────────────────────────────────

    async def _request(self, action: str, **payload: Any) -> dict[str, Any]:
        """Send a ``send`` frame and await the matching ``result``.

        Concurrency is bounded by ``_MAX_INFLIGHT_SENDS`` so that Gateway
        ``_send_with_retry`` retry storms cannot pile up unlimited parallel
        sends on the serial OneBot WS (which would amplify congestion and
        cause more timeouts → more retries, a death spiral).
        """
        async with self._send_semaphore:
            if not self._ws or self._ws.closed:
                return {"success": False, "error": "adapter WS not connected"}
            req_id = str(uuid.uuid4())
            msg: dict[str, Any] = {
                "type": "send",
                "action": action,
                "req_id": req_id,
            }
            msg.update(payload)
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
            self._futures[req_id] = fut
            logger.debug(
                "OneBot plugin _request: action=%s req_id=%s payload_keys=%s frame=%s",
                action, req_id, list(payload.keys()),
                json.dumps(msg, ensure_ascii=False)[:2000],
            )
            await self._ws.send_json(msg)
            try:
                result = await asyncio.wait_for(fut, timeout=_RESULT_TIMEOUT)
                logger.debug(
                    "OneBot plugin _request result: action=%s req_id=%s success=%s message_id=%s",
                    action, req_id, result.get("success"), result.get("message_id"),
                )
                return result
            except TimeoutError:
                self._futures.pop(req_id, None)
                return {"success": False, "error": f"timeout waiting for {action} result"}

    # ── Required abstract methods ────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        try:
            text = strip_markdown(content)
            chunks = self.truncate_message(text, max_length=self.MAX_MESSAGE_LENGTH)
            logger.debug("OneBot plugin send: chat_id=%s chunks=%d text_len=%d", chat_id, len(chunks), len(text or ""))
            last_id: str | None = None
            for i, chunk in enumerate(chunks):
                payload: dict[str, Any] = {"chat_id": chat_id, "content": chunk}
                if i == 0 and reply_to:
                    payload["reply_to"] = reply_to
                result = await self._request("send_text", **payload)
                if not result.get("success"):
                    return SendResult(
                        success=False,
                        error=result.get("error", "unknown send error"),
                        retryable=True,
                    )
                last_id = result.get("message_id", last_id)
            return SendResult(success=True, message_id=last_id)
        except Exception as exc:
            logger.error("OneBot send error: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        result = await self._api_call(
            "get_group_info" if chat_id.startswith("group:") else "get_stranger_info",
            {"group_id": int(chat_id[6:])} if chat_id.startswith("group:") else {"user_id": int(chat_id)},
        )
        if not result.get("success"):
            return {"name": chat_id, "type": "unknown", "chat_id": chat_id}
        info = result.get("data", {})
        if chat_id.startswith("group:"):
            return {"name": info.get("group_name", chat_id), "type": "group", "chat_id": chat_id}
        return {"name": info.get("nickname", chat_id), "type": "dm", "chat_id": chat_id}

    # ── Optional send overrides ─────────────────────────────────────────

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        try:
            logger.debug("OneBot plugin send_image: chat_id=%s url=%s", chat_id, image_url)
            payload: dict[str, Any] = {"chat_id": chat_id, "image_url": image_url}
            if caption:
                payload["caption"] = strip_markdown(caption)
            if reply_to:
                payload["reply_to"] = reply_to
            result = await self._request("send_image", **payload)
            return _result_to_send_result(result)
        except Exception as exc:
            logger.warning("send_image failed: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs,
    ) -> SendResult:
        try:
            logger.debug("OneBot plugin send_voice: chat_id=%s path=%s", chat_id, audio_path)
            payload: dict[str, Any] = {"chat_id": chat_id, "audio_path": audio_path}
            if reply_to:
                payload["reply_to"] = reply_to
            result = await self._request("send_voice", **payload)
            return _result_to_send_result(result)
        except Exception as exc:
            logger.warning("send_voice failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs,
    ) -> SendResult:
        try:
            logger.debug("OneBot plugin send_video: chat_id=%s path=%s", chat_id, video_path)
            payload: dict[str, Any] = {"chat_id": chat_id, "video_path": video_path}
            if caption:
                payload["caption"] = strip_markdown(caption)
            if reply_to:
                payload["reply_to"] = reply_to
            result = await self._request("send_video", **payload)
            return _result_to_send_result(result)
        except Exception as exc:
            logger.warning("send_video failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs,
    ) -> SendResult:
        try:
            logger.debug("OneBot plugin send_document: chat_id=%s path=%s", chat_id, file_path)
            payload: dict[str, Any] = {"chat_id": chat_id, "file_path": file_path}
            if file_name:
                payload["filename"] = file_name
            result = await self._request("send_document", **payload)
            return _result_to_send_result(result)
        except Exception as exc:
            logger.warning("send_document failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def _api_call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._ws or self._ws.closed:
            return {"success": False, "error": "adapter WS not connected"}
        logger.debug("OneBot plugin api_call: action=%s", action)
        logger.debug("OneBot plugin api_call params: %s", json.dumps(params, ensure_ascii=False)[:2000])
        req_id = str(uuid.uuid4())
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._futures[req_id] = fut
        await self._ws.send_json({
            "type": "api_call",
            "action": action,
            "req_id": req_id,
            "params": params,
        })
        try:
            result = await asyncio.wait_for(fut, timeout=_RESULT_TIMEOUT)
            logger.debug("OneBot plugin api_call result: action=%s success=%s data=%s",
                         action, result.get("success"), json.dumps(result.get("data"), ensure_ascii=False)[:2000])
            return result
        except TimeoutError:
            self._futures.pop(req_id, None)
            return {"success": False, "error": f"timeout waiting for {action}"}

    # ── Typing (no-op, OneBot has no typing indicator) ───────────────────

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        pass

    async def stop_typing(self, chat_id: str) -> None:
        pass

    # ── Formatting ───────────────────────────────────────────────────────

    def format_message(self, content: str) -> str:
        return strip_markdown(content)


# ── Helpers ──────────────────────────────────────────────────────────────


def _result_to_send_result(result: dict[str, Any]) -> SendResult:
    return SendResult(
        success=result.get("success", False),
        message_id=result.get("message_id"),
        error=result.get("error"),
        retryable=not result.get("success", False),
    )


# ── Plugin registration helpers ─────────────────────────────────────────


def check_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    url = os.getenv("ONEBOT_ADAPTER_URL") or extra.get("adapter_url", "")
    token = os.getenv("ONEBOT_ADAPTER_TOKEN") or extra.get("adapter_token", "")
    return bool(url and token)


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> dict | None:
    url = os.getenv("ONEBOT_ADAPTER_URL", "").strip()
    if not url:
        return None
    seed: dict = {"adapter_url": url}
    token = os.getenv("ONEBOT_ADAPTER_TOKEN", "").strip()
    if token:
        seed["adapter_token"] = token
    home = os.getenv("ONEBOT_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": home}
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Out-of-process send for cron delivery via the adapter's relay WS.

    Opens a short-lived WebSocket to the adapter service (the same /hermes
    endpoint the live plugin uses), sends a ``send_text`` frame, awaits the
    matching ``result`` frame, then closes.  This keeps cron delivery on the
    same transport as normal sends so the WebUI HTTP app is not required.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    ws_url = (
        os.getenv("ONEBOT_ADAPTER_URL")
        or extra.get("adapter_url", "")
    ).rstrip("/")
    token = os.getenv("ONEBOT_ADAPTER_TOKEN") or extra.get("adapter_token", "")
    if not ws_url:
        return {"error": "standalone send failed: ONEBOT_ADAPTER_URL not set"}
    req_id = str(uuid.uuid4())
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                ws_url, headers={"Authorization": f"Bearer {token}"} if token else {},
                timeout=aiohttp.ClientWSTimeout(ws_close=30),
            ) as ws:
                # Ignore inbound frames (ready, ring-buffer replay, etc.)
                # until we see the result for our req_id.
                await ws.send_json({
                    "type": "send",
                    "v": 1,
                    "action": "send_text",
                    "req_id": req_id,
                    "chat_id": chat_id,
                    "content": message,
                })
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "result" and data.get("req_id") == req_id:
                        if data.get("success"):
                            return {"success": True, "message_id": str(data.get("message_id", ""))}
                        return {"error": data.get("error", "send failed")}
                return {"error": "standalone send failed: connection closed before result"}
    except Exception as exc:
        return {"error": f"standalone send failed: {exc}"}


def register(ctx) -> None:
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="onebot",
        label="OneBot (OneBot)",
        adapter_factory=lambda cfg: OneBotAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ONEBOT_ADAPTER_URL", "ONEBOT_ADAPTER_TOKEN"],
        install_hint="pip install aiohttp; 启动 hermes-onebot-adapter 服务并在 WebUI 安装插件",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ONEBOT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=_QQ_TEXT_LIMIT,
        emoji="🐧",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=os.getenv(
            "ONEBOT_PLATFORM_HINT",
            "你正通过 OneBot(QQ) 对话。QQ 不渲染 Markdown，仅纯文本。"
            "群聊需 @bot 触发。消息上限约 4500 字符，超长会自动分段。"
            "私聊 chat_id 为 QQ 号，群聊为 group:<群号>。"
            "要 @ 某人，使用 {@QQ号} 格式，如 {@123456} 你好。"
            "收到的消息中 @ 显示为 @QQ号(昵称) 格式。"
            "群聊消息前缀格式为 [昵称(QQ号)#群内序号]: 内容，管理员标识为 [昵称(QQ号)(管理员)#群内序号]: 内容。"
            "#后数字是群内递增序号(real_seq),调用 onebot 工具时传此数字。"
            "引用回复和合并转发中的发送者也包含 QQ 号和 #群内序号。括号中的 QQ 号可直接用于 {@QQ号} 回复。",
        ),
    )

    # Register OneBot API tools (send, group management, history, etc.)
    from .onebot_tools import register_tools

    # Defer adapter injection until the adapter is constructed; register_tools
    # just declares the tool schemas/handlers.  The adapter instance calls
    # set_adapter(self) in its __init__ so tools can reach _api_call.
    register_tools(ctx)

"""Service composition: builds the aiohttp Applications and lifecycle hooks.

Three Applications share service state but bind to separate ports:
  * onebot_app  -> onebot_reverse_ws_port (OneBot reverse WS endpoint)
  * hermes_app  -> hermes_ws_port         (Hermes plugin WS endpoint)
  * webui_app   -> webui_port             (WebUI + REST API + static SPA)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import deque
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import aiohttp
import aiohttp.web

from onebot_adapter import __version__
from onebot_adapter.config import AdapterConfig, ConfigStore, ensure_tokens, load_config, save_config
from onebot_adapter.onebot.api import OneBotApi
from onebot_adapter.onebot.name_resolver import NameResolver
from onebot_adapter.onebot.seq_map import SeqMap
from onebot_adapter.onebot.ws_api import WsApiTransport
from onebot_adapter.onebot.ws_forward import OneBotForwardClient
from onebot_adapter.onebot.ws_reverse import OneBotReverseServer
from onebot_adapter.relay.hermes_ws import HermesRelayServer
from onebot_adapter.relay.protocol import parse_chat_id
from onebot_adapter.webui import routes as webui_routes

logger = logging.getLogger(__name__)


class _ExcludeLogFormatPreview(logging.Filter):
    """Filter that rejects log records emitted by the ``log_format`` module
    logger (truncated recv/send preview lines).  Those events are also
    emitted in full (untruncated) form by the dedicated ``onebot_adapter.file``
    logger, which propagates to the parent ``onebot_adapter`` logger and
    reaches the file handler.  Without this filter the file would contain
    both the truncated preview and the full line for every recv/send event.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.endswith(".onebot.log_format")




class AdapterService:
    def __init__(self, store: ConfigStore | None = None) -> None:
        self.store = store or ConfigStore(load_config())
        self._state: dict[str, Any] = {
            "onebot_connected": False,
            "hermes_plugin_connected": False,
            "log_buffer": deque(maxlen=500),
        }
        self._api: OneBotApi | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws_api_transport: WsApiTransport | None = None
        self._onebot_reverse: OneBotReverseServer | None = None
        self._onebot_forward: OneBotForwardClient | None = None
        self._relay: HermesRelayServer | None = None
        self._seq_map: SeqMap | None = None
        self._runners: list[aiohttp.web.AppRunner] = []
        self._forward_task: asyncio.Task[None] | None = None
        self._self_id_probed = False
        self._file_handler: logging.Handler | None = None
        self._webui_log_handler: logging.Handler | None = None
        self._probe_lock = asyncio.Lock()
        self._cleaning_up = False

    def _init_components(self) -> None:
        cfg = self.store.config
        assert self._session is not None
        self._ws_api_transport = WsApiTransport()
        self._api = OneBotApi(ws_transport=self._ws_api_transport)
        self._state["api"] = self._api
        self._name_resolver = NameResolver(self._api)
        self._state["name_resolver"] = self._name_resolver
        self._seq_map = SeqMap(maxlen=cfg.seq_map_size)
        self._state["seq_map"] = self._seq_map
        self._relay = HermesRelayServer(
            cfg,
            self._api,
            adapter_version=__version__,
            onebot_connected_fn=self._onebot_connected,
            on_connect=self._update_status,
            on_disconnect=self._update_status,
            on_filtered=self._on_filtered_command,
            on_dispatch=self._maybe_react_delivered,
            seq_map=self._seq_map,
            name_resolver=self._name_resolver,
        )
        self._state["relay"] = self._relay
        self._onebot_reverse = OneBotReverseServer(
            cfg,
            self._api,
            on_event=self._on_onebot_event,
            on_connect=self._on_onebot_connect,
            on_disconnect=self._update_status,
            on_filtered=self._on_filtered_command,
            is_known_command_fn=self._relay.is_known_command,
            canonical_command_name_fn=self._relay.canonical_command_name,
            seq_map=self._seq_map,
            name_resolver=self._name_resolver,
            ws_api_transport=self._ws_api_transport,
        )
        self._onebot_forward = OneBotForwardClient(
            cfg,
            self._api,
            on_event=self._on_onebot_event,
            on_connect=self._on_onebot_connect,
            on_disconnect=self._update_status,
            session=self._session,
            on_filtered=self._on_filtered_command,
            is_known_command_fn=self._relay.is_known_command,
            canonical_command_name_fn=self._relay.canonical_command_name,
            seq_map=self._seq_map,
            name_resolver=self._name_resolver,
            ws_api_transport=self._ws_api_transport,
        )
        # Register config-change listener early so hot-reload via the WebUI
        # (which starts first) notifies components immediately — previously
        # this was in _on_hermes_startup, which left a window where a config
        # change before the Hermes WS site started would be silently ignored.
        self.store.on_change(self._on_config_change)

    def _setup_file_logging(self, cfg: AdapterConfig) -> None:
        """Create or replace the file logging handler for persistent logs.

        The handler is attached to the ``onebot_adapter`` parent logger so
        that ALL modules under the package (relay, onebot.*, webui, etc.)
        propagate their log records into ``adapter.log``.  A filter excludes
        the truncated preview lines emitted by ``onebot_adapter.onebot.log_format``'s
        module logger — those events are already written to the file in full
        (untruncated) form by the dedicated ``onebot_adapter.file`` logger,
        so accepting the truncated copies here would duplicate recv/send lines.
        """
        if self._file_handler is not None:
            logging.getLogger("onebot_adapter").removeHandler(self._file_handler)
            logging.getLogger("onebot_adapter.file").removeHandler(self._file_handler)
            self._file_handler.close()
            self._file_handler = None

        if not cfg.log_file_enabled:
            return

        log_dir = cfg.log_file_dir or os.path.expanduser("~/.onebot_adapter/logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "adapter.log")

        handler = TimedRotatingFileHandler(
            log_path, when="midnight", interval=1,
            backupCount=cfg.log_retention_days,
            encoding="utf-8",
        )
        handler.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        # Skip the truncated preview lines from the log_format module logger;
        # the full untruncated versions are emitted by the "onebot_adapter.file"
        # logger and will reach this handler via propagation.
        handler.addFilter(_ExcludeLogFormatPreview())
        logging.getLogger("onebot_adapter").addHandler(handler)
        self._file_handler = handler

    def _update_file_logging(self, old: AdapterConfig, new: AdapterConfig) -> None:
        """Hot-reload file logging settings."""
        changed = (
            old.log_file_enabled != new.log_file_enabled
            or old.log_file_dir != new.log_file_dir
            or old.log_retention_days != new.log_retention_days
        )
        if changed:
            self._setup_file_logging(new)

    def _apply_log_level(self, level_str: str) -> None:
        """Apply a log level string (e.g. "DEBUG") to root logger and all
        owned handlers (WebUI + file).  Called on startup and hot-reload."""
        import logging as _logging
        level = getattr(_logging, level_str.upper(), _logging.INFO)
        _logging.getLogger().setLevel(level)
        if self._webui_log_handler is not None:
            self._webui_log_handler.setLevel(level)
        if self._file_handler is not None:
            self._file_handler.setLevel(level)
        logger.info("log level set to %s", level_str.upper())

    def build_onebot_app(self) -> aiohttp.web.Application:
        assert self._onebot_reverse is not None
        app = aiohttp.web.Application()
        self._onebot_reverse.add_routes(app)
        return app

    def build_hermes_app(self) -> aiohttp.web.Application:
        assert self._relay is not None
        app = aiohttp.web.Application()
        self._relay.add_routes(app)
        app.on_startup.append(self._on_hermes_startup)
        app.on_cleanup.append(self._on_hermes_cleanup)
        return app

    def build_webui_app(self) -> aiohttp.web.Application:
        app = aiohttp.web.Application()
        webui_routes.add_routes(app, self.store, self._state)
        return app

    def _onebot_connected(self) -> bool:
        if self._onebot_reverse and self._onebot_reverse.connected:
            return True
        if self._onebot_forward and self._onebot_forward.connected:
            return True
        return False

    def _on_onebot_connect(self) -> None:
        """OneBot WS 连接建立时触发：更新状态并尝试探测 self_id。

        self_id 探测现在依赖 WS 连接（之前走 HTTP API），
        所以放在每次连接建立时触发，而非启动时盲试。
        """
        self._update_status()
        if not self._self_id_probed and self.store.config.self_id == "":
            asyncio.create_task(self._probe_self_id_guarded())

    async def _probe_self_id_guarded(self) -> None:
        """Concurrent-safe wrapper: only one probe at a time."""
        if self._probe_lock.locked():
            return  # another probe is in flight
        async with self._probe_lock:
            if self._self_id_probed or self.store.config.self_id != "":
                return
            await self._probe_self_id()

    def _update_status(self) -> None:
        """Synchronise WebUI state with live connection status."""
        self._state["onebot_connected"] = self._onebot_connected()
        self._state["hermes_plugin_connected"] = bool(self._relay and self._relay.has_clients)

    async def _on_onebot_event(self, event) -> None:
        self._update_status()
        logger.debug(
            "app _on_onebot_event: relaying to Hermes chat_id=%s text_preview=%r",
            event.chat_id, (event.text or "")[:500],
        )
        if self._relay:
            outcome = await self._relay.push_event(event)
            if outcome == "queued":
                await self._maybe_react_queued(event)
            elif outcome == "broadcast":
                await self._maybe_react_delivered(event)
            # "dropped": neither queued nor delivered — do not react.

    async def _maybe_react_delivered(self, event) -> None:
        """消息送达 Hermes(广播或出队)后在原消息上贴表情回应(可配置)。

        触发条件:功能全局开启且当前会话未单独关闭;Hermes 插件有连接(否则
        消息只进了 ring buffer 等重连重放,不算"送达");event.message_id 可转 int。
        调用 OneBot ``set_msg_emoji_like`` API,失败仅记 debug 日志,不影响主流程。
        """
        await self._do_react(event, self.store.config.reaction_emoji_id)

    async def _maybe_react_queued(self, event) -> None:
        """消息进入排队队列时在原消息上贴表情回应(可配置)。

        与 _maybe_react_delivered 结构相同,但使用 reaction_emoji_id_queued
        配置项。当该配置为空字符串时,排队时不贴表情。
        """
        emoji_id = self.store.config.reaction_emoji_id_queued
        if not emoji_id:
            return
        await self._do_react(event, emoji_id)

    async def _do_react(self, event, emoji_id: str) -> None:
        """统一的贴表情回应实现。"""
        cfg = self.store.config
        try:
            is_group, num_id = parse_chat_id(event.chat_id)
        except (ValueError, TypeError):
            return
        group_id = str(num_id) if is_group else None
        if not cfg.resolve_reaction_emoji_enabled(group_id):
            return
        if not self._relay or not self._relay.has_clients:
            return
        try:
            msg_id = int(event.message_id)
        except (ValueError, TypeError):
            return
        assert self._api is not None
        params: dict[str, Any] = {"message_id": msg_id, "emoji_id": emoji_id}
        if is_group:
            params["group_id"] = num_id
        else:
            params["user_id"] = num_id
        try:
            await self._api.call("set_msg_emoji_like", params)
            logger.debug(
                "reaction emoji set: msg=%s emoji=%s chat=%s",
                event.message_id, emoji_id, event.chat_id,
            )
        except Exception:
            logger.debug(
                "set_msg_emoji_like failed (msg=%s chat=%s)", event.message_id, event.chat_id
            )

    async def _on_filtered_command(self, filtered) -> None:
        """Handle a /command that was denied by the command filter.

        Sends the reject message back to the originating chat via the OneBot
        HTTP API (through the relay) and does NOT forward the event to the
        Hermes plugin.
        """
        logger.debug(
            "app _on_filtered_command: chat_id=%s cmd=%s user=%s",
            getattr(filtered, "chat_id", ""),
            getattr(filtered, "command_name", ""),
            getattr(filtered, "user_id", ""),
        )
        if self._relay:
            await self._relay.send_reject_message(
                chat_id=getattr(filtered, "chat_id", ""),
                message=getattr(filtered, "reject_message", "") or "⛔ 指令被过滤",
                reply_to=getattr(filtered, "reply_to_message_id", None),
            )

    async def _on_hermes_startup(self, app: aiohttp.web.Application) -> None:
        cfg = self.store.config
        logger.info(
            "OneBot adapter %s | onebot_mode=%s onebot_port=%d hermes_ws_port=%d webui_port=%d",
            __version__, cfg.onebot_mode, cfg.onebot_reverse_ws_port, cfg.hermes_ws_port, cfg.webui_port,
        )
        if cfg.onebot_mode == "forward":
            assert self._onebot_forward is not None
            self._forward_task = self._onebot_forward.start()

    async def _on_hermes_cleanup(self, app: aiohttp.web.Application) -> None:
        if self._cleaning_up:
            return
        self._cleaning_up = True
        if self._onebot_forward:
            await self._onebot_forward.stop()
        if self._onebot_reverse:
            await self._onebot_reverse.stop()
        if self._relay:
            await self._relay.stop()
        if self._session and not self._session.closed:
            await self._session.close()
        for runner in self._runners:
            await runner.cleanup()
        logger.info("OneBot adapter stopped")

    async def _on_config_change(self, old: AdapterConfig, new: AdapterConfig) -> None:
        """Handle config changes from the WebUI (hot-reload).

        Registered as an async callback — the ConfigStore schedules it via
        ``asyncio.create_task`` when it fires.
        """
        # Always push the fresh config object to every component so that
        # holders reading config fields at request/connect time see the new
        # values (hermes_ws_token, onebot_ws_token for WS handshake,
        # group/DM settings used by the event parser, etc.).
        if self._relay:
            self._relay.update_config(new)
        if self._onebot_reverse:
            self._onebot_reverse.update_config(new)
        if self._onebot_forward:
            self._onebot_forward.update_config(new)
        if self._seq_map and old.seq_map_size != new.seq_map_size:
            self._seq_map.update_maxlen(new.seq_map_size)
            logger.info("SeqMap size changed: %d -> %d", old.seq_map_size, new.seq_map_size)

        # File logging hot-reload
        self._update_file_logging(old, new)

        # log_level hot-reload: update root logger + WebUI handler + file handler
        if old.log_level != new.log_level:
            self._apply_log_level(new.log_level)

        # OneBot mode switch: stop/start forward client.
        if old.onebot_mode != new.onebot_mode:
            logger.info("OneBot mode changed: %s -> %s", old.onebot_mode, new.onebot_mode)
            if old.onebot_mode == "forward" and self._onebot_forward:
                await self._onebot_forward.stop()
                logger.info("Forward WS client stopped due to mode switch")
            if new.onebot_mode == "forward" and self._onebot_forward:
                self._forward_task = self._onebot_forward.start()
                logger.info("Forward WS client started due to mode switch")
        elif new.onebot_mode == "forward" and self._onebot_forward:
            # Mode unchanged but connection-affecting fields changed: force a
            # reconnect so the new token / forward WS URL takes effect on the
            # next handshake (the WS handshake token is sent only at connect).
            conn_changed = (
                old.onebot_ws_token != new.onebot_ws_token
                or old.onebot_forward_ws_url != new.onebot_forward_ws_url
            )
            if conn_changed:
                logger.info(
                    "Forward WS connection params changed; reconnecting "
                    "(token=%s, url=%s)",
                    "set" if new.onebot_ws_token else "empty",
                    new.onebot_forward_ws_url,
                )
                await self._onebot_forward.stop()
                self._forward_task = self._onebot_forward.start()

        self._update_status()

    async def _probe_self_id(self) -> None:
        assert self._api is not None
        for _ in range(10):
            try:
                info = await self._api.get_login_info()
                self.store.patch(self_id=str(info.get("user_id", "")))
                save_config(self.store.config)
                self._self_id_probed = True
                logger.info("OneBot self_id probed: %s", self.store.config.self_id)
                # Notify already-connected plugins of the new self_id
                if self._relay:
                    await self._relay.broadcast_self_id(self.store.config.self_id)
                return
            except Exception as exc:
                logger.debug("self_id probe attempt failed: %s", exc)
                await asyncio.sleep(3)
        logger.warning("OneBot self_id probe failed; set it in WebUI")

    @staticmethod
    async def _try_port(
        runner: aiohttp.web.AppRunner, host: str, port: int, label: str, max_retries: int = 50,
    ) -> aiohttp.web.TCPSite:
        """Bind *runner* to *port*; if busy try the next port up to *max_retries* times."""
        import errno

        for attempt in range(max_retries):
            try:
                site = aiohttp.web.TCPSite(runner, host, port + attempt)
                await site.start()
                logger.info("%s listening on %s:%d", label, host, site.port)
                return site
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                if attempt == max_retries - 1:
                    raise
                logger.debug("%s port %d busy, trying %d", label, port + attempt, port + attempt + 1)
        raise RuntimeError("unreachable")

    async def serve(self, host: str = "127.0.0.1", no_webui: bool = False) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": f"hermes-onebot-adapter/{__version__}"},
        )
        self._init_components()
        cfg = self.store.config

        if not no_webui:
            # Attach WebUI log handler so /api/logs has content
            from onebot_adapter.webui.log_handler import attach_log_handler
            self._webui_log_handler = attach_log_handler(self._state, level=cfg.log_level)
        # Ensure root logger level matches config so handler actually receives records
        logging.getLogger().setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
        # Suppress noisy third-party loggers from the WebUI log buffer
        for noisy in ("aiohttp.access", "aiohttp.web", "aiohttp.server", "aiohttp.websocket", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        # Attach file handler for persistent logging
        self._setup_file_logging(cfg)

        loop = asyncio.get_running_loop()

        onebot_app = self.build_onebot_app()
        hermes_app = self.build_hermes_app()

        onebot_runner = aiohttp.web.AppRunner(onebot_app)
        hermes_runner = aiohttp.web.AppRunner(hermes_app)
        self._runners = [onebot_runner, hermes_runner]

        if not no_webui:
            webui_app = self.build_webui_app()
            webui_runner = aiohttp.web.AppRunner(webui_app)
            self._runners.append(webui_runner)

        for runner in self._runners:
            await runner.setup()

        bindings: list[aiohttp.web.TCPSite] = []
        runner_label_port = [
            (onebot_runner, "OneBot WS", host, cfg.onebot_reverse_ws_port, "onebot_reverse_ws_port"),
            (hermes_runner, "Hermes WS", host, cfg.hermes_ws_port, "hermes_ws_port"),
        ]
        if not no_webui:
            runner_label_port.append((webui_runner, "WebUI", host, cfg.webui_port, "webui_port"))
        for runner, label, hst, port, cfg_key in runner_label_port:
            site = await self._try_port(runner, hst, port, label, 50)
            bindings.append(site)
            if site.port != port:
                logger.warning("%s port %d busy, using %d instead", label, port, site.port)
                self.store.patch(**{cfg_key: site.port})
        cfg = self.store.config
        if not no_webui:
            logger.info("WebUI ready at http://%s:%d", host, cfg.webui_port)

        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        try:
            await stop.wait()
        finally:
            await self._on_hermes_cleanup(hermes_app)


def run(host: str = "127.0.0.1", port: int | None = None, no_webui: bool = False) -> None:
    old_cfg = load_config()
    webui_token_was_empty = not old_cfg.webui_token
    cfg = ensure_tokens(old_cfg)
    store = ConfigStore(cfg)
    if port and not no_webui:
        store.patch(webui_port=port)
        cfg = store.config
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if webui_token_was_empty and not no_webui:
        logger.info(
            "WebUI 鉴权 token 已自动生成: %s\n"
            "    首次登录 WebUI 时需要输入此 token,"
            "也可在 ~/.onebot_adapter/config.json 的 webui_token 字段查看",
            cfg.webui_token,
        )
    service = AdapterService(store)
    try:
        asyncio.run(service.serve(host=host, no_webui=no_webui))
    except KeyboardInterrupt:
        pass

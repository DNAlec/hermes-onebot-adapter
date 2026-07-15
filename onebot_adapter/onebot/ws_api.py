"""OneBot 11 API 调用的 WebSocket 传输层。

OneBot 11 的 WebSocket 连接是双向的：同一条 WS 既推送事件帧（带 ``post_type``），
也接受 API 请求帧（``{"action", "params", "echo"}``）并回响应帧
（``{"retcode", "data", "echo"}``）。本模块用 ``echo`` 字段做请求-响应关联，
让上层 ``OneBotApi`` 用纯异步 ``call(action, params)`` 接口复用同一条 WS 发送消息。

ws_reverse / ws_forward 在每条 WS 连接建立/断开时调用 ``register(ws)`` /
``unregister(ws)``；在收到 text 帧时先调用 ``on_text(raw)``——若是响应帧（命中 pending echo）
则 resolve 对应 future 并返回 True，否则返回 False 交给事件解析路径。

多条 WS 同时活跃时（reverse 模式下多个 OneBot 实例拨入），``_pick_ws`` 取第一个活跃连接发请求。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0


class WsApiTransport:
    """关联 OneBot WebSocket 上的 API 请求-响应。

    线程安全：所有方法应在同一个事件循环里调用（与 aiohttp 一致）。
    """

    def __init__(self) -> None:
        self._active: set[Any] = set()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Track which ws each echo was sent on so we can reject only that
        # ws's pending requests when it disconnects (multi-instance mode).
        self._echo_ws: dict[str, Any] = {}

    @property
    def has_active(self) -> bool:
        return bool(self._active)

    def register(self, ws: Any) -> None:
        """登记一条活跃 OneBot WS 连接。"""
        self._active.add(ws)
        logger.debug("WsApiTransport: registered ws (%d active)", len(self._active))

    def unregister(self, ws: Any) -> None:
        """注销一条 WS 连接，并 reject 该 ws 发出的 pending 请求。

        多连接场景下只 reject 属于这条 ws 的请求，其它连接的请求保留。
        若这是最后一条活跃连接，reject 全部 pending。
        """
        self._active.discard(ws)
        logger.debug("WsApiTransport: unregistered ws (%d active)", len(self._active))
        # Reject pending requests issued by this ws
        to_reject = [echo for echo, w in self._echo_ws.items() if w is ws]
        for echo in to_reject:
            fut = self._pending.pop(echo, None)
            self._echo_ws.pop(echo, None)
            if fut is not None and not fut.done():
                fut.set_exception(ConnectionError("OneBot WS connection closed"))
                logger.debug("WsApiTransport: rejected pending echo=%s (ws closed)", echo)
        if not self._active:
            self._reject_all_pending("OneBot WS connection closed")

    def on_text(self, raw: str) -> bool:
        """处理一条收到的 text 帧。

        若是响应帧（有 ``echo`` 且在 pending 表中）则 resolve 对应 future 并返回 True；
        否则返回 False（事件帧或未知响应，交给 parser 处理）。

        不会抛异常——解析失败视为非响应帧，返回 False。
        """
        try:
            data: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(data, dict):
            return False
        echo = data.get("echo")
        if not echo or echo not in self._pending:
            return False
        fut = self._pending.pop(echo)
        self._echo_ws.pop(echo, None)
        if not fut.done():
            fut.set_result(data)
        try:
            data_preview = json.dumps(data.get("data"), ensure_ascii=False)[:500]
        except (TypeError, ValueError):
            data_preview = "<unserializable>"
        logger.debug(
            "WsApiTransport: resolved echo=%s retcode=%s data=%s",
            echo, data.get("retcode"), data_preview,
        )
        return True

    async def request(
        self, action: str, params: dict[str, Any] | None = None, timeout: float | None = None,
    ) -> dict[str, Any]:
        """发送一个 API 请求并等待响应。

        返回完整的响应字典 ``{"retcode", "data", "status", "msg", "echo", ...}``。
        超时抛 ``asyncio.TimeoutError``；无活跃连接抛 ``RuntimeError``；
        WS 断开导致 pending 被取消抛 ``ConnectionError``。
        """
        ws = self._pick_ws()
        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = fut
        self._echo_ws[echo] = ws
        frame = {"action": action, "params": params or {}, "echo": echo}
        logger.debug(
            "WsApiTransport: sending action=%s echo=%s params=%s",
            action, echo, json.dumps(params or {}, ensure_ascii=False)[:500],
        )
        try:
            await ws.send_json(frame)
        except Exception as exc:
            self._pending.pop(echo, None)
            self._echo_ws.pop(echo, None)
            if not fut.done():
                fut.cancel()
            raise RuntimeError(f"failed to send WS API frame for {action!r}: {exc}") from exc

        wait_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        try:
            return await asyncio.wait_for(fut, timeout=wait_timeout)
        except TimeoutError:
            self._pending.pop(echo, None)
            self._echo_ws.pop(echo, None)
            if not fut.done():
                fut.cancel()
            logger.warning("WsApiTransport: request %s timed out (echo=%s, %.1fs)", action, echo, wait_timeout)
            raise
        except Exception:
            # Future 被 _reject_all_pending / unregister 设置了 ConnectionError 等
            self._pending.pop(echo, None)
            self._echo_ws.pop(echo, None)
            raise

    def _pick_ws(self) -> Any:
        if not self._active:
            raise RuntimeError("no active OneBot WS connection for API call")
        # 取第一个活跃连接（set 迭代顺序不保证，但对单实例部署无影响；多实例场景极少见）
        return next(iter(self._active))

    def _reject_all_pending(self, reason: str) -> None:
        if not self._pending:
            return
        pending = list(self._pending.items())
        self._pending.clear()
        self._echo_ws.clear()
        for echo, fut in pending:
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
                logger.debug("WsApiTransport: rejected pending echo=%s (%s)", echo, reason)

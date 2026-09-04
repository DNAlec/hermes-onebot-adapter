"""OneBot 11 API 调用的 WebSocket 传输层。

OneBot 11 的 WebSocket 连接是双向的：同一条 WS 既推送事件帧（带 ``post_type``），
也接受 API 请求帧（``{"action", "params", "echo"}``）并回响应帧
（``{"retcode", "data", "echo"}``）。本模块用 ``echo`` 字段做请求-响应关联，
让上层 ``OneBotApi`` 用纯异步 ``call(action, params)`` 接口复用同一条 WS 发送消息。

ws_reverse / ws_forward 在每条 WS 连接建立/断开时调用 ``register(ws)`` /
``unregister(ws)``；在收到 text 帧时先调用 ``on_text(raw)``——若是响应帧（命中
adapter 或 passthrough waiter）则 consume 并返回 True，否则返回 False 交给事件解析路径。

多条 WS 同时活跃时（reverse 模式下多个 OneBot 实例拨入），``_pick_ws`` 取第一个活跃连接发请求。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any

from onebot_adapter.logging_utils import safe_json

logger = logging.getLogger(__name__)

# Bound for the duration of one inbound event's parse/dispatch so API calls
# made while handling that event (get_msg, get_forward_msg, …) go back on
# the same OneBot WebSocket that delivered it.  Outbound plugin/WebUI calls
# leave this unset and fall back to _pick_ws().
_current_request_ws: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "onebot_request_ws", default=None,
)

_DEFAULT_TIMEOUT = 300.0
# Match AdapterConfig.file_upload_timeout default; cascade passthrough uses this
# ceiling so upload/flash actions are not cut short of adapter-side waits.
_DEFAULT_PASSTHROUGH_TIMEOUT = 600.0
# Cap concurrent cascade passthrough waiters (adapter request() waiters excluded).
_DEFAULT_MAX_PASSTHROUGH = 256
_ECHO_IN_FLIGHT = "echo already in flight"
_ECHO_UNHASHABLE = "echo must be a hashable JSON value"
_ECHO_TIMEOUT = "OneBot API timed out"
_ECHO_TOO_MANY = "too many in-flight passthrough requests"


def bind_request_ws(ws: Any) -> contextvars.Token[Any]:
    return _current_request_ws.set(ws)


def reset_request_ws(token: contextvars.Token[Any]) -> None:
    _current_request_ws.reset(token)


@dataclass
class _Waiter:
    """One in-flight echo. Exactly one of ``future`` / ``on_response`` is set."""

    ws: Any
    echo: Any = None
    future: asyncio.Future[dict[str, Any]] | None = None
    on_response: Callable[[str], None] | None = None
    owner: Any = None
    timeout_handle: asyncio.TimerHandle | None = None


class WsApiTransport:
    """关联 OneBot WebSocket 上的 API 请求-响应。

    线程安全：所有方法应在同一个事件循环里调用（与 aiohttp 一致）。
    """

    def __init__(
        self,
        *,
        passthrough_timeout: float = _DEFAULT_PASSTHROUGH_TIMEOUT,
        max_passthrough: int = _DEFAULT_MAX_PASSTHROUGH,
    ) -> None:
        self._active: set[Any] = set()
        # echo key → waiter (adapter future and cascade passthrough share this table)
        self._waiters: dict[Hashable, _Waiter] = {}
        self.passthrough_timeout = passthrough_timeout
        self.max_passthrough = max_passthrough

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
        to_fail = [key for key, waiter in self._waiters.items() if waiter.ws is ws]
        for key in to_fail:
            self._fail_waiter(key, "OneBot WS connection closed")
        if not self._active:
            self._reject_all_pending("OneBot WS connection closed")

    def on_text(self, raw: str) -> bool:
        """处理一条收到的 text 帧。

        若是响应帧（命中 waiter）则 consume 并返回 True；
        否则返回 False（事件帧或未知响应，交给 parser 处理）。

        不会抛异常——解析失败视为非响应帧，返回 False。
        """
        try:
            data: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(data, dict):
            return False
        key = echo_key(data.get("echo"))
        if key is None:
            return False
        waiter = self._pop_waiter(key)
        if waiter is None:
            return False
        if waiter.future is not None and not waiter.future.done():
            waiter.future.set_result(data)
            logger.debug(
                "WsApiTransport: resolved echo=%s retcode=%s data=%s",
                key, data.get("retcode"), safe_json(data.get("data"), 500),
            )
        if waiter.on_response is not None:
            try:
                waiter.on_response(raw)
            except Exception:
                logger.exception("WsApiTransport: passthrough callback failed echo=%s", key)
        return True

    async def request(
        self, action: str, params: dict[str, Any] | None = None, timeout: float | None = None,
    ) -> dict[str, Any]:
        """发送一个 API 请求并等待响应。

        返回完整的响应字典 ``{"retcode", "data", "status", "msg", "echo", ...}``。
        超时抛 ``asyncio.TimeoutError``；无活跃连接抛 ``RuntimeError``；
        WS 断开导致 pending 被取消抛 ``ConnectionError``。

        ``request()`` allocates a fresh uuid4 echo and never reuses an in-flight key.
        """
        ws = self._pick_ws()
        echo = uuid.uuid4().hex
        while echo in self._waiters:
            echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        wait_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        self._arm_waiter(
            echo, _Waiter(ws=ws, echo=echo, future=fut), wait_timeout,
        )
        frame = {"action": action, "params": params or {}, "echo": echo}
        logger.debug(
            "WsApiTransport: sending action=%s echo=%s params=%s",
            action, echo, safe_json(params or {}, 500),
        )
        try:
            await ws.send_json(frame)
        except Exception as exc:
            self._pop_waiter(echo)
            # If unregister() already set a ConnectionError on the future (WS
            # closed between _pick_ws and send_json), re-raise that — it's the
            # more informative and documented error for this condition.
            if not fut.done():
                fut.cancel()
                raise RuntimeError(f"failed to send WS API frame for {action!r}: {exc}") from exc
            raise fut.exception() from exc

        try:
            return await fut
        except TimeoutError:
            logger.warning(
                "WsApiTransport: request %s timed out (echo=%s, %.1fs)", action, echo, wait_timeout,
            )
            raise
        except asyncio.CancelledError:
            self._pop_waiter(echo)
            if not fut.done():
                fut.cancel()
            raise
        except Exception:
            # Future 被 _reject_all_pending / unregister 设置了 ConnectionError 等
            self._pop_waiter(echo)
            raise

    async def passthrough(
        self, raw: str, on_response: Callable[[str], None], *,
        owner: Any = None, timeout: float | None = None,
    ) -> None:
        """Send *raw* unchanged and deliver the matching echo response to *on_response*.

        Occupies the same waiter table as :meth:`request`.  Duplicate or
        unhashable echos fail immediately via *on_response* and are not sent.
        At most :attr:`max_passthrough` passthrough waiters may be in flight
        (adapter ``request()`` waiters do not count).  Default wait is
        :attr:`passthrough_timeout` (the adapter upload ceiling).
        """
        ws = self._pick_ws()
        echo = echo_of(raw)
        key = echo_key(echo)
        if echo is not None and key is None:
            on_response(failed_response(echo, _ECHO_UNHASHABLE))
            return
        if key is not None:
            if key in self._waiters:
                on_response(failed_response(echo, _ECHO_IN_FLIGHT))
                return
            if self._passthrough_inflight() >= self.max_passthrough:
                on_response(failed_response(echo, _ECHO_TOO_MANY))
                return
            wait_timeout = timeout if timeout is not None else self.passthrough_timeout
            self._arm_waiter(
                key,
                _Waiter(ws=ws, echo=echo, on_response=on_response, owner=owner),
                wait_timeout,
            )
        logger.debug("WsApiTransport: passthrough raw frame %s", safe_json(raw, 500))
        try:
            await ws.send_str(raw)
        except Exception:
            if key is not None:
                self._pop_waiter(key)
            raise

    def drop_owner(self, owner: Any) -> None:
        """Drop passthrough waiters owned by a disconnecting client.

        ``None`` is a no-op: adapter ``request()`` waiters carry no owner,
        so matching on None would strand every in-flight request.
        """
        if owner is None:
            return
        stale = [key for key, waiter in self._waiters.items() if waiter.owner is owner]
        for key in stale:
            self._pop_waiter(key)

    def _passthrough_inflight(self) -> int:
        return sum(1 for waiter in self._waiters.values() if waiter.on_response is not None)

    def _pick_ws(self) -> Any:
        if not self._active:
            raise RuntimeError("no active OneBot WS connection for API call")
        current = _current_request_ws.get()
        if current is not None and current in self._active:
            return current
        # 无入站绑定（插件/WebUI 主动调用）时取一条活跃连接。
        return next(iter(self._active))

    def _arm_waiter(self, key: Hashable, waiter: _Waiter, timeout: float) -> None:
        self._waiters[key] = waiter
        waiter.timeout_handle = asyncio.get_running_loop().call_later(
            timeout, self._on_waiter_timeout, key,
        )

    def _pop_waiter(self, key: Hashable) -> _Waiter | None:
        waiter = self._waiters.pop(key, None)
        if waiter is not None and waiter.timeout_handle is not None:
            waiter.timeout_handle.cancel()
            waiter.timeout_handle = None
        return waiter

    def _on_waiter_timeout(self, key: Hashable) -> None:
        waiter = self._pop_waiter(key)
        if waiter is None:
            return
        if waiter.future is not None and not waiter.future.done():
            waiter.future.set_exception(TimeoutError(_ECHO_TIMEOUT))
            return
        if waiter.on_response is None:
            return
        logger.warning("WsApiTransport: passthrough timed out (echo=%s)", key)
        try:
            waiter.on_response(failed_response(waiter.echo, _ECHO_TIMEOUT))
        except Exception:
            logger.exception("WsApiTransport: passthrough timeout callback echo=%s", key)

    def _fail_waiter(self, key: Hashable, reason: str) -> None:
        waiter = self._pop_waiter(key)
        if waiter is None:
            return
        if waiter.future is not None and not waiter.future.done():
            waiter.future.set_exception(ConnectionError(reason))
            logger.debug("WsApiTransport: rejected pending echo=%s (%s)", key, reason)
        if waiter.on_response is not None:
            try:
                waiter.on_response(failed_response(waiter.echo, reason))
            except Exception:
                logger.exception("WsApiTransport: passthrough fail callback echo=%s", key)

    def _reject_all_pending(self, reason: str) -> None:
        for key in list(self._waiters):
            self._fail_waiter(key, reason)


def echo_of(raw: str) -> Any | None:
    """Return the frame's ``echo``, or ``None`` when absent/empty."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    echo = data.get("echo")
    if echo is None or echo == "":
        return None
    return echo


def echo_key(echo: Any) -> Hashable | None:
    """Dict key for *echo*, or ``None`` if absent, empty, or unhashable."""
    if echo is None or echo == "":
        return None
    try:
        hash(echo)
    except TypeError:
        return None
    return echo


def failed_response(echo: Any, message: str) -> str:
    return json.dumps(
        {
            "status": "failed",
            "retcode": -1,
            "data": None,
            "echo": echo,
            "msg": message,
            "wording": message,
        },
        ensure_ascii=False,
    )

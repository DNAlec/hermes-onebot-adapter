"""OneBot 11 API async client over WebSocket.

通过注入的 ``WsApiTransport`` 在 OneBot 的双向 WS 连接上发送 API 调用。
上层调用方（relay、webui、name_resolver、parser）统一用 ``call(action, params)``
接口,``send_group_msg`` / ``get_login_info`` / ``get_msg`` 等方法封装常用 action。

WS 帧格式：请求 ``{"action", "params", "echo"}``，响应 ``{"retcode", "data", "echo", ...}``。
响应帧由 ``WsApiTransport.on_text`` 拦截并通过 ``echo`` 关联到对应 future。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any
from urllib.parse import unquote, urlparse

from onebot_adapter.logging_utils import safe_json
from onebot_adapter.onebot.ws_api import WsApiTransport

logger = logging.getLogger(__name__)

_DEBUG_LOG_MAX = 2000
_GROUP_UPLOAD_TIMEOUT = 60.0
_PRIVATE_UPLOAD_TIMEOUT = 600.0
_GROUP_UPLOAD_CONFIRM_DELAYS = (0.0, 2.0, 5.0)
_GROUP_UPLOAD_CONFIRM_QUERY_TIMEOUT = 8.0
_GROUP_UPLOAD_CONFIRM_HISTORY_COUNT = 100
_GROUP_UPLOAD_CONFIRM_CLOCK_SKEW = 3.0


class UploadOutcomeUnknownError(RuntimeError):
    """An upload timed out and its side effect could not be confirmed safely."""


class OneBotApi:
    """OneBot 11 API 客户端,走 WebSocket 传输层调用 OneBot API。"""

    def __init__(self, ws_transport: WsApiTransport) -> None:
        self._ws = ws_transport

    @property
    def connected(self) -> bool:
        """Whether a OneBot WebSocket is currently available for API calls."""
        return self._ws.has_active

    async def call(
        self, action: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        params = params or {}
        logger.debug(
            "OneBot API call: %s params=%s",
            action, safe_json(params, _DEBUG_LOG_MAX),
        )
        started = time.monotonic()
        started_wall = time.time()
        if timeout is None and action == "upload_group_file":
            request_timeout = _GROUP_UPLOAD_TIMEOUT
        elif timeout is None and action == "upload_private_file":
            request_timeout = _PRIVATE_UPLOAD_TIMEOUT
        else:
            request_timeout = timeout
        try:
            data = await self._ws.request(action, params, timeout=request_timeout)
        except TimeoutError as exc:
            if action == "upload_group_file" and timeout is None:
                confirmed = await self._confirm_group_file_upload(params, started_wall)
                if confirmed is not None:
                    logger.warning(
                        "OneBot API upload_group_file timed out but was confirmed in group history: %s",
                        safe_json(confirmed, 500),
                    )
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "file_id": confirmed.get("file_id") or None,
                            "message_id": confirmed.get("message_id"),
                            "confirmed_after_timeout": True,
                        },
                    }
                group_id = params.get("group_id", "")
                name = params.get("name") or os.path.basename(str(params.get("file", "")))
                raise UploadOutcomeUnknownError(
                    f"upload_group_file timed out and could not be confirmed safely "
                    f"(group_id={group_id}, name={name!r}); the file may already have been uploaded, "
                    "do not retry automatically"
                ) from exc
            logger.warning(
                "OneBot API %s request failed duration_ms=%.1f: %s",
                action, (time.monotonic() - started) * 1000, exc,
            )
            raise
        except Exception as exc:
            logger.warning(
                "OneBot API %s request failed duration_ms=%.1f: %s",
                action, (time.monotonic() - started) * 1000, exc,
            )
            raise
        duration_ms = (time.monotonic() - started) * 1000
        logger.debug(
            "OneBot API %s response duration_ms=%.1f: %s",
            action, duration_ms, safe_json(data, _DEBUG_LOG_MAX),
        )
        if data.get("retcode", 0) != 0 or data.get("status") == "failed":
            error_message = data.get("msg") or data.get("message") or data.get("wording")
            logger.warning(
                "OneBot API %s error: retcode=%s status=%s msg=%s",
                action, data.get("retcode"), data.get("status"), error_message,
            )
            raise RuntimeError(
                f"OneBot API error {action}: retcode={data.get('retcode')} "
                f"status={data.get('status')} msg={error_message}"
            )
        logger.debug("OneBot API %s -> ok duration_ms=%.1f", action, duration_ms)
        return data

    async def _confirm_group_file_upload(
        self, params: dict[str, Any], started_wall: float,
    ) -> dict[str, Any] | None:
        """Conservatively confirm a timed-out upload from recent raw group history."""
        try:
            group_id = int(params["group_id"])
        except (KeyError, TypeError, ValueError):
            return None
        expected_name = str(params.get("name") or os.path.basename(str(params.get("file", ""))))
        if not expected_name:
            return None
        expected_size = _local_file_size(str(params.get("file", "")))
        candidates: dict[tuple[str, str], dict[str, Any]] = {}

        for delay in _GROUP_UPLOAD_CONFIRM_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self.call(
                    "get_group_msg_history",
                    {
                        "group_id": group_id,
                        "message_seq": 0,
                        "count": _GROUP_UPLOAD_CONFIRM_HISTORY_COUNT,
                    },
                    timeout=_GROUP_UPLOAD_CONFIRM_QUERY_TIMEOUT,
                )
            except Exception as exc:
                logger.warning("group upload confirmation query failed: %s", exc)
                continue
            messages = (response.get("data") or {}).get("messages") or []
            matches = _matching_group_file_messages(
                messages,
                group_id=group_id,
                expected_name=expected_name,
                expected_size=expected_size,
                started_wall=started_wall,
            )
            for match in matches:
                candidates[_upload_candidate_key(match)] = match
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    async def get_login_info(self) -> dict[str, Any]:
        return (await self.call("get_login_info"))["data"]

    async def send_private_msg(self, user_id: int, message: list[dict]) -> dict[str, Any]:
        return (await self.call("send_private_msg", {"user_id": user_id, "message": message}))["data"]

    async def send_group_msg(self, group_id: int, message: list[dict]) -> dict[str, Any]:
        return (await self.call("send_group_msg", {"group_id": group_id, "message": message}))["data"]

    async def get_msg(self, message_id: int) -> dict[str, Any]:
        return (await self.call("get_msg", {"message_id": message_id}))["data"]

    async def get_forward_msg(self, message_id: str) -> dict[str, Any]:
        return (await self.call("get_forward_msg", {"message_id": message_id}))["data"]

    async def get_group_info(self, group_id: int, no_cache: bool = True) -> dict[str, Any]:
        return (await self.call("get_group_info", {"group_id": group_id, "no_cache": no_cache}))["data"]

    async def get_stranger_info(self, user_id: int, no_cache: bool = True) -> dict[str, Any]:
        return (await self.call("get_stranger_info", {"user_id": user_id, "no_cache": no_cache}))["data"]

    async def get_group_member_info(
        self, group_id: int, user_id: int, no_cache: bool = False,
    ) -> dict[str, Any]:
        return (await self.call("get_group_member_info", {
            "group_id": group_id, "user_id": user_id, "no_cache": no_cache,
        }))["data"]

    async def upload_group_file(self, group_id: int, file: str, name: str) -> None:
        await self.call("upload_group_file", {"group_id": group_id, "file": file, "name": name})

    async def upload_private_file(self, user_id: int, file: str, name: str) -> None:
        await self.call("upload_private_file", {"user_id": user_id, "file": file, "name": name})


def text_segment(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def image_segment(file_url: str) -> dict:
    return {"type": "image", "data": {"file": file_url}}


def reply_segment(message_id: int | str) -> dict:
    return {"type": "reply", "data": {"id": str(message_id)}}


def record_segment(file_url: str) -> dict:
    return {"type": "record", "data": {"file": file_url}}


def video_segment(file_url: str) -> dict:
    return {"type": "video", "data": {"file": file_url}}


def _local_file_size(file_ref: str) -> int | None:
    path = file_ref
    if file_ref.startswith("file://"):
        path = unquote(urlparse(file_ref).path)
    if not path or not os.path.isfile(path):
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _matching_group_file_messages(
    messages: Any,
    *,
    group_id: int,
    expected_name: str,
    expected_size: int | None,
    started_wall: float,
) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("group_id", "")) != str(group_id):
            continue
        if str(message.get("user_id", "")) != str(message.get("self_id", "")):
            continue
        try:
            message_time = float(message["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            message_time < started_wall - _GROUP_UPLOAD_CONFIRM_CLOCK_SKEW
            or message_time > time.time() + _GROUP_UPLOAD_CONFIRM_CLOCK_SKEW
        ):
            continue
        segments = message.get("message")
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict) or segment.get("type") != "file":
                continue
            file_data = segment.get("data") or {}
            if not isinstance(file_data, dict):
                continue
            if str(file_data.get("file") or file_data.get("name") or "") != expected_name:
                continue
            reported_size = file_data.get("file_size") or file_data.get("size")
            if expected_size is not None:
                if reported_size in (None, "", 0, "0"):
                    continue
                try:
                    if int(reported_size) != expected_size:
                        continue
                except (TypeError, ValueError):
                    continue
            candidate = {
                "message_id": message.get("message_id"),
                "file_id": file_data.get("file_id"),
            }
            if not candidate["message_id"] and not candidate["file_id"]:
                continue
            key = _upload_candidate_key(candidate)
            if key not in seen:
                seen.add(key)
                matches.append(candidate)
    return matches


def _upload_candidate_key(candidate: dict[str, Any]) -> tuple[str, str]:
    message_id = str(candidate.get("message_id") or "")
    if message_id:
        return ("message", message_id)
    return ("file", str(candidate.get("file_id") or ""))

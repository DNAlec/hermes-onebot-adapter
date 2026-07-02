"""OneBot 11 API async client over WebSocket.

通过注入的 ``WsApiTransport`` 在 OneBot 的双向 WS 连接上发送 API 调用。
上层调用方（relay、webui、name_resolver、parser）统一用 ``call(action, params)``
接口,``send_group_msg`` / ``get_login_info`` / ``get_msg`` 等方法封装常用 action。

WS 帧格式：请求 ``{"action", "params", "echo"}``，响应 ``{"retcode", "data", "echo", ...}``。
响应帧由 ``WsApiTransport.on_text`` 拦截并通过 ``echo`` 关联到对应 future。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from onebot_adapter.onebot.ws_api import WsApiTransport

logger = logging.getLogger(__name__)

_DEBUG_LOG_MAX = 2000


class OneBotApi:
    """OneBot 11 API 客户端,走 WebSocket 传输层调用 OneBot API。"""

    def __init__(self, ws_transport: WsApiTransport) -> None:
        self._ws = ws_transport

    async def close(self) -> None:
        # WS 连接由 ws_reverse/forward 管理生命周期，这里不关闭
        return None

    async def call(
        self, action: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        logger.debug(
            "OneBot API call: %s params=%s",
            action, json.dumps(params or {}, ensure_ascii=False)[:_DEBUG_LOG_MAX],
        )
        try:
            data = await self._ws.request(action, params, timeout=timeout)
        except Exception as exc:
            logger.warning("OneBot API %s request failed: %s", action, exc)
            raise
        logger.debug("OneBot API %s response: %s", action, json.dumps(data, ensure_ascii=False)[:_DEBUG_LOG_MAX])
        if data.get("retcode", 0) != 0:
            logger.warning(
                "OneBot API %s error: retcode=%s status=%s msg=%s",
                action, data.get("retcode"), data.get("status"), data.get("msg"),
            )
            raise RuntimeError(
                f"OneBot API error {action}: retcode={data.get('retcode')} "
                f"status={data.get('status')} msg={data.get('msg')}"
            )
        logger.debug("OneBot API %s -> ok", action)
        return data

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
        await self.call("upload_group_file", {"group_id": group_id, "file": file, "name": name}, timeout=60)

    async def upload_private_file(self, user_id: int, file: str, name: str) -> None:
        await self.call("upload_private_file", {"user_id": user_id, "file": file, "name": name}, timeout=60)


def text_segment(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def image_segment(file_url: str) -> dict:
    return {"type": "image", "data": {"file": file_url}}


def at_segment(qq: int | str) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def reply_segment(message_id: int | str) -> dict:
    return {"type": "reply", "data": {"id": str(message_id)}}


def record_segment(file_url: str) -> dict:
    return {"type": "record", "data": {"file": file_url}}


def video_segment(file_url: str) -> dict:
    return {"type": "video", "data": {"file": file_url}}

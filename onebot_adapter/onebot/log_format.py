"""Render message-flow log lines in a NapCat-like style.

Produces concise INFO-level log entries for the message pipeline:

    接收 <- 群聊 [群号(群名)] [昵称(QQ号)] 正文…
    发送 -> 群聊 [群号(群名)] [回复消息#123] @昵称(QQ) 正文…
    接收 <- 私聊 [昵称(QQ号)] 正文…
    发送 -> 私聊 [QQ号] 正文…

The body is truncated to ``log_message_preview`` characters (default 100, 0 = no
truncate) and suffixed with ``...`` when truncated.

Receive/send *preview* copies go to ``onebot_adapter.onebot.message_preview``
(console/WebUI only, ``propagate=False``). Persistent copies go to
``onebot_adapter.file``. Drop/reject lines use this module logger so they
appear in both console and ``adapter.log``.
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any

from onebot_adapter.relay.protocol import DroppedEvent, FilteredEvent, NormalizedEvent

logger = logging.getLogger(__name__)
PREVIEW_LOGGER_NAME = "onebot_adapter.onebot.message_preview"
_preview_logger = logging.getLogger(PREVIEW_LOGGER_NAME)
# Set at import, not lazily on first use: anything inspecting propagation
# (mirrored handlers assume no root copy; pytest's caplog attaches to
# non-propagating loggers at phase start) must see the same state always.
_preview_logger.propagate = False
_file_logger = logging.getLogger("onebot_adapter.file")

outbound_log_req_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "outbound_log_req_id", default="",
)

_MESSAGE_SEND_ACTIONS = frozenset({"send_msg", "send_group_msg", "send_private_msg"})
_UPLOAD_ACTIONS = frozenset({"upload_group_file", "upload_private_file"})
OUTBOUND_LOG_ACTIONS = _MESSAGE_SEND_ACTIONS | _UPLOAD_ACTIONS

_DROP_REASON_FROM_FILTER = {
    "command": "command",
    "bot_blacklist": "blacklist",
    "dm_policy": "user_filter",
    "rate_limit": "rate_limit",
    "rate_limit_storage": "rate_limit",
}


def configure_preview_logger() -> logging.Logger:
    """Ensure the preview logger never propagates into the file handler tree."""
    _preview_logger.propagate = False
    return _preview_logger


def sync_preview_logger_handlers(*, file_handler: logging.Handler | None = None) -> None:
    """Mirror root handlers (console + WebUI) onto the preview logger.

    The file handler is excluded even if it is ever attached to root.
    """
    preview = configure_preview_logger()
    root = logging.getLogger()
    wanted = [handler for handler in root.handlers if handler is not file_handler]
    for handler in list(preview.handlers):
        if handler not in wanted:
            preview.removeHandler(handler)
    for handler in wanted:
        if handler not in preview.handlers:
            preview.addHandler(handler)


def detach_preview_logger_handlers() -> None:
    preview = logging.getLogger(PREVIEW_LOGGER_NAME)
    for handler in list(preview.handlers):
        preview.removeHandler(handler)


def truncate(text: str, limit: int) -> str:
    """Truncate *text* to *limit* characters, appending ``...`` when shorter.

    *limit* <= 0 disables truncation.
    """
    if limit and len(text) > limit:
        return text[:limit] + "..."
    return text


def format_recv_line(event: NormalizedEvent, preview: int = 40) -> str:
    """Render the receive-side log line for a parsed event.

    Group chat:  ``群聊 [群号(群名)] [昵称(QQ号)] body``
    DM:          ``私聊 [昵称(QQ号)] body``

    ``event.chat_name`` is already populated by the parser (group name or DM
    nickname), so this function is synchronous and performs no I/O.
    """
    body = truncate(event.text or "", preview)
    if event.chat_type == "group":
        # event.chat_name for groups is "群号(群名)" or "群号"
        chat = f"[{event.chat_name}]" if event.chat_name else f"[{event.chat_id}]"
        sender = f"[{event.user_name}({event.user_id})]" if event.user_id else f"[{event.user_name}]"
        return f"群聊 {chat} {sender} {body}"
    # DM
    sender = f"[{event.user_name}({event.user_id})]" if event.user_id else f"[{event.user_name}]"
    return f"私聊 {sender} {body}"


async def format_send_line(
    *,
    chat_id: str,
    segs: list[dict],
    is_group: bool,
    group_name: str = "",
    reply_to: str | None = None,
    preview: int = 40,
    name_resolver: Any = None,
) -> str:
    """Render the send-side log line from the OneBot segments being sent.

    Group chat:  ``群聊 [群号(群名)] [回复消息#123] @昵称(QQ) 正文片段``
    DM:         ``私聊 [QQ号] 正文片段``

    Segments are rendered:
      reply → ``[回复消息#<id>]`` (uses *reply_to* if not embedded in segs)
      at    → ``@昵称(QQ)`` (resolves via *name_resolver*, falls back to QQ)
      image → ``[图片]``
      record → ``[语音]``
      video → ``[视频]``
      file  → ``[文件:<name>]``
      text  → raw text (preserves @ markers as-is)

    *name_resolver* is an optional :class:`NameResolver` with an LRU cache; when
    lookup fails or the resolver is unavailable, ``@QQ`` is shown instead.  The
    function never raises — on any error it falls back to a simplified form.
    """
    try:
        # Determine numeric chat id.  For per_user group sessions the format is
        # "group:<gid>:user:<uid>" — we want the group id (segment after the
        # first colon), not the trailing user id.
        if chat_id.startswith("group:"):
            num_id = chat_id.split(":", 2)[1]
        else:
            num_id = chat_id

        if is_group:
            chat = f"[{num_id}({group_name})]" if group_name else f"[{num_id}]"
            prefix = f"群聊 {chat}"
        else:
            prefix = f"私聊 [{num_id}]"

        body_parts: list[str] = []

        for s in segs:
            t = s.get("type")
            data = s.get("data", {}) or {}
            if t == "text":
                body_parts.append(data.get("text", ""))
            elif t == "at":
                qq = str(data.get("qq", ""))
                name = ""
                if name_resolver and qq:
                    try:
                        name = await name_resolver.resolve(qq, str(num_id) if is_group else "")
                    except Exception:
                        name = ""
                if name:
                    body_parts.append(f"@{name}({qq})")
                else:
                    body_parts.append(f"@{qq}")
            elif t == "image":
                body_parts.append("[图片]")
            elif t == "record":
                body_parts.append("[语音]")
            elif t == "video":
                body_parts.append("[视频]")
            elif t == "file":
                fname = data.get("file") or data.get("name", "")
                body_parts.append(f"[文件:{fname}]" if fname else "[文件]")
            elif t == "reply":
                rid = data.get("id")
                if rid:
                    body_parts.append(f"[回复消息#{rid}]")
                else:
                    body_parts.append("[回复消息]")

        # When reply_to is provided separately (send_text path builds segs
        # without an explicit reply segment id), surface it.
        if reply_to and not any(s.get("type") == "reply" for s in segs):
            body_parts.insert(0, f"[回复消息#{reply_to}]")

        body = truncate(" ".join(p for p in body_parts if p).strip(), preview)
        return f"{prefix} {body}".rstrip()
    except Exception:
        # Never let logging break the send path
        return f"{'群聊' if is_group else '私聊'} {chat_id}"


def log_recv_line(
    event: NormalizedEvent,
    preview: int = 40,
    file_message_mode: str = "preview",
) -> None:
    """Log a receive-side line to the console/WebUI and optionally the file.

    ``file_message_mode`` controls the dedicated persistent message copy:
    ``none`` disables it, ``preview`` applies the configured preview limit, and
    ``full`` preserves the historical full-body behaviour.
    """
    configure_preview_logger()
    line = format_recv_line(event, preview)
    _preview_logger.info("接收 <- %s [message_id=%s]", line, event.message_id)
    if file_message_mode != "none":
        file_preview = 0 if file_message_mode == "full" else preview
        _file_logger.info(
            "接收 <- %s [message_id=%s]",
            format_recv_line(event, file_preview),
            event.message_id,
        )


def log_drop_line(
    reason: str,
    *,
    chat_id: str = "",
    user_id: str = "",
    user_name: str = "",
    message_id: str = "",
    command_name: str = "",
) -> None:
    """Log a DEBUG drop/reject without the message body."""
    extra = f" cmd={command_name}" if command_name else ""
    logger.debug(
        "丢弃 -- reason=%s chat_id=%s user=%s(%s) message_id=%s%s",
        reason,
        chat_id or "-",
        user_name or "-",
        user_id or "-",
        message_id or "-",
        extra,
    )


def log_cascaded_event(event: DroppedEvent) -> None:
    """Log a trigger-miss that was forwarded to a cascade client (no body)."""
    extra = f" cmd={event.command_name}" if event.command_name else ""
    logger.debug(
        "转发 -- reason=%s chat_id=%s user=%s(%s) message_id=%s%s",
        event.reason,
        event.chat_id or "-",
        event.user_name or "-",
        event.user_id or "-",
        event.message_id or "-",
        extra,
    )


def log_dropped_event(event: DroppedEvent | FilteredEvent) -> None:
    """Log a parser/app drop using the stable reason codes."""
    if isinstance(event, FilteredEvent):
        reason = _DROP_REASON_FROM_FILTER.get(event.filter_type, event.filter_type or "command")
        log_drop_line(
            reason,
            chat_id=event.chat_id,
            user_id=event.user_id,
            user_name=event.user_name,
            message_id=event.message_id,
            command_name=event.command_name,
        )
        return
    log_drop_line(
        event.reason,
        chat_id=event.chat_id,
        user_id=event.user_id,
        user_name=event.user_name,
        message_id=event.message_id,
        command_name=event.command_name,
    )


def _message_to_segments(message: Any) -> list[dict]:
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    if isinstance(message, list):
        return [item for item in message if isinstance(item, dict)]
    return []


def describe_outbound_send(action: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Build ``log_send_line`` kwargs for a successful OneBot send/upload."""
    if action not in OUTBOUND_LOG_ACTIONS:
        return None
    if action in _UPLOAD_ACTIONS:
        is_group = action == "upload_group_file"
        num_id = params.get("group_id") if is_group else params.get("user_id")
        name = params.get("name") or ""
        if not name:
            file_ref = str(params.get("file", "") or "")
            name = file_ref.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        chat_id = f"group:{num_id}" if is_group else str(num_id or "")
        return {
            "chat_id": chat_id,
            "segs": [{"type": "file", "data": {"name": name}}],
            "is_group": is_group,
            "reply_to": None,
        }

    message_type = str(params.get("message_type") or "")
    group_id = params.get("group_id")
    user_id = params.get("user_id")
    if action == "send_private_msg":
        is_group = False
    elif action == "send_group_msg" or message_type == "group":
        is_group = True
    else:
        is_group = group_id not in (None, "")
    num_id = group_id if is_group else user_id
    chat_id = f"group:{num_id}" if is_group else str(num_id or "")
    segs = _message_to_segments(params.get("message"))
    reply_to = None
    for seg in segs:
        if seg.get("type") == "reply":
            reply_to = str((seg.get("data") or {}).get("id") or "") or None
            break
    return {"chat_id": chat_id, "segs": segs, "is_group": is_group, "reply_to": reply_to}


async def log_send_line(
    *,
    chat_id: str,
    segs: list[dict],
    is_group: bool,
    group_name: str = "",
    reply_to: str | None = None,
    preview: int = 40,
    name_resolver: Any = None,
    file_message_mode: str = "preview",
    req_id: str = "",
    message_id: str = "",
) -> None:
    """Log a send-side line to the console/WebUI and optionally the file.

    Renders the full line once with ``preview=0`` (no truncation) so name
    resolution runs exactly once per call.  The console line is produced by
    truncating the already-rendered full string, avoiding a second pass that
    could re-issue API calls when the first pass had a cache *miss* (failed
    lookups are not cached, so a second pass would hit the API again).
    """
    configure_preview_logger()
    full = await format_send_line(
        chat_id=chat_id, segs=segs, is_group=is_group,
        group_name=group_name, reply_to=reply_to,
        preview=0, name_resolver=name_resolver,
    )
    suffix = " [message_id=%s req_id=%s]"
    _preview_logger.info("发送 -> %s" + suffix, truncate(full, preview), message_id, req_id)
    if file_message_mode != "none":
        file_line = full if file_message_mode == "full" else truncate(full, preview)
        _file_logger.info("发送 -> %s" + suffix, file_line, message_id, req_id)

"""Render message-flow log lines in a NapCat-like style.

Produces concise INFO-level log entries for the message pipeline:

    接收 <- 群聊 [群号(群名)] [昵称(QQ号)] 正文…
    发送 -> 群聊 [群号(群名)] [回复消息#123] @昵称(QQ) 正文…
    接收 <- 私聊 [昵称(QQ号)] 正文…
    发送 -> 私聊 [QQ号] 正文…

The body is truncated to ``log_message_preview`` characters (default 40, 0 = no
truncate) and suffixed with ``...`` when truncated.
"""
from __future__ import annotations

import logging
from typing import Any

from onebot_adapter.relay.protocol import NormalizedEvent

logger = logging.getLogger(__name__)
_file_logger = logging.getLogger("onebot_adapter.file")


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


def log_recv_line(event: NormalizedEvent, preview: int = 40) -> None:
    """Log a receive-side line to both console (truncated) and file (full)."""
    line = format_recv_line(event, preview)
    logger.info("接收 <- %s", line)
    _file_logger.info("接收 <- %s", format_recv_line(event, 0))


async def log_send_line(
    *,
    chat_id: str,
    segs: list[dict],
    is_group: bool,
    group_name: str = "",
    reply_to: str | None = None,
    preview: int = 40,
    name_resolver: Any = None,
) -> None:
    """Log a send-side line to both console (truncated) and file (full).

    Calls ``format_send_line`` twice: once with the configured preview length
    for the truncated console line, then again with ``preview=0`` for the
    full untruncated file line.  The second pass re-runs ``name_resolver.resolve``
    for each @-mention, but the first pass already populated the cache so the
    second pass only hits the fast path (no API calls, just lock + dict lookup).
    """
    line = await format_send_line(
        chat_id=chat_id, segs=segs, is_group=is_group,
        group_name=group_name, reply_to=reply_to,
        preview=preview, name_resolver=name_resolver,
    )
    logger.info("发送 -> %s", line)
    # Derive the full (untruncated) version without re-running name resolution:
    # render the same body with preview=0.  name_resolver.resolve is cached so
    # the second pass is cheap, but the first pass already populated the cache
    # so the second pass only hits the fast path (no API calls).
    full = await format_send_line(
        chat_id=chat_id, segs=segs, is_group=is_group,
        group_name=group_name, reply_to=reply_to,
        preview=0, name_resolver=name_resolver,
    )
    _file_logger.info("发送 -> %s", full)

"""Normalized wire protocol between the adapter service and the Hermes plugin.

All frames are JSON text frames — no binary frames. Media (images, videos,
voice, files) is delivered to the plugin in one of two modes, selected by
the adapter config's ``media_delivery_mode``:

  * ``passthrough`` (default): media URLs are rendered inline in ``text`` as
    placeholders like ``[图1](https://...)``. The LLM fetches them on demand.
    ``media_items`` is empty.
  * ``cache``: media URLs are collected into ``NormalizedEvent.media_items``
    so the plugin can download them via ``cache_image_from_url`` etc. and
    fill ``MessageEvent.media_urls`` with local paths. Text placeholders are
    rendered without URLs (``[图1]``) so the LLM still sees media positions.

Outbound media (send_image/send_voice/...) passes file paths or URLs as
strings in the JSON ``send`` frame — the adapter forwards these to OneBot,
which reads the local file or downloads the URL itself.

Direction notation:
  A->P : adapter service -> Hermes plugin (inbound events, responses)
  P->A : Hermes plugin -> adapter service (send requests, API calls)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = 1

TypeKind = Literal[
    "ready",
    "ping",
    "pong",
    "event",
    "send",
    "api_call",
    "result",
    "error",
    "commands_snapshot",
    "commands_refresh",
    "filtered",
    "idle",
    "hermes_mode_report",
    "mode_refresh",
    "plugin_info",
]

ChatType = Literal["dm", "group"]
SendAction = Literal["send_text", "send_image", "send_voice", "send_video", "send_document"]


def envelope(type_: TypeKind, **fields: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": type_, "v": PROTOCOL_VERSION}
    msg.update(fields)
    return msg


@dataclass
class MediaItem:
    """A media segment extracted from a OneBot message, for plugin-side caching.

    Carries the URL (and file_id for ``file`` segments without a URL) plus
    metadata needed by the plugin to pick the right ``cache_*_from_url`` /
    ``cache_*_from_bytes`` helper when ``media_delivery_mode == "cache"``.
    """

    kind: str          # "image" | "record" | "video" | "file"
    url: str = ""      # direct URL when available; empty for file_id-only segments
    mime: str = ""     # best-effort MIME type (empty if unknown)
    name: str = ""     # filename for file segments (empty otherwise)
    file_id: str = ""  # OneBot file_id (only set for file segments without a URL)
    index: int = 0     # 0-based media index (matches the ``[图N]`` placeholder number - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "mime": self.mime,
            "name": self.name,
            "file_id": self.file_id,
            "index": self.index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MediaItem:
        return cls(
            kind=data.get("kind", ""),
            url=data.get("url", ""),
            mime=data.get("mime", ""),
            name=data.get("name", ""),
            file_id=data.get("file_id", ""),
            index=int(data.get("index", 0)),
        )


@dataclass
class NormalizedEvent:
    """OneBot 11 event reduced to a Hermes-neutral shape.

    Media delivery is controlled by the adapter's ``media_delivery_mode``:

      * ``passthrough`` (default): ``media_items`` is empty; media URLs are
        rendered inline in ``text`` as placeholders like ``[图1](https://...)``.
      * ``cache``: ``media_items`` carries one entry per media segment so the
        plugin can download them; ``text`` placeholders are rendered without
        URLs (``[图1]``) so the LLM still sees media positions.

    No media is downloaded by the adapter and no binary frames are produced.
    """

    message_id: str
    chat_id: str
    chat_type: ChatType
    user_id: str
    user_name: str
    text: str
    reply_to_message_id: str | None = None
    reply_to_text: str | None = None
    timestamp: float = 0.0
    channel_prompt: str | None = None
    is_admin: bool = False
    chat_name: str = ""
    real_seq: str = ""
    media_items: list[MediaItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "text": self.text,
            "reply_to_message_id": self.reply_to_message_id,
            "reply_to_text": self.reply_to_text,
            "timestamp": self.timestamp,
            "channel_prompt": self.channel_prompt,
            "is_admin": self.is_admin,
            "chat_name": self.chat_name,
            "real_seq": self.real_seq,
            "media_items": [m.to_dict() for m in self.media_items],
        }


@dataclass
class CommandInfo:
    """A slash command registered in Hermes (builtin or plugin)."""

    name: str
    description: str = ""
    source: str = ""           # "builtin" | plugin name | "context-engine:..."
    aliases: list[str] = field(default_factory=list)
    args_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "aliases": list(self.aliases),
            "args_hint": self.args_hint,
        }


@dataclass
class FilteredEvent:
    """A message event that was filtered by the command filter.

    Carries enough context for the adapter service to send a reject message
    back to the originating chat via the OneBot HTTP API, without going
    through the Hermes plugin.  This is a process-internal Python object —
    it is never serialised onto the wire (the adapter's ``on_filtered``
    callback receives it directly).
    """

    chat_id: str
    chat_type: ChatType
    user_id: str
    user_name: str
    command_name: str
    reject_message: str
    message_id: str = ""
    reply_to_message_id: str | None = None
    timestamp: float = 0.0


def ready_message(
    onebot_connected: bool,
    adapter_version: str,
    self_id: str = "",
    media_delivery_mode: str = "passthrough",
) -> dict[str, Any]:
    return envelope(
        "ready",
        onebot_connected=onebot_connected,
        adapter_version=adapter_version,
        self_id=self_id,
        media_delivery_mode=media_delivery_mode,
    )


def event_message(event: NormalizedEvent) -> dict[str, Any]:
    return envelope("event", event="message", **event.to_dict())


def send_message(action: SendAction, req_id: str, chat_id: str, **payload: Any) -> dict[str, Any]:
    msg = envelope("send", action=action, req_id=req_id, chat_id=chat_id)
    msg.update(payload)
    return msg


def api_call_message(action: str, req_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return envelope("api_call", action=action, req_id=req_id, params=params)


def result_message(
    req_id: str, success: bool, message_id: str | None = None, error: str | None = None, data: Any = None
) -> dict[str, Any]:
    return envelope(
        "result",
        req_id=req_id,
        success=success,
        message_id=message_id,
        error=error,
        data=data,
    )


def error_message(code: str, message: str) -> dict[str, Any]:
    return envelope("error", code=code, message=message)


def ping_message() -> dict[str, Any]:
    return envelope("ping")


def pong_message() -> dict[str, Any]:
    return envelope("pong")


def commands_snapshot_message(commands: list[CommandInfo]) -> dict[str, Any]:
    """A->P: adapter service ← Hermes plugin.  Plugin pushes the full list of
    slash commands registered in Hermes so the adapter can filter /commands
    before forwarding messages to the plugin."""
    return envelope("commands_snapshot", commands=[c.to_dict() for c in commands])


def commands_refresh_message() -> dict[str, Any]:
    """A->P: adapter service → Hermes plugin.  Adapter asks the plugin to
    re-collect and push a fresh commands_snapshot (e.g. after plugin reload)."""
    return envelope("commands_refresh")


def idle_message(chat_id: str, group_id: str) -> dict[str, Any]:
    """P->A: Hermes plugin -> adapter service.  Plugin fires this after a
    shared-group session finishes processing a turn (via the host's
    ``register_post_delivery_callback`` hook).  The adapter uses it as the
    "busy -> idle" signal to dequeue the next queued message for that group.

    ``chat_id`` is the original event chat_id (``group:<gid>`` form, no
    ``:user:`` suffix — only shared groups send idle).  ``group_id`` is the
    bare numeric group id used as the queue key.
    """
    return envelope("idle", chat_id=chat_id, group_id=group_id)


def hermes_mode_report_message(group_sessions_per_user: bool) -> dict[str, Any]:
    """P->A: Hermes plugin -> adapter service.  Plugin pushes Hermes' current
    ``group_sessions_per_user`` config value so the adapter can decide whether
    shared-group queueing is needed (False ⇒全群共享 session ⇒ 排队有意义;
    True ⇒ 每人独立 session ⇒ 无需排队).  Sent on connect/reconnect and on
    ``mode_refresh`` request.
    """
    return envelope("hermes_mode_report", group_sessions_per_user=bool(group_sessions_per_user))


def mode_refresh_message() -> dict[str, Any]:
    """A->P: adapter service → Hermes plugin.  Ask the plugin to re-read
    Hermes config and push a fresh ``hermes_mode_report`` frame."""
    return envelope("mode_refresh")


def plugin_info_message(plugin_version: str) -> dict[str, Any]:
    """P->A: Hermes plugin → adapter service.  Plugin reports its own version
    (read from ``plugin.yaml`` at startup) so the adapter can detect version
    mismatches and warn the user in the WebUI."""
    return envelope("plugin_info", plugin_version=plugin_version)


def parse_chat_id(chat_id: str) -> tuple[bool, int]:
    """Parse a normalized chat_id into ``(is_group, numeric_id)``.

    Supported formats:
      - ``"group:<gid>"``  → (True, <gid>)   群聊(全群共享 session,Hermes 隔离由其自己配置决定)
      - ``"<uid>"``        → (False, <uid>)  私聊

    Raises ``ValueError`` on malformed input.
    """
    if chat_id.startswith("group:"):
        gid_str = chat_id[len("group:"):]
        return True, int(gid_str)
    return False, int(chat_id)

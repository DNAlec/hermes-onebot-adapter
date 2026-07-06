"""Normalized wire protocol between the adapter service and the Hermes plugin.

All frames are JSON text frames — no binary frames. Media (images, videos,
voice, files) is passed as file paths or URLs in the JSON payload (path
passthrough). The adapter forwards these directly to OneBot/NapCat, which
reads the local file or downloads the URL itself.

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
]

ChatType = Literal["dm", "group"]
SendAction = Literal["send_text", "send_image", "send_voice", "send_video", "send_document"]


def envelope(type_: TypeKind, **fields: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": type_, "v": PROTOCOL_VERSION}
    msg.update(fields)
    return msg


@dataclass
class NormalizedEvent:
    """OneBot 11 event reduced to a Hermes-neutral shape.

    Media is NOT included as binary payloads — all images/videos/voice/files
    are rendered as URL placeholders in ``text`` (e.g. ``[图1](https://...)``)
    so the LLM can fetch them on demand.
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
    raw: dict[str, Any] | None = None

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
    through the Hermes plugin.
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "command_name": self.command_name,
            "reject_message": self.reject_message,
            "message_id": self.message_id,
            "reply_to_message_id": self.reply_to_message_id,
            "timestamp": self.timestamp,
        }


def ready_message(onebot_connected: bool, adapter_version: str, self_id: str = "") -> dict[str, Any]:
    return envelope(
        "ready",
        onebot_connected=onebot_connected,
        adapter_version=adapter_version,
        self_id=self_id,
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


def filtered_message(event: FilteredEvent) -> dict[str, Any]:
    """OneBot event filtered by the command filter (adapter internal, not
    forwarded to the Hermes plugin as a regular ``event``).  Delivered to the
    adapter service's ``on_filtered`` callback so it can send a reject reply."""
    return envelope("filtered", **event.to_dict())


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


def parse_chat_id(chat_id: str) -> tuple[bool, int]:
    """Parse a normalized chat_id into ``(is_group, numeric_id)``.

    Supported formats:
      - ``"group:<gid>"``           → (True, <gid>)            shared session
      - ``"group:<gid>:user:<uid>"`` → (True, <gid>)            per_user session
      - ``"<uid>"``                 → (False, <uid>)           DM

    For per_user group sessions the user id is dropped (the message is still
    sent to the group, not to the individual user).  Raises ``ValueError`` on
    malformed input.
    """
    if chat_id.startswith("group:"):
        rest = chat_id[len("group:"):]
        # rest is "<gid>" or "<gid>:user:<uid>"
        gid_str = rest.split(":", 1)[0]
        return True, int(gid_str)
    return False, int(chat_id)

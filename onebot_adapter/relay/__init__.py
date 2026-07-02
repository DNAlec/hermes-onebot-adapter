"""Relay between the adapter service and the Hermes plugin."""
from onebot_adapter.relay.protocol import (
    MediaDescriptor,
    MediaPayload,
    NormalizedEvent,
    api_call_message,
    envelope,
    error_message,
    event_message,
    media_message,
    ping_message,
    pong_message,
    ready_message,
    result_message,
    send_media_message,
    send_message,
)

__all__ = [
    "MediaDescriptor",
    "MediaPayload",
    "NormalizedEvent",
    "api_call_message",
    "envelope",
    "error_message",
    "event_message",
    "media_message",
    "ping_message",
    "pong_message",
    "ready_message",
    "result_message",
    "send_media_message",
    "send_message",
]

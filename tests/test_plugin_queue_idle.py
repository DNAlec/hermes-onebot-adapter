from unittest.mock import AsyncMock, MagicMock

import pytest

from onebot_adapter.hermes_plugin import adapter as plugin_adapter


def _make_adapter() -> plugin_adapter.OneBotAdapter:
    adapter = plugin_adapter.OneBotAdapter.__new__(plugin_adapter.OneBotAdapter)
    adapter.config = MagicMock()
    adapter.config.extra = {
        "group_sessions_per_user": False,
        "thread_sessions_per_user": False,
    }
    adapter._ws = MagicMock(closed=False)
    adapter._ws.send_json = AsyncMock()
    adapter._send_idle = AsyncMock()
    return adapter


def _event(chat_id: str, text: str) -> MagicMock:
    event = MagicMock()
    event.source.chat_id = chat_id
    event.text = text
    return event


async def test_shared_group_message_fires_idle_on_processing_complete():
    adapter = _make_adapter()
    await adapter.on_processing_complete(_event("group:42", "hello"))
    adapter._send_idle.assert_awaited_once_with("group:42")


@pytest.mark.parametrize("command", ["/stop", "/new", "/reset", "/status"])
async def test_slash_command_does_not_fire_idle(command):
    adapter = _make_adapter()
    await adapter.on_processing_complete(_event("group:42", command))
    adapter._send_idle.assert_not_called()


async def test_dm_does_not_fire_idle():
    adapter = _make_adapter()
    await adapter.on_processing_complete(_event("100", "hello"))
    adapter._send_idle.assert_not_called()


async def test_pending_followup_skips_idle():
    adapter = _make_adapter()
    adapter._session_key_for_event = lambda _event: "session:42"
    adapter._pending_messages = {"session:42": object()}
    await adapter.on_processing_complete(_event("group:42", "hello"))
    adapter._send_idle.assert_not_called()


async def test_debounce_followup_skips_idle():
    adapter = _make_adapter()
    adapter._session_key_for_event = lambda _event: "session:42"
    adapter._text_debounce = {"session:42": object()}
    await adapter.on_processing_complete(_event("group:42", "hello"))
    adapter._send_idle.assert_not_called()


async def test_idle_fires_when_session_has_no_followup():
    adapter = _make_adapter()
    adapter._session_key_for_event = lambda _event: "session:42"
    adapter._pending_messages = {}
    adapter._text_debounce = {}
    await adapter.on_processing_complete(_event("group:42", "hello"))
    adapter._send_idle.assert_awaited_once_with("group:42")

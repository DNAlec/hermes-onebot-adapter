from __future__ import annotations

from types import SimpleNamespace
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


@pytest.mark.parametrize("command", ["/status", "/help"])
async def test_slash_command_does_not_fire_idle(command):
    adapter = _make_adapter()
    await adapter.on_processing_complete(_event("group:42", command))
    adapter._send_idle.assert_not_called()


@pytest.mark.parametrize("command", ["/stop", "/new", "/reset"])
async def test_interrupt_command_fires_idle(command):
    adapter = _make_adapter()
    await adapter.on_processing_complete(_event("group:42", command))
    adapter._send_idle.assert_awaited_once_with("group:42")


async def test_cancelled_turn_does_not_fire_idle():
    adapter = _make_adapter()
    await adapter.on_processing_complete(
        _event("group:42", "hello"), SimpleNamespace(name="CANCELLED")
    )
    adapter._send_idle.assert_not_called()


async def test_cancelled_interrupt_command_does_not_fire_idle():
    adapter = _make_adapter()
    await adapter.on_processing_complete(
        _event("group:42", "/stop"), SimpleNamespace(name="CANCELLED")
    )
    adapter._send_idle.assert_not_called()


async def test_interrupt_dispatch_fires_idle_when_session_free():
    """In-session /stop never hits on_processing_complete for the command."""
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter._session_key_for_event = lambda _event: "session:42"
    adapter._pending_messages = {}
    adapter._text_debounce = {}
    adapter._active_sessions = {}
    await adapter._dispatch_event(_event("group:42", "/stop"), (False, "", "", False), [])
    adapter.handle_message.assert_awaited_once()
    adapter._send_idle.assert_awaited_once_with("group:42")


async def test_interrupt_dispatch_skips_idle_when_session_stays_busy():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter._session_key_for_event = lambda _event: "session:42"
    adapter._pending_messages = {}
    adapter._text_debounce = {}
    adapter._active_sessions = {"session:42": object()}
    await adapter._dispatch_event(_event("group:42", "/stop"), (False, "", "", False), [])
    adapter._send_idle.assert_not_called()


async def test_normal_dispatch_does_not_idle_when_handle_message_returns():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter._session_key_for_event = lambda _event: "session:42"
    adapter._pending_messages = {}
    adapter._text_debounce = {}
    adapter._active_sessions = {}
    await adapter._dispatch_event(_event("group:42", "hello"), (False, "", "", False), [])
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


async def test_hook_idles_even_when_active_sessions_guard_is_still_set():
    """on_processing_complete runs before Hermes releases _active_sessions."""
    adapter = _make_adapter()
    adapter._session_key_for_event = lambda _event: "session:42"
    adapter._pending_messages = {}
    adapter._text_debounce = {}
    adapter._active_sessions = {"session:42": object()}
    await adapter.on_processing_complete(_event("group:42", "hello"))
    adapter._send_idle.assert_awaited_once_with("group:42")


async def test_interrupt_dispatch_skips_idle_when_hook_already_sent():
    """Dying turn SUCCESS idle during cancel must not be followed by a second /stop idle."""
    adapter = _make_adapter()
    adapter._session_key_for_event = lambda _event: "session:42"
    adapter._pending_messages = {}
    adapter._text_debounce = {}
    adapter._active_sessions = {}
    adapter._send_idle = plugin_adapter.OneBotAdapter._send_idle.__get__(
        adapter, plugin_adapter.OneBotAdapter
    )
    adapter._idle_sends = {}

    async def handle_message(_message):
        await adapter.on_processing_complete(_event("group:42", "hello"))

    adapter.handle_message = handle_message
    await adapter._dispatch_event(_event("group:42", "/stop"), (False, "", "", False), [])
    assert adapter._ws.send_json.await_count == 1


async def test_dying_turn_idle_during_stop_is_deferred_until_dispatch_finishes():
    """Dying-turn SUCCESS during /stop must not idle until handle_message returns.

    Otherwise the adapter dequeues the next queued group message while
    ``_dispatch_active_session_command`` still owns the session; Hermes then
    heals a stale lock, swallows that message, and the adapter busy slot
    never receives idle.
    """
    adapter = _make_adapter()
    adapter._session_key_for_event = lambda _event: "session:42"
    adapter._pending_messages = {}
    adapter._text_debounce = {}
    adapter._active_sessions = {}
    adapter._send_idle = plugin_adapter.OneBotAdapter._send_idle.__get__(
        adapter, plugin_adapter.OneBotAdapter
    )
    adapter._idle_sends = {}

    async def handle_message(_message):
        await adapter.on_processing_complete(_event("group:42", "hello"))
        assert adapter._ws.send_json.await_count == 0

    adapter.handle_message = handle_message
    await adapter._dispatch_event(_event("group:42", "/stop"), (False, "", "", False), [])
    assert adapter._ws.send_json.await_count == 1
    assert adapter._ws.send_json.await_args.args[0]["chat_id"] == "group:42"
    assert adapter._interrupt_idle_block_counts == {}


async def test_interrupt_dispatch_still_idles_when_other_group_idled():
    """Another group's idle must not suppress this group's in-session /stop idle."""
    adapter = _make_adapter()
    adapter._session_key_for_event = lambda event: "session:" + str(event.source.chat_id)
    adapter._pending_messages = {}
    adapter._text_debounce = {}
    adapter._active_sessions = {}
    adapter._send_idle = plugin_adapter.OneBotAdapter._send_idle.__get__(
        adapter, plugin_adapter.OneBotAdapter
    )
    adapter._idle_sends = {}

    async def handle_message(_message):
        await adapter.on_processing_complete(_event("group:99", "hello"))

    adapter.handle_message = handle_message
    await adapter._dispatch_event(_event("group:42", "/stop"), (False, "", "", False), [])
    frames = [call.args[0] for call in adapter._ws.send_json.await_args_list]
    assert [frame["chat_id"] for frame in frames] == ["group:99", "group:42"]


async def test_interrupt_dispatch_skips_idle_when_session_key_unknown():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter._session_key_for_event = lambda _event: ""
    adapter._pending_messages = {}
    adapter._text_debounce = {}
    adapter._active_sessions = {}
    await adapter._dispatch_event(_event("group:42", "/stop"), (False, "", "", False), [])
    adapter._send_idle.assert_not_called()

from unittest.mock import MagicMock

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
    adapter.register_post_delivery_callback = MagicMock()
    return adapter


def test_shared_group_message_registers_idle_callback(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setattr(plugin_adapter, "_BASE_AVAILABLE", True)
    monkeypatch.setattr(plugin_adapter, "SessionSource", object)
    monkeypatch.setattr(plugin_adapter, "build_session_key", lambda *args, **kwargs: "onebot:group:42")

    adapter._maybe_register_idle_callback(
        {"chat_id": "group:42", "text": "hello"},
        MagicMock(),
    )

    adapter.register_post_delivery_callback.assert_called_once()


@pytest.mark.parametrize("command", ["/stop", "/new", "/reset", "/status"])
def test_slash_command_does_not_register_idle_callback(command, monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setattr(plugin_adapter, "_BASE_AVAILABLE", True)
    monkeypatch.setattr(plugin_adapter, "SessionSource", object)
    monkeypatch.setattr(plugin_adapter, "build_session_key", MagicMock())

    adapter._maybe_register_idle_callback(
        {"chat_id": "group:42", "text": command},
        MagicMock(),
    )

    plugin_adapter.build_session_key.assert_not_called()
    adapter.register_post_delivery_callback.assert_not_called()

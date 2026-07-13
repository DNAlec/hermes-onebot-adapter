"""Tests for the plugin-bundled OneBot API tools."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from onebot_adapter.hermes_plugin.onebot_tools import (
    _TOOLS,
    TOOLSET,
    _check_admin,
    register_tools,
    set_adapter,
)


class MockAdapter:
    """Minimal adapter mock for testing tool handlers."""

    def __init__(self, is_admin: bool = False, group_id: str = "", user_id: str = ""):
        self._current_is_admin = is_admin
        self._current_group_id = group_id
        self._current_user_id = user_id
        self._api_calls: list[tuple[str, dict]] = []
        self._api_results: dict[str, Any] = {}

    async def _api_call(self, action: str, params: dict) -> dict:
        self._api_calls.append((action, params))
        if action in self._api_results:
            return self._api_results[action]
        return {"success": True, "data": {"mock": True}}


def _parse(result: str) -> dict:
    """Parse a tool result string, normalizing success/error detection."""
    return json.loads(result)


def _is_success(result: str) -> bool:
    """Check if a tool result indicates success (works with both formats)."""
    data = json.loads(result)
    if "error" in data:
        return False
    if "success" in data:
        return data["success"]
    return True


def _has_error(result: str) -> bool:
    """Check if a tool result has an error."""
    return "error" in json.loads(result)


@pytest.fixture(autouse=True)
def reset_adapter():
    """Reset the module-level adapter before each test."""
    set_adapter(None)
    yield
    set_adapter(None)


def _tool_handler(name: str):
    """Get the handler function for a tool by name."""
    for tname, handler, _, _ in _TOOLS:
        if tname == name:
            return handler
    raise KeyError(f"tool {name!r} not found")


def test_toolset_constant():
    assert TOOLSET == "onebot"


def test_tool_count():
    # 24 read-only/messaging + 14 admin = 38
    assert len(_TOOLS) == 38


def test_all_tools_have_required_fields():
    for name, handler, schema, is_admin in _TOOLS:
        assert name.startswith("onebot_")
        assert callable(handler)
        assert "name" in schema
        assert "description" in schema
        assert "parameters" in schema
        assert schema["name"] == name
        assert isinstance(is_admin, bool)


def test_register_tools_calls_ctx():
    ctx = MagicMock()
    ctx.register_tool = MagicMock()
    register_tools(ctx)
    assert ctx.register_tool.call_count == len(_TOOLS)
    # Check first call
    first_call = ctx.register_tool.call_args_list[0]
    assert first_call.kwargs["toolset"] == TOOLSET
    assert first_call.kwargs["is_async"] is True
    assert first_call.kwargs["emoji"] == "🐧"


def test_check_admin_no_adapter():
    set_adapter(None)
    assert "not initialized" in _check_admin()


def test_check_admin_not_admin():
    adapter = MockAdapter(is_admin=False)
    set_adapter(adapter)
    assert "管理员" in _check_admin()


def test_check_admin_is_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    assert _check_admin() is None


# ── Read-only tool tests ─────────────────────────────────────────────────


async def test_get_login_info():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_login_info")
    raw = await handler({})
    assert _is_success(raw) is True
    assert adapter._api_calls[0][0] == "get_login_info"


async def test_get_group_list():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_group_list")
    raw = await handler({})
    assert _is_success(raw) is True
    assert adapter._api_calls[0][0] == "get_group_list"


async def test_get_group_info():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_group_info")
    raw = await handler({"group_id": 42})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("get_group_info", {"group_id": 42, "no_cache": False})


async def test_get_group_member_info():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_group_member_info")
    raw = await handler({"group_id": 42, "user_id": 100})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("get_group_member_info", {"group_id": 42, "user_id": 100, "no_cache": False})


async def test_get_msg():
    adapter = MockAdapter(group_id="42")
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_msg")
    raw = await handler({"real_seq": 999})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("get_msg", {"real_seq": 999, "group_id": "42"})


async def test_get_forward_msg():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_forward_msg")
    raw = await handler({"message_id": "fwd123"})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("get_forward_msg", {"message_id": "fwd123"})


# ── Messaging tool tests ─────────────────────────────────────────────────


async def test_send_message_group():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_send_message")
    msg = [{"type": "text", "data": {"text": "hello"}}]
    raw = await handler({"message_type": "group", "group_id": "42", "message": msg})
    assert _is_success(raw) is True
    assert adapter._api_calls[0][0] == "send_msg"
    assert adapter._api_calls[0][1]["group_id"] == 42
    assert adapter._api_calls[0][1]["message"] == msg


async def test_send_message_private():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_send_message")
    raw = await handler({"message_type": "private", "user_id": "100", "message": []})
    assert _is_success(raw) is True
    assert adapter._api_calls[0][1]["user_id"] == 100


async def test_recall_message():
    adapter = MockAdapter(group_id="42")
    set_adapter(adapter)
    handler = _tool_handler("onebot_recall_message")
    raw = await handler({"real_seq": 555})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("delete_msg", {"real_seq": 555, "group_id": "42"})


async def test_poke():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_poke")
    raw = await handler({"user_id": 100, "group_id": 42})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("send_poke", {"user_id": 100, "group_id": 42})


async def test_get_file():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_file")
    raw = await handler({"file_id": "f_abc123"})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("get_file", {"file_id": "f_abc123"})


async def test_get_recent_contact():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_recent_contact")
    raw = await handler({})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("get_recent_contact", {"count": 10})


async def test_get_recent_contact_custom_count():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_recent_contact")
    raw = await handler({"count": 5})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("get_recent_contact", {"count": 5})


async def test_send_like():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_send_like")
    raw = await handler({"user_id": 100, "times": 3})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("send_like", {"user_id": 100, "times": 3})


async def test_send_like_default_times():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_send_like")
    raw = await handler({"user_id": 100})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("send_like", {"user_id": 100, "times": 1})


async def test_get_friends_with_category():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_friends_with_category")
    raw = await handler({})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("get_friends_with_category", {})


async def test_get_profile_like():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_profile_like")
    raw = await handler({})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("get_profile_like", {})


async def test_fetch_custom_face():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_fetch_custom_face")
    raw = await handler({})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("fetch_custom_face", {"count": 48})


async def test_fetch_custom_face_custom_count():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_fetch_custom_face")
    raw = await handler({"count": 20})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("fetch_custom_face", {"count": 20})


async def test_send_forward_msg_group():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_send_forward_msg")
    nodes = [{"type": "node", "data": {"name": "test", "content": [{"type": "text", "data": {"text": "hi"}}]}}]
    raw = await handler({"message_type": "group", "group_id": "42", "messages": nodes})
    assert _is_success(raw) is True
    action, params = adapter._api_calls[0]
    assert action == "send_forward_msg"
    assert params["message_type"] == "group"
    assert params["group_id"] == 42
    assert params["messages"] == nodes


async def test_send_forward_msg_private():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_send_forward_msg")
    nodes = [{"type": "node", "data": {"name": "test", "content": [{"type": "text", "data": {"text": "hi"}}]}}]
    raw = await handler({"message_type": "private", "user_id": "100", "messages": nodes})
    assert _is_success(raw) is True
    action, params = adapter._api_calls[0]
    assert action == "send_forward_msg"
    assert params["message_type"] == "private"
    assert params["user_id"] == 100
    assert params["messages"] == nodes


async def test_forward_single_msg_group_context():
    """群聊上下文:转发到当前群,action=forward_group_single_msg。"""
    adapter = MockAdapter(group_id="42")
    set_adapter(adapter)
    handler = _tool_handler("onebot_forward_single_msg")
    raw = await handler({"real_seq": 999})
    assert _is_success(raw) is True
    action, params = adapter._api_calls[0]
    assert action == "forward_group_single_msg"
    assert params["real_seq"] == 999
    assert params["group_id"] == 42


async def test_forward_single_msg_dm_context():
    """私聊上下文:转发到当前好友,action=forward_friend_single_msg。"""
    adapter = MockAdapter(user_id="10001000")
    set_adapter(adapter)
    handler = _tool_handler("onebot_forward_single_msg")
    raw = await handler({"real_seq": 888})
    assert _is_success(raw) is True
    action, params = adapter._api_calls[0]
    assert action == "forward_friend_single_msg"
    assert params["real_seq"] == 888
    assert params["user_id"] == 10001000


async def test_forward_single_msg_no_context_explicit_group_id():
    """无当前会话上下文:用 args 中的 group_id 决定目标。"""
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_forward_single_msg")
    raw = await handler({"real_seq": 777, "group_id": 99})
    assert _is_success(raw) is True
    action, params = adapter._api_calls[0]
    assert action == "forward_group_single_msg"
    assert params["real_seq"] == 777
    assert params["group_id"] == 99


async def test_forward_single_msg_no_target_error():
    """无当前会话上下文且无 group_id/user_id:返回错误。"""
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_forward_single_msg")
    raw = await handler({"real_seq": 666})
    assert _has_error(raw) is True
    assert len(adapter._api_calls) == 0


# ── Admin tool tests (require admin) ─────────────────────────────────────


async def test_kick_group_member_no_admin():
    adapter = MockAdapter(is_admin=False)
    set_adapter(adapter)
    handler = _tool_handler("onebot_kick_group_member")
    raw = await handler({"group_id": 42, "user_id": 100})
    assert _has_error(raw) is True
    assert "管理员" in _parse(raw).get("error", "")
    assert len(adapter._api_calls) == 0


async def test_kick_group_member_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_kick_group_member")
    raw = await handler({"group_id": 42, "user_id": 100})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("set_group_kick", {"group_id": 42, "user_id": 100, "reject_add_request": False})


async def test_mute_group_member_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_mute_group_member")
    raw = await handler({"group_id": 42, "user_id": 100, "duration": 3600})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("set_group_ban", {"group_id": 42, "user_id": 100, "duration": 3600})


async def test_mute_group_whole_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_mute_group_whole")
    raw = await handler({"group_id": 42, "enable": True})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("set_group_whole_ban", {"group_id": 42, "enable": True})


async def test_set_group_card_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_set_group_card")
    raw = await handler({"group_id": 42, "user_id": 100, "card": "新名片"})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("set_group_card", {"group_id": 42, "user_id": 100, "card": "新名片"})


async def test_leave_group_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_leave_group")
    raw = await handler({"group_id": 42})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("set_group_leave", {"group_id": 42})


async def test_handle_group_request_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_handle_group_request")
    raw = await handler({"flag": "flag123", "approve": True})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == (
        "set_group_add_request",
        {"flag": "flag123", "sub_type": "add", "approve": True, "reason": ""},
    )


async def test_set_group_special_title_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_set_group_special_title")
    raw = await handler({"group_id": 42, "user_id": 100, "special_title": "龙王"})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == (
        "set_group_special_title",
        {"group_id": 42, "user_id": 100, "special_title": "龙王"},
    )


async def test_set_online_status_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_set_online_status")
    raw = await handler({"status": 11, "ext_status": 0, "battery_status": 80})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == (
        "set_online_status",
        {"status": 11, "ext_status": 0, "battery_status": 80},
    )


async def test_set_signature_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_set_signature")
    raw = await handler({"longNick": "新签名"})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("set_self_longnick", {"longNick": "新签名"})


async def test_set_avatar_admin():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    handler = _tool_handler("onebot_set_avatar")
    raw = await handler({"file": "https://example.com/avatar.png"})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("set_qq_avatar", {"file": "https://example.com/avatar.png"})


async def test_all_admin_tools_blocked_without_admin():
    """Every admin tool should return error when user is not admin."""
    adapter = MockAdapter(is_admin=False)
    set_adapter(adapter)
    admin_tools = [(name, handler) for name, handler, _, is_admin in _TOOLS if is_admin]
    assert len(admin_tools) == 14
    for name, handler in admin_tools:
        raw = await handler({
            "group_id": 1, "user_id": 2, "flag": "x", "group_name": "n", "card": "c",
            "special_title": "t", "status": 1, "ext_status": 0, "longNick": "sig",
            "file": "/tmp/x.png",
        })
        assert _has_error(raw) is True, f"{name} should be blocked for non-admin"
        assert len(adapter._api_calls) == 0, f"{name} should not have called API"


# ── Error handling ───────────────────────────────────────────────────────


async def test_api_call_error_returns_tool_error():
    adapter = MockAdapter()
    adapter._api_call = AsyncMock(side_effect=RuntimeError("connection refused"))
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_group_list")
    raw = await handler({})
    assert _has_error(raw) is True
    assert "connection refused" in _parse(raw).get("error", "")


async def test_no_adapter_returns_error():
    set_adapter(None)
    handler = _tool_handler("onebot_get_group_list")
    raw = await handler({})
    assert _has_error(raw) is True


# ── Tool group_id propagation tests ──────────────────────────────────────


async def test_get_msg_passes_real_seq_and_group_id():
    """工具传 real_seq + group_id 给适配器(转换在适配器侧 _handle_api_call)。"""
    adapter = MockAdapter(group_id="42")
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_msg")
    raw = await handler({"real_seq": 999})
    assert _is_success(raw) is True
    action, params = adapter._api_calls[0]
    assert action == "get_msg"
    assert params["real_seq"] == 999
    assert params["group_id"] == "42"


async def test_recall_passes_real_seq_and_group_id():
    adapter = MockAdapter(group_id="42")
    set_adapter(adapter)
    handler = _tool_handler("onebot_recall_message")
    raw = await handler({"real_seq": 555})
    assert _is_success(raw) is True
    action, params = adapter._api_calls[0]
    assert action == "delete_msg"
    assert params["real_seq"] == 555
    assert params["group_id"] == "42"


async def test_get_msg_no_group_id_for_dm():
    """私聊场景:_current_group_id 为空,改传 user_id 给适配器侧做 SeqMap 查询。"""
    adapter = MockAdapter(group_id="", user_id="10001000")
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_msg")
    raw = await handler({"real_seq": 888})
    assert _is_success(raw) is True
    _, params = adapter._api_calls[0]
    assert params["user_id"] == "10001000"
    assert "group_id" not in params

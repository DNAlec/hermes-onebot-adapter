"""Tests for the plugin-bundled OneBot API tools."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from onebot_adapter.hermes_plugin import onebot_tools
from onebot_adapter.hermes_plugin.adapter import _msg_context
from onebot_adapter.hermes_plugin.onebot_tools import (
    _TOOLS,
    TOOLSET,
    register_tools,
    set_adapter,
)


class MockAdapter:
    """Minimal adapter mock for testing tool handlers."""

    def __init__(
        self,
        is_admin: bool = False,
        group_id: str = "",
        user_id: str = "",
        is_global_admin: bool = False,
    ):
        _msg_context.set((is_admin, group_id, user_id, is_global_admin))
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
    _msg_context.set(None)
    yield
    set_adapter(None)
    _msg_context.set(None)


def _tool_handler(name: str):
    """Get the handler function for a tool by name."""
    for tname, handler, _ in _TOOLS:
        if tname == name:
            return handler
    raise KeyError(f"tool {name!r} not found")


def _tool_schema(name: str):
    for tname, _, schema in _TOOLS:
        if tname == name:
            return schema
    raise KeyError(f"tool {name!r} not found")


def test_toolset_constant():
    assert TOOLSET == "onebot"


def test_tool_count():
    # Keep the canonical plugin and HTTP automation catalogs at 100 tools.
    assert len(_TOOLS) == 100


def test_new_canonical_tool_names_are_complete_and_exclude_aliases():
    expected = {
        "get_essence_msg_list", "set_essence_msg", "delete_essence_msg", "get_group_notice",
        "send_group_notice", "del_group_notice", "set_group_sign", "get_group_signed_list",
        "get_qun_album_list", "get_group_album_media_list", "upload_image_to_qun_album",
        "set_group_album_media_like", "cancel_group_album_media_like", "do_group_album_comment",
        "del_group_album_media", "set_group_todo", "complete_group_todo", "cancel_group_todo",
        "set_friend_remark", "get_unidirectional_friend_list", "set_qq_profile", "nc_get_user_status",
        "get_doubt_friends_add_request", "get_group_ignore_add_request", "fetch_custom_face_detail",
        "add_custom_face", "delete_custom_face", "set_custom_face_desc", "set_group_portrait",
        "set_group_remark", "get_group_ignored_notifies", "get_group_shut_list", "get_group_info_ex",
        "get_group_detail_info", "create_collection", "get_collection_list", "fetch_emoji_like",
        "get_emoji_likes", "get_group_file_system_info", "get_group_root_files",
        "get_group_files_by_folder", "get_group_file_url", "delete_group_file",
        "create_group_file_folder", "delete_group_folder", "move_group_file", "rename_group_file",
        "trans_group_file", "create_flash_task", "send_flash_msg", "get_share_link",
        "get_fileset_id", "get_fileset_info", "get_flash_file_list", "get_flash_file_url",
        "download_fileset", "get_group_system_msg", "get_group_honor_info",
        "set_group_add_option",
    }
    names = {name for name, _, _ in _TOOLS}
    assert {f"onebot_{name}" for name in expected} <= names
    assert not {
        "onebot_send_group_sign", "onebot_set_group_search", "onebot_get_online_clients",
        "onebot_set_doubt_friends_add_request",
    } & names


def test_message_mutation_schemas_expose_real_seq_not_message_id():
    names = {
        "onebot_set_essence_msg", "onebot_delete_essence_msg", "onebot_set_group_todo",
        "onebot_complete_group_todo", "onebot_cancel_group_todo", "onebot_fetch_emoji_like",
        "onebot_get_emoji_likes",
    }
    for name in names:
        parameters = _tool_schema(name)["parameters"]
        assert "real_seq" in parameters["properties"]
        assert "message_id" not in parameters["properties"]
        assert "real_seq" in parameters["required"]


async def test_bot_blacklist_tools_use_adapter_local_actions():
    adapter = MockAdapter(group_id="42", user_id="100")
    set_adapter(adapter)
    get_result = await _tool_handler("onebot_get_bot_blacklist")({"scope": "group", "group_id": "42"})
    assert _is_success(get_result)
    assert adapter._api_calls[0] == (
        "adapter_get_bot_blacklist",
        {"scope": "group", "group_id": "42"},
    )

    edit_result = await _tool_handler("onebot_edit_bot_blacklist")({
        "action": "set", "scope": "group", "group_id": "42", "user_id": "200",
        "duration_seconds": 3600, "reason": "刷屏",
    })
    assert _is_success(edit_result)
    assert adapter._api_calls[1] == (
        "adapter_edit_bot_blacklist",
        {
            "operation": "set", "scope": "group", "group_id": "42", "user_id": "200",
            "duration_seconds": 3600, "reason": "刷屏", "created_by_user_id": "100",
        },
    )


async def test_bot_blacklist_edit_requires_group_and_set_fields():
    adapter = MockAdapter(user_id="100")
    set_adapter(adapter)
    handler = _tool_handler("onebot_edit_bot_blacklist")
    assert _has_error(await handler({"action": "set", "scope": "group", "user_id": "200"}))
    assert _has_error(await handler({"action": "set", "scope": "dm", "user_id": "200"}))
    assert adapter._api_calls == []


def test_all_tools_have_required_fields():
    for name, handler, schema in _TOOLS:
        assert name.startswith("onebot_")
        assert callable(handler)
        assert "name" in schema
        assert "description" in schema
        assert "parameters" in schema
        assert schema["name"] == name


def test_register_tools_calls_ctx():
    ctx = MagicMock()
    ctx.register_tool = MagicMock()
    register_tools(ctx)
    assert ctx.register_tool.call_count == len(_TOOLS) - 12
    # Check first call
    first_call = ctx.register_tool.call_args_list[0]
    assert first_call.kwargs["toolset"] == TOOLSET
    assert first_call.kwargs["is_async"] is True
    assert first_call.kwargs["emoji"] == "🐧"


def test_default_hidden_tools_are_not_registered():
    ctx = MagicMock()
    register_tools(ctx)
    names = {call.kwargs["name"] for call in ctx.register_tool.call_args_list}
    assert {
        "onebot_mark_msg_as_read",
        "onebot_get_recent_contact",
        "onebot_set_friend_remark",
        "onebot_set_group_remark",
        "onebot_create_flash_task",
        "onebot_send_flash_msg",
        "onebot_get_share_link",
        "onebot_get_fileset_id",
        "onebot_get_fileset_info",
        "onebot_get_flash_file_list",
        "onebot_get_flash_file_url",
        "onebot_download_fileset",
    }.isdisjoint(names)


def test_default_hidden_tool_can_be_explicitly_registered(monkeypatch):
    monkeypatch.setattr(
        onebot_tools,
        "_load_tool_policies",
        lambda: {"onebot_mark_msg_as_read": {"registered": True}},
    )
    ctx = MagicMock()
    register_tools(ctx)
    names = {call.kwargs["name"] for call in ctx.register_tool.call_args_list}
    assert "onebot_mark_msg_as_read" in names


def test_register_tools_skips_hidden_policy(monkeypatch):
    monkeypatch.setattr(
        onebot_tools,
        "_load_tool_policies",
        lambda: {"onebot_get_login_info": {"registered": False}},
    )
    ctx = MagicMock()
    register_tools(ctx)
    names = {call.kwargs["name"] for call in ctx.register_tool.call_args_list}
    assert "onebot_get_login_info" not in names
    assert len(names) == 87


def test_descriptions_only_name_qq_group_permission_requirements():
    descriptions = {name: schema["description"] for name, _, schema in _TOOLS}
    assert all("需管理员权限" not in description for description in descriptions.values())
    assert "需群聊管理员权限" in descriptions["onebot_kick_group_member"]
    assert "需群聊管理员权限" in descriptions["onebot_set_group_portrait"]
    assert "管理员权限" not in descriptions["onebot_set_friend_remark"]
    assert "管理员权限" not in descriptions["onebot_set_group_remark"]


def test_exact_default_admin_tool_set():
    expected = set(onebot_tools._DEFAULT_ADMIN_TOOL_NAMES)
    actual = {name for name, _, _ in _TOOLS if onebot_tools.default_tool_permission(name) == "admin"}
    assert actual == expected
    assert all(
        onebot_tools.default_tool_permission(name) == "everyone"
        for name, _, _ in _TOOLS
        if name not in expected
    )


def _registered_handler(ctx: MagicMock, name: str):
    return next(call.kwargs["handler"] for call in ctx.register_tool.call_args_list if call.kwargs["name"] == name)


async def test_everyone_policy_can_explicitly_downgrade_admin_tool(monkeypatch):
    monkeypatch.setattr(
        onebot_tools,
        "_load_tool_policies",
        lambda: {"onebot_kick_group_member": {"permission": "everyone"}},
    )
    ctx = MagicMock()
    register_tools(ctx)
    adapter = MockAdapter(is_admin=False, group_id="42")
    set_adapter(adapter)

    raw = await _registered_handler(ctx, "onebot_kick_group_member")({
        "group_id": 42,
        "user_id": 100,
        "reject_add_request": False,
    })

    assert _is_success(raw)
    assert adapter._api_calls[0][0] == "set_group_kick"


async def test_group_admin_policy_is_limited_to_current_group(monkeypatch):
    monkeypatch.setattr(onebot_tools, "_load_tool_policies", lambda: {})
    ctx = MagicMock()
    register_tools(ctx)
    adapter = MockAdapter(is_admin=True, group_id="42")
    set_adapter(adapter)
    handler = _registered_handler(ctx, "onebot_kick_group_member")

    denied = await handler({"group_id": 43, "user_id": 100, "reject_add_request": False})
    assert "其他群" in _parse(denied)["error"]
    assert adapter._api_calls == []

    allowed = await handler({"group_id": 42, "user_id": 100, "reject_add_request": False})
    assert _is_success(allowed)
    assert adapter._api_calls[0][0] == "set_group_kick"


async def test_group_admin_cannot_call_account_admin_tool(monkeypatch):
    monkeypatch.setattr(onebot_tools, "_load_tool_policies", lambda: {})
    ctx = MagicMock()
    register_tools(ctx)
    adapter = MockAdapter(is_admin=True, group_id="42")
    set_adapter(adapter)

    raw = await _registered_handler(ctx, "onebot_set_avatar")({"file": "https://example.com/a.png"})

    assert "全局管理员" in _parse(raw)["error"]
    assert adapter._api_calls == []


async def test_group_admin_cannot_send_admin_flash_to_private_user(monkeypatch):
    monkeypatch.setattr(
        onebot_tools,
        "_load_tool_policies",
        lambda: {"onebot_send_flash_msg": {"registered": True, "permission": "admin"}},
    )
    ctx = MagicMock()
    register_tools(ctx)
    adapter = MockAdapter(is_admin=True, group_id="42")
    set_adapter(adapter)

    raw = await _registered_handler(ctx, "onebot_send_flash_msg")({
        "fileset_id": "f1", "user_id": 100,
    })

    assert "全局管理员" in _parse(raw)["error"]
    assert adapter._api_calls == []


async def test_global_admin_can_call_account_admin_tool(monkeypatch):
    monkeypatch.setattr(onebot_tools, "_load_tool_policies", lambda: {})
    ctx = MagicMock()
    register_tools(ctx)
    adapter = MockAdapter(is_admin=True, group_id="42", is_global_admin=True)
    set_adapter(adapter)

    raw = await _registered_handler(ctx, "onebot_set_avatar")({"file": "https://example.com/a.png"})

    assert _is_success(raw)
    assert adapter._api_calls[0][0] == "set_qq_avatar"


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


async def test_send_message_defaults_to_matching_current_context():
    group_adapter = MockAdapter(group_id="42", user_id="100")
    set_adapter(group_adapter)
    handler = _tool_handler("onebot_send_message")
    raw = await handler({"message_type": "group", "message": []})
    assert _is_success(raw) is True
    assert group_adapter._api_calls[0][1]["group_id"] == 42

    dm_adapter = MockAdapter(user_id="100")
    set_adapter(dm_adapter)
    raw = await handler({"message_type": "private", "message": []})
    assert _is_success(raw) is True
    assert dm_adapter._api_calls[0][1]["user_id"] == 100


async def test_send_message_rejects_missing_or_conflicting_target():
    adapter = MockAdapter(group_id="42", user_id="100")
    set_adapter(adapter)
    handler = _tool_handler("onebot_send_message")
    assert _has_error(await handler({"message_type": "private", "message": []}))
    assert _has_error(await handler({
        "message_type": "group", "group_id": "42", "user_id": "100", "message": [],
    }))
    assert adapter._api_calls == []


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


async def test_poke_defaults_to_current_group():
    adapter = MockAdapter(group_id="42")
    set_adapter(adapter)
    handler = _tool_handler("onebot_poke")
    raw = await handler({"user_id": 100})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("send_poke", {"user_id": 100, "group_id": 42})


async def test_poke_explicit_group_overrides_current_group():
    adapter = MockAdapter(group_id="42")
    set_adapter(adapter)
    handler = _tool_handler("onebot_poke")
    raw = await handler({"user_id": 100, "group_id": 99})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("send_poke", {"user_id": 100, "group_id": 99})


async def test_poke_without_group_context_stays_private():
    adapter = MockAdapter(user_id="100")
    set_adapter(adapter)
    handler = _tool_handler("onebot_poke")
    raw = await handler({"user_id": 100})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("send_poke", {"user_id": 100})


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


async def test_send_forward_msg_defaults_to_current_group():
    adapter = MockAdapter(group_id="42", user_id="100")
    set_adapter(adapter)
    handler = _tool_handler("onebot_send_forward_msg")
    raw = await handler({"message_type": "group", "messages": []})
    assert _is_success(raw) is True
    assert adapter._api_calls[0][1]["group_id"] == 42


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


async def test_forward_single_msg_rejects_conflicting_targets():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_forward_single_msg")
    raw = await handler({"real_seq": 666, "group_id": 42, "user_id": 100})
    assert _has_error(raw) is True
    assert adapter._api_calls == []


async def test_upload_file_defaults_to_matching_current_context():
    group_adapter = MockAdapter(group_id="42", user_id="100")
    set_adapter(group_adapter)
    handler = _tool_handler("onebot_upload_file")
    raw = await handler({"message_type": "group", "file": "/tmp/group.txt"})
    assert _is_success(raw) is True
    assert group_adapter._api_calls[0] == (
        "upload_group_file",
        {"group_id": 42, "file": "/tmp/group.txt", "name": "group.txt"},
    )

    dm_adapter = MockAdapter(user_id="100")
    set_adapter(dm_adapter)
    raw = await handler({"message_type": "private", "file": "/tmp/private.txt"})
    assert _is_success(raw) is True
    assert dm_adapter._api_calls[0] == (
        "upload_private_file",
        {"user_id": 100, "file": "/tmp/private.txt", "name": "private.txt"},
    )


async def test_mark_msg_as_read_requires_explicit_scope():
    adapter = MockAdapter(group_id="42")
    set_adapter(adapter)
    handler = _tool_handler("onebot_mark_msg_as_read")
    assert _has_error(await handler({}))
    assert _has_error(await handler({"real_seq": 123, "all": True}))
    assert adapter._api_calls == []

    raw = await handler({"all": True})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == ("mark_msg_as_read", {"message_id": 0, "group_id": "42"})


async def test_essence_and_todo_handlers_pass_real_seq_with_group_context():
    adapter = MockAdapter(is_admin=True, group_id="42")
    set_adapter(adapter)
    assert _is_success(await _tool_handler("onebot_set_essence_msg")({"real_seq": 101}))
    assert _is_success(await _tool_handler("onebot_complete_group_todo")({"group_id": 99, "real_seq": 202}))
    assert adapter._api_calls == [
        ("set_essence_msg", {"real_seq": 101, "group_id": 42}),
        ("complete_group_todo", {"real_seq": 202, "group_id": 99}),
    ]


async def test_emoji_handlers_map_canonical_schema_to_napcat_fields():
    adapter = MockAdapter(group_id="42")
    set_adapter(adapter)
    raw = await _tool_handler("onebot_fetch_emoji_like")({
        "real_seq": 303, "emoji_id": "66", "emoji_type": "1", "count": 5, "cookie": "next",
    })
    assert _is_success(raw)
    assert adapter._api_calls[0] == (
        "fetch_emoji_like",
        {
            "real_seq": 303, "group_id": 42, "emojiId": "66", "emojiType": "1",
            "count": 5, "cookie": "next",
        },
    )


async def test_group_notice_defaults_and_group_file_mapping():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    assert _is_success(await _tool_handler("onebot_send_group_notice")({"group_id": 42, "content": "公告"}))
    assert adapter._api_calls[0] == (
        "_send_group_notice",
        {
            "group_id": 42, "content": "公告", "pinned": 0, "type": 1, "confirm_required": 1,
            "is_show_edit_card": 0, "tip_window_type": 0,
        },
    )
    assert _is_success(await _tool_handler("onebot_move_group_file")({
        "group_id": 42, "file_id": "file", "current_parent_directory": "/", "target_parent_directory": "/dst",
    }))
    assert adapter._api_calls[1] == (
        "move_group_file",
        {
            "group_id": 42, "file_id": "file", "current_parent_directory": "/",
            "target_parent_directory": "/dst",
        },
    )


# ── Admin tool tests (require admin) ─────────────────────────────────────


async def test_kick_group_member_no_admin(monkeypatch):
    monkeypatch.setattr(onebot_tools, "_load_tool_policies", lambda: {})
    ctx = MagicMock()
    register_tools(ctx)
    adapter = MockAdapter(is_admin=False, group_id="42")
    set_adapter(adapter)
    raw = await _registered_handler(ctx, "onebot_kick_group_member")({"group_id": 42, "user_id": 100})
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
    raw = await handler({"flag": "flag123", "sub_type": "add", "approve": True})
    assert _is_success(raw) is True
    assert adapter._api_calls[0] == (
        "set_group_add_request",
        {"flag": "flag123", "sub_type": "add", "approve": True, "reason": ""},
    )


def test_risky_admin_intent_parameters_are_required():
    expected = {
        "onebot_mute_group_member": {"duration"},
        "onebot_mute_group_whole": {"enable"},
        "onebot_set_group_admin": {"enable"},
        "onebot_set_group_card": {"card"},
        "onebot_handle_group_request": {"sub_type", "approve"},
        "onebot_handle_friend_request": {"approve"},
        "onebot_set_group_special_title": {"special_title"},
    }
    for name, required_fields in expected.items():
        required = set(_tool_schema(name)["parameters"]["required"])
        assert required_fields <= required


async def test_risky_admin_handlers_reject_missing_intent():
    adapter = MockAdapter(is_admin=True)
    set_adapter(adapter)
    calls = [
        ("onebot_mute_group_member", {"group_id": 42, "user_id": 100}),
        ("onebot_mute_group_whole", {"group_id": 42}),
        ("onebot_set_group_admin", {"group_id": 42, "user_id": 100}),
        ("onebot_set_group_card", {"group_id": 42, "user_id": 100}),
        ("onebot_handle_group_request", {"flag": "x", "sub_type": "add"}),
        ("onebot_handle_friend_request", {"flag": "x"}),
        ("onebot_set_group_special_title", {"group_id": 42, "user_id": 100}),
    ]
    for name, args in calls:
        assert _has_error(await _tool_handler(name)(args)), name
    assert adapter._api_calls == []


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


async def test_default_admin_tools_blocked_without_admin(monkeypatch):
    """Default-admin tools are denied by the registration wrapper."""
    monkeypatch.setattr(onebot_tools, "_load_tool_policies", lambda: {})
    ctx = MagicMock()
    register_tools(ctx)
    adapter = MockAdapter(is_admin=False)
    set_adapter(adapter)
    registered = {call.kwargs["name"] for call in ctx.register_tool.call_args_list}
    for name in onebot_tools._DEFAULT_ADMIN_TOOL_NAMES:
        if name not in registered:
            continue
        raw = await _registered_handler(ctx, name)({
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


async def test_api_call_failure_envelope_returns_tool_error():
    adapter = MockAdapter()
    adapter._api_results["upload_group_file"] = {
        "success": False,
        "error": "OneBot API error upload_group_file: retcode=100 msg=识别URL失败",
    }
    set_adapter(adapter)
    handler = _tool_handler("onebot_upload_file")
    raw = await handler({
        "message_type": "group",
        "group_id": 42,
        "file": "/home/alec/Downloads/archive.7z",
    })
    parsed = _parse(raw)
    assert _has_error(raw) is True
    assert "识别URL失败" in parsed["error"]
    assert parsed.get("uploaded") is not True


async def test_api_call_success_envelope_is_unwrapped_for_tools():
    adapter = MockAdapter()
    adapter._api_results["get_group_info"] = {
        "success": True,
        "data": {"group_id": 42, "group_name": "Test"},
    }
    set_adapter(adapter)
    raw = await _tool_handler("onebot_get_group_info")({"group_id": 42})
    assert _parse(raw) == {"group_id": 42, "group_name": "Test"}


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


# ── Flash transfer / filesets / group system actions ─────────────────────


async def test_create_flash_task_passes_files():
    adapter = MockAdapter()
    adapter._api_results["create_flash_task"] = {
        "success": True,
        "data": {
            "result": 0,
            "createFlashTransferResult": {
                "fileSetId": "f1",
                "shareLink": "https://example.com/share",
                "expireTime": "123",
            },
        },
    }
    set_adapter(adapter)
    raw = await _tool_handler("onebot_create_flash_task")({
        "files": "/tmp/a.zip",
        "name": "a",
    })
    assert _is_success(raw)
    assert adapter._api_calls[0] == (
        "create_flash_task",
        {"files": "/tmp/a.zip", "name": "a"},
    )
    result = _parse(raw)
    assert result["fileset_id"] == "f1"
    assert result["share_link"] == "https://example.com/share"


async def test_send_flash_msg_requires_exactly_one_target():
    adapter = MockAdapter()
    set_adapter(adapter)
    assert _has_error(await _tool_handler("onebot_send_flash_msg")({"fileset_id": "f1"}))
    assert _has_error(await _tool_handler("onebot_send_flash_msg")({
        "fileset_id": "f1", "group_id": 1, "user_id": 2,
    }))

    raw = await _tool_handler("onebot_send_flash_msg")({"fileset_id": "f1", "group_id": 42})
    assert _is_success(raw)
    assert adapter._api_calls[0] == (
        "send_flash_msg",
        {"fileset_id": "f1", "group_id": 42},
    )
    assert _has_error(await _tool_handler("onebot_send_flash_msg")({
        "fileset_id": "f1", "group_id": 0,
    }))


async def test_get_share_link_and_fileset_id():
    adapter = MockAdapter()
    set_adapter(adapter)
    assert _is_success(await _tool_handler("onebot_get_share_link")({"fileset_id": "f1"}))
    assert adapter._api_calls[0] == ("get_share_link", {"fileset_id": "f1"})
    assert _is_success(await _tool_handler("onebot_get_fileset_id")({"share_code": "abc123"}))
    assert adapter._api_calls[1] == ("get_fileset_id", {"share_code": "abc123"})


async def test_flash_listing_and_download_tools():
    adapter = MockAdapter()
    set_adapter(adapter)
    assert _is_success(await _tool_handler("onebot_get_fileset_info")({"fileset_id": "f1"}))
    assert _is_success(await _tool_handler("onebot_get_flash_file_list")({"fileset_id": "f1"}))
    assert _is_success(await _tool_handler("onebot_get_flash_file_url")({
        "fileset_id": "f1", "file_name": "x.zip",
    }))
    assert _is_success(await _tool_handler("onebot_download_fileset")({"fileset_id": "f1"}))
    assert [call[0] for call in adapter._api_calls] == [
        "get_fileset_info", "get_flash_file_list", "get_flash_file_url", "download_fileset",
    ]


async def test_flash_file_url_requires_valid_selector():
    adapter = MockAdapter()
    set_adapter(adapter)
    handler = _tool_handler("onebot_get_flash_file_url")

    assert _has_error(await handler({"fileset_id": "f1"}))
    assert _has_error(await handler({"fileset_id": "f1", "file_index": -1}))
    assert adapter._api_calls == []


async def test_flash_inner_failure_returns_tool_error():
    adapter = MockAdapter()
    adapter._api_results["get_flash_file_url"] = {
        "success": True,
        "data": {"result": -1, "errMsg": "未找到对应文件", "transferUrl": ""},
    }
    set_adapter(adapter)

    raw = await _tool_handler("onebot_get_flash_file_url")({
        "fileset_id": "f1", "file_name": "missing.zip",
    })

    assert _has_error(raw)
    assert "未找到对应文件" in _parse(raw)["error"]


async def test_get_group_system_msg_passes_count():
    adapter = MockAdapter()
    set_adapter(adapter)
    raw = await _tool_handler("onebot_get_group_system_msg")({})
    assert _is_success(raw)
    assert adapter._api_calls[0] == ("get_group_system_msg", {"count": 50})


async def test_get_group_honor_info_passes_type():
    adapter = MockAdapter()
    set_adapter(adapter)
    raw = await _tool_handler("onebot_get_group_honor_info")({"group_id": 42, "type": "talkative"})
    assert _is_success(raw)
    assert adapter._api_calls[0] == ("get_group_honor_info", {"group_id": 42, "type": "talkative"})
    assert _has_error(await _tool_handler("onebot_get_group_honor_info")({
        "group_id": 42, "type": "current_talkative",
    }))


async def test_set_group_add_option_requires_admin(monkeypatch):
    monkeypatch.setattr(onebot_tools, "_load_tool_policies", lambda: {})
    ctx = MagicMock()
    register_tools(ctx)
    adapter = MockAdapter(is_admin=False, group_id="42")
    set_adapter(adapter)
    raw = await _registered_handler(ctx, "onebot_set_group_add_option")({"group_id": 42, "add_type": 1})
    assert _has_error(raw)
    assert adapter._api_calls == []

    admin_adapter = MockAdapter(is_admin=True, group_id="42")
    set_adapter(admin_adapter)
    raw = await _registered_handler(ctx, "onebot_set_group_add_option")({
        "group_id": 42, "add_type": 4, "group_question": "q", "group_answer": "a",
    })
    assert _is_success(raw)
    assert admin_adapter._api_calls[0] == (
        "set_group_add_option",
        {"group_id": 42, "add_type": 4, "group_question": "q", "group_answer": "a"},
    )


async def test_set_group_add_option_validates_question_fields():
    adapter = MockAdapter(is_admin=True, group_id="42")
    set_adapter(adapter)
    handler = _tool_handler("onebot_set_group_add_option")

    assert _has_error(await handler({"group_id": 42, "add_type": 6}))
    assert _has_error(await handler({"group_id": 42, "add_type": 4, "group_question": "q"}))
    assert _has_error(await handler({"group_id": 42, "add_type": 5}))
    assert adapter._api_calls == []

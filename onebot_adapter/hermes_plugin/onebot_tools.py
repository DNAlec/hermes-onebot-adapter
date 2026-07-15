"""Plugin-bundled OneBot API tools for Hermes Agent.

These tools let the LLM call the OneBot 11 HTTP API directly (send messages
to arbitrary groups, manage group members, fetch histories, etc.) by routing
through the adapter service's WS ``api_call`` channel.

Registration is done via ``ctx.register_tool(...)`` at plugin load time —
no dependency on the host's ``tools/qq_tool.py``.

Admin gating: tools that mutate group state (kick, mute, ban, etc.) check
``_current_is_admin`` which is set per-message by the adapter.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# ── Runtime bridge to the adapter's WS api_call channel ──────────────────

_adapter: Any = None  # OneBotAdapter instance (set by register_tools)


def set_adapter(adapter: Any) -> None:
    """Inject the OneBotAdapter instance so tools can call api_call."""
    global _adapter
    _adapter = adapter


# Per-message context (admin, group_id, user_id) set by _dispatch_event.
# Imported from adapter.py; the contextvar is defined there because
# _dispatch_event sets it before calling handle_message.
try:
    from .adapter import _msg_context
except ImportError:
    import contextvars as _contextvars_mod
    _msg_context = _contextvars_mod.ContextVar("_msg_context", default=None)


def _api_call(action: str, **params: Any) -> Any:
    """Return an awaitable that calls the adapter's _api_call method."""
    if _adapter is None:
        raise RuntimeError("OneBot adapter not initialized")
    # Convert kwargs to a params dict, dropping None values
    clean = {k: v for k, v in params.items() if v is not None}
    return _adapter._api_call(action, clean)


# ── Schema helpers ───────────────────────────────────────────────────────


def _schema(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": desc,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": required or [],
        },
    }


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _int(desc: str) -> dict:
    return {"type": "integer", "description": desc}


def _bool(desc: str) -> dict:
    return {"type": "boolean", "description": desc}


def _array(desc: str) -> dict:
    return {"type": "array", "description": desc, "items": {"type": "object"}}


# ── Tool result/error formatting ─────────────────────────────────────────

try:
    from tools.registry import tool_error, tool_result
except ImportError:
    def tool_result(data: Any) -> str:
        return json.dumps({"success": True, "data": data}, ensure_ascii=False, default=str)

    def tool_error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


# ── Admin gating ─────────────────────────────────────────────────────────


def _check_admin() -> str | None:
    """Return an error string if the current user is not an admin."""
    if _adapter is None:
        return "OneBot adapter not initialized"
    ctx = _msg_context.get()
    is_admin = ctx[0] if ctx is not None else getattr(_adapter, "_current_is_admin", False)
    if not is_admin:
        return "此操作需要管理员权限"
    return None


def _current_group_id() -> str:
    """获取当前消息的 group_id(供工具传给适配器侧做 real_seq→message_id 转换)。"""
    ctx = _msg_context.get()
    if ctx is not None:
        return ctx[1]
    return getattr(_adapter, "_current_group_id", "") if _adapter else ""


def _current_user_id() -> str:
    """获取当前消息的 user_id(DM 场景下 SeqMap 用 user_id 作 scope_id)。"""
    ctx = _msg_context.get()
    if ctx is not None:
        return ctx[2]
    return getattr(_adapter, "_current_user_id", "") if _adapter else ""


# ═══════════════════════════════════════════════════════════════════════════
# READ-ONLY TOOLS (no admin required)
# ═══════════════════════════════════════════════════════════════════════════


async def _get_login_info(args: dict, **_) -> str:
    try:
        data = await _api_call("get_login_info")
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_group_list(args: dict, **_) -> str:
    try:
        data = await _api_call("get_group_list")
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_group_info(args: dict, **_) -> str:
    try:
        data = await _api_call("get_group_info", group_id=int(args["group_id"]), no_cache=args.get("no_cache", False))
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_group_member_list(args: dict, **_) -> str:
    try:
        data = await _api_call("get_group_member_list", group_id=int(args["group_id"]))
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_group_member_info(args: dict, **_) -> str:
    try:
        data = await _api_call(
            "get_group_member_info",
            group_id=int(args["group_id"]),
            user_id=int(args["user_id"]),
            no_cache=args.get("no_cache", False),
        )
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_friend_list(args: dict, **_) -> str:
    try:
        data = await _api_call("get_friend_list")
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_user_info(args: dict, **_) -> str:
    try:
        data = await _api_call("get_stranger_info", user_id=int(args["user_id"]), no_cache=args.get("no_cache", False))
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_msg(args: dict, **_) -> str:
    try:
        params: dict = {"real_seq": int(args["real_seq"])}
        gid = _current_group_id()
        if gid:
            params["group_id"] = gid
        else:
            uid = _current_user_id()
            if uid:
                params["user_id"] = uid
        data = await _api_call("get_msg", **params)
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_group_msg_history(args: dict, **_) -> str:
    try:
        data = await _api_call(
            "get_group_msg_history",
            group_id=int(args["group_id"]),
            message_seq=int(args.get("message_seq", 0)),
            count=int(args.get("count", 20)),
        )
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_friend_msg_history(args: dict, **_) -> str:
    try:
        data = await _api_call(
            "get_friend_msg_history",
            user_id=int(args["user_id"]),
            count=int(args.get("count", 20)),
        )
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_forward_msg(args: dict, **_) -> str:
    try:
        data = await _api_call("get_forward_msg", message_id=args["message_id"])
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _mark_msg_as_read(args: dict, **_) -> str:
    try:
        seq_val = args.get("real_seq", 0)
        if seq_val:
            params: dict = {"real_seq": int(seq_val)}
        else:
            # 0 或未传 → 标记全部已读
            params = {"message_id": 0}
        gid = _current_group_id()
        if gid:
            params["group_id"] = gid
        else:
            uid = _current_user_id()
            if uid:
                params["user_id"] = uid
        data = await _api_call("mark_msg_as_read", **params)
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_file(args: dict, **_) -> str:
    try:
        data = await _api_call("get_file", file_id=args["file_id"])
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_recent_contact(args: dict, **_) -> str:
    try:
        data = await _api_call("get_recent_contact", count=int(args.get("count", 10)))
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _send_like(args: dict, **_) -> str:
    try:
        await _api_call("send_like", user_id=int(args["user_id"]), times=int(args.get("times", 1)))
        return tool_result({"liked": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_friends_with_category(args: dict, **_) -> str:
    try:
        data = await _api_call("get_friends_with_category")
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _get_profile_like(args: dict, **_) -> str:
    try:
        data = await _api_call("get_profile_like")
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _fetch_custom_face(args: dict, **_) -> str:
    try:
        data = await _api_call("fetch_custom_face", count=int(args.get("count", 48)))
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


# ═══════════════════════════════════════════════════════════════════════════
# MESSAGING TOOLS
# ═══════════════════════════════════════════════════════════════════════════


async def _send_message(args: dict, **_) -> str:
    try:
        data = await _api_call(
            "send_msg",
            message_type=args["message_type"],
            group_id=int(args["group_id"]) if args.get("group_id") else None,
            user_id=int(args["user_id"]) if args.get("user_id") else None,
            message=args["message"],
        )
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _recall_message(args: dict, **_) -> str:
    try:
        params: dict = {"real_seq": int(args["real_seq"])}
        gid = _current_group_id()
        if gid:
            params["group_id"] = gid
        else:
            uid = _current_user_id()
            if uid:
                params["user_id"] = uid
        await _api_call("delete_msg", **params)
        return tool_result({"recalled": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _send_forward_msg(args: dict, **_) -> str:
    try:
        data = await _api_call(
            "send_forward_msg",
            message_type=args["message_type"],
            group_id=int(args["group_id"]) if args.get("group_id") else None,
            user_id=int(args["user_id"]) if args.get("user_id") else None,
            messages=args["messages"],
        )
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _forward_single_msg(args: dict, **_) -> str:
    """单条消息转发(群聊或私聊)。action 由 group_id/user_id 决定。"""
    try:
        params: dict = {"real_seq": int(args["real_seq"])}
        gid = _current_group_id()
        uid = _current_user_id()
        if gid:
            params["group_id"] = int(gid)
            action = "forward_group_single_msg"
        elif uid:
            params["user_id"] = int(uid)
            action = "forward_friend_single_msg"
        elif args.get("group_id"):
            params["group_id"] = int(args["group_id"])
            action = "forward_group_single_msg"
        elif args.get("user_id"):
            params["user_id"] = int(args["user_id"])
            action = "forward_friend_single_msg"
        else:
            return tool_error("无法确定转发目标:需要 group_id 或 user_id")
        await _api_call(action, **params)
        return tool_result({"forwarded": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _poke(args: dict, **_) -> str:
    try:
        params: dict[str, Any] = {"user_id": int(args["user_id"])}
        if args.get("group_id"):
            params["group_id"] = int(args["group_id"])
        await _api_call("send_poke", **params)
        return tool_result({"poked": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _set_msg_emoji_like(args: dict, **_) -> str:
    try:
        params: dict = {"real_seq": int(args["real_seq"]), "emoji_id": args["emoji_id"]}
        gid = _current_group_id()
        if gid:
            params["group_id"] = gid
        else:
            uid = _current_user_id()
            if uid:
                params["user_id"] = uid
        await _api_call("set_msg_emoji_like", **params)
        return tool_result({"liked": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN TOOLS (require admin)
# ═══════════════════════════════════════════════════════════════════════════


async def _kick_group_member(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call(
            "set_group_kick",
            group_id=int(args["group_id"]),
            user_id=int(args["user_id"]),
            reject_add_request=args.get("reject_add_request", False),
        )
        return tool_result({"kicked": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _mute_group_member(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        duration = int(args.get("duration", 600))
        await _api_call(
            "set_group_ban",
            group_id=int(args["group_id"]),
            user_id=int(args["user_id"]),
            duration=duration,
        )
        return tool_result({"muted": True, "duration": duration})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _mute_group_whole(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call("set_group_whole_ban", group_id=int(args["group_id"]), enable=args.get("enable", True))
        return tool_result({"whole_ban": args.get("enable", True)})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _set_group_admin(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call(
            "set_group_admin",
            group_id=int(args["group_id"]),
            user_id=int(args["user_id"]),
            enable=args.get("enable", True),
        )
        return tool_result({"admin_set": args.get("enable", True)})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _set_group_card(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call(
            "set_group_card",
            group_id=int(args["group_id"]),
            user_id=int(args["user_id"]),
            card=args.get("card", ""),
        )
        return tool_result({"card_set": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _set_group_name(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call("set_group_name", group_id=int(args["group_id"]), group_name=args["group_name"])
        return tool_result({"name_set": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _leave_group(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call("set_group_leave", group_id=int(args["group_id"]))
        return tool_result({"left": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _handle_group_request(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call(
            "set_group_add_request",
            flag=args["flag"],
            sub_type=args.get("sub_type", "add"),
            approve=args.get("approve", True),
            reason=args.get("reason", ""),
        )
        return tool_result({"handled": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _handle_friend_request(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call(
            "set_friend_add_request",
            flag=args["flag"],
            approve=args.get("approve", True),
            remark=args.get("remark", ""),
        )
        return tool_result({"handled": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _delete_friend(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call("delete_friend", user_id=int(args["user_id"]))
        return tool_result({"deleted": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _set_group_special_title(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call(
            "set_group_special_title",
            group_id=int(args["group_id"]),
            user_id=int(args["user_id"]),
            special_title=args.get("special_title", ""),
        )
        return tool_result({"title_set": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _set_online_status(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call(
            "set_online_status",
            status=int(args["status"]),
            ext_status=int(args["ext_status"]),
            battery_status=int(args.get("battery_status", 0)),
        )
        return tool_result({"status_set": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _set_signature(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call("set_self_longnick", longNick=args["longNick"])
        return tool_result({"signature_set": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _set_avatar(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    try:
        await _api_call("set_qq_avatar", file=args["file"])
        return tool_result({"avatar_set": True})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


# ═══════════════════════════════════════════════════════════════════════════
# REGISTRATION
# ═══════════════════════════════════════════════════════════════════════════

TOOLSET = "onebot"

# Tool definitions: (name, handler, schema)
_TOOLS: list[tuple[str, Callable, dict]] = [
    # ── Read-only ──
    ("onebot_get_login_info", _get_login_info, _schema(
        "onebot_get_login_info", "获取当前登录账号信息（QQ号、昵称）。",
        {},
    )),
    ("onebot_get_group_list", _get_group_list, _schema(
        "onebot_get_group_list", "获取所有加入的群列表。",
        {},
    )),
    ("onebot_get_group_info", _get_group_info, _schema(
        "onebot_get_group_info", "获取指定群的信息（群名、人数等）。",
        {"group_id": _int("群号"), "no_cache": _bool("不使用缓存")},
        ["group_id"],
    )),
    ("onebot_get_group_member_list", _get_group_member_list, _schema(
        "onebot_get_group_member_list", "获取指定群的成员列表。",
        {"group_id": _int("群号")},
        ["group_id"],
    )),
    ("onebot_get_group_member_info", _get_group_member_info, _schema(
        "onebot_get_group_member_info", "获取指定群成员的详细信息（昵称、角色、入群时间等）。",
        {"group_id": _int("群号"), "user_id": _int("QQ号"), "no_cache": _bool("不使用缓存")},
        ["group_id", "user_id"],
    )),
    ("onebot_get_friend_list", _get_friend_list, _schema(
        "onebot_get_friend_list", "获取好友列表。",
        {},
    )),
    ("onebot_get_user_info", _get_user_info, _schema(
        "onebot_get_user_info", "获取陌生人信息（昵称、性别、年龄等）。",
        {"user_id": _int("QQ号"), "no_cache": _bool("不使用缓存")},
        ["user_id"],
    )),
    ("onebot_get_msg", _get_msg, _schema(
        "onebot_get_msg",
        "获取指定消息的详细内容。"
        "注意:如果目标消息属于合并转发,返回的 user_id 和 group_id"
        "可能是 NapCat 占位值,不可靠——不要用这些 ID 调用其他工具。",
        {"real_seq": _int("消息序号(群聊为前缀#后的群内序号,私聊为全局消息ID)")},
        ["real_seq"],
    )),
    ("onebot_get_group_msg_history", _get_group_msg_history, _schema(
        "onebot_get_group_msg_history", "获取群历史消息记录。",
        {
            "group_id": _int("群号"),
            "message_seq": _int(
                "起始消息ID(0为最新)。注意:此参数名虽为message_seq但实际填"
                "message_id(消息ID),不是群内序号real_seq;请用onebot_get_msg"
                "获取单条消息时传real_seq。"
            ),
            "count": _int("获取条数（默认20）"),
        },
        ["group_id"],
    )),
    ("onebot_get_friend_msg_history", _get_friend_msg_history, _schema(
        "onebot_get_friend_msg_history", "获取好友历史消息记录。",
        {"user_id": _int("QQ号"), "count": _int("获取条数（默认20）")},
        ["user_id"],
    )),
    ("onebot_get_forward_msg", _get_forward_msg, _schema(
        "onebot_get_forward_msg",
        "获取合并转发消息的详细内容。"
        "注意:返回子消息中的 user_id 和 group_id 可能是 NapCat 占位值,不可靠——不要用这些 ID 调用其他工具。",
        {"message_id": _str("合并转发消息的ID")},
        ["message_id"],
    )),
    ("onebot_mark_msg_as_read", _mark_msg_as_read, _schema(
        "onebot_mark_msg_as_read", "标记消息为已读。留空则标记全部已读。",
        {"real_seq": _int("消息序号(留空标记全部已读)")},
        [],
    )),
    ("onebot_get_file", _get_file, _schema(
        "onebot_get_file", "获取群/私聊文件信息(返回 url/path/size/name)。file_id 从消息段的 file 类型获取。",
        {"file_id": _str("文件ID(从消息段 file 类型获取)")},
        ["file_id"],
    )),
    ("onebot_get_recent_contact", _get_recent_contact, _schema(
        "onebot_get_recent_contact", "获取最近联系人列表(含最后一条消息预览)。",
        {"count": _int("返回数量(默认10)")},
        [],
    )),
    ("onebot_send_like", _send_like, _schema(
        "onebot_send_like", "给好友点赞(每日上限10次)。",
        {"user_id": _int("QQ号"), "times": _int("点赞次数(默认1)")},
        ["user_id"],
    )),
    ("onebot_get_friends_with_category", _get_friends_with_category, _schema(
        "onebot_get_friends_with_category",
        "获取带分类的好友列表(比 get_friend_list 信息更全:含分类名、在线数、签名、生日等)。",
        {},
    )),
    ("onebot_get_profile_like", _get_profile_like, _schema(
        "onebot_get_profile_like", "获取自身点赞列表(总点赞数、新点赞数、点赞用户详情)。",
        {},
    )),
    ("onebot_fetch_custom_face", _fetch_custom_face, _schema(
        "onebot_fetch_custom_face", "获取自定义表情列表(返回表情 URL 数组)。",
        {"count": _int("返回数量(默认48)")},
        [],
    )),
    # ── Messaging ──
    ("onebot_send_message", _send_message, _schema(
        "onebot_send_message",
        "主动发送 QQ 消息到指定群或私聊。"
        "回复当前对话通常直接输出文本即可——系统会自动把你的输出送达,无需调用本工具。"
        "当你需要主动发送消息时使用本工具:在当前会话中分多条发送、推送到其他群或用户、跨会话通知等。"
        "直接输出文本无法 @ 人,要 @ 某人必须用本工具的 at 段。"
        "message 为 OneBot 11 消息段数组,例如 "
        '纯文本 [{"type":"text","data":{"text":"hello"}}],'
        '或 @ 人 [{"type":"at","data":{"qq":"123456"}},{"type":"text","data":{"text":" 你好"}}]。',
        {
            "message_type": _str("'group' 或 'private'"),
            "group_id": _str("群号(message_type=group时必填)"),
            "user_id": _str("QQ号(message_type=private时必填)"),
            "message": _array("OneBot 11消息段数组"),
        },
        ["message_type", "message"],
    )),
    ("onebot_recall_message", _recall_message, _schema(
        "onebot_recall_message", "撤回指定消息。",
        {"real_seq": _int("消息序号(群聊为前缀#后的群内序号,私聊为全局消息ID)")},
        ["real_seq"],
    )),
    ("onebot_send_forward_msg", _send_forward_msg, _schema(
        "onebot_send_forward_msg",
        "发送合并转发消息(统一接口,支持群聊和私聊)。"
        "messages 为 node 消息段数组,每个 node 包含 name/uin/content 或引用已有消息的 id。"
        "返回 message_id 和 res_id。",
        {
            "message_type": _str("'group' 或 'private'"),
            "group_id": _str("群号(message_type=group时必填)"),
            "user_id": _str("QQ号(message_type=private时必填)"),
            "messages": _array("合并转发 node 消息段数组"),
        },
        ["message_type", "messages"],
    )),
    ("onebot_forward_single_msg", _forward_single_msg, _schema(
        "onebot_forward_single_msg",
        "单条消息转发到群聊或私聊(无需构造 node 数组,比合并转发更轻量)。"
        "默认转发到当前会话;无当前会话时通过 group_id 或 user_id 指定目标。",
        {
            "real_seq": _int("要转发的消息序号(群聊为前缀#后的群内序号,私聊为全局消息ID)"),
            "group_id": _int("目标群号(转发到群聊时填写)"),
            "user_id": _int("目标QQ号(转发到私聊时填写)"),
        },
        ["real_seq"],
    )),
    ("onebot_poke", _poke, _schema(
        "onebot_poke", "发送戳一戳（拍一拍）。",
        {"user_id": _int("目标QQ号"), "group_id": _str("群号（群内戳一拍时填写）")},
        ["user_id"],
    )),
    ("onebot_set_msg_emoji_like", _set_msg_emoji_like, _schema(
        "onebot_set_msg_emoji_like", "对消息发送表情回应。",
        {"real_seq": _int("消息序号(群聊为前缀#后的群内序号,私聊为全局消息ID)"), "emoji_id": _str("表情ID")},
        ["real_seq", "emoji_id"],
    )),
    # ── Admin (require admin) ──
    ("onebot_kick_group_member", _kick_group_member, _schema(
        "onebot_kick_group_member", "将成员踢出群聊（需管理员权限）。",
        {
            "group_id": _int("群号"),
            "user_id": _int("目标QQ号"),
            "reject_add_request": _bool("拒绝再次加群请求"),
        },
        ["group_id", "user_id"],
    )),
    ("onebot_mute_group_member", _mute_group_member, _schema(
        "onebot_mute_group_member", "禁言群成员（需管理员权限）。duration=0解除禁言。",
        {
            "group_id": _int("群号"),
            "user_id": _int("目标QQ号"),
            "duration": _int("禁言时长（秒，默认600）"),
        },
        ["group_id", "user_id"],
    )),
    ("onebot_mute_group_whole", _mute_group_whole, _schema(
        "onebot_mute_group_whole", "全员禁言（需管理员权限）。",
        {"group_id": _int("群号"), "enable": _bool("True开启False关闭")},
        ["group_id"],
    )),
    ("onebot_set_group_admin", _set_group_admin, _schema(
        "onebot_set_group_admin", "设置/取消群管理员（需群主权限）。",
        {"group_id": _int("群号"), "user_id": _int("目标QQ号"), "enable": _bool("True设置False取消")},
        ["group_id", "user_id"],
    )),
    ("onebot_set_group_card", _set_group_card, _schema(
        "onebot_set_group_card", "设置群名片（需管理员权限）。",
        {"group_id": _int("群号"), "user_id": _int("目标QQ号"), "card": _str("群名片内容")},
        ["group_id", "user_id"],
    )),
    ("onebot_set_group_name", _set_group_name, _schema(
        "onebot_set_group_name", "修改群名（需管理员权限）。",
        {"group_id": _int("群号"), "group_name": _str("新群名")},
        ["group_id", "group_name"],
    )),
    ("onebot_leave_group", _leave_group, _schema(
        "onebot_leave_group", "退出群聊（需管理员权限）。",
        {"group_id": _int("群号")},
        ["group_id"],
    )),
    ("onebot_handle_group_request", _handle_group_request, _schema(
        "onebot_handle_group_request", "处理加群请求/邀请（需管理员权限）。",
        {
            "flag": _str("请求flag（从事件中获取）"),
            "sub_type": _str("'add'加群 或 'invite'邀请"),
            "approve": _bool("是否同意"),
            "reason": _str("拒绝理由"),
        },
        ["flag"],
    )),
    ("onebot_handle_friend_request", _handle_friend_request, _schema(
        "onebot_handle_friend_request", "处理好友请求（需管理员权限）。",
        {"flag": _str("请求flag"), "approve": _bool("是否同意"), "remark": _str("备注名")},
        ["flag"],
    )),
    ("onebot_delete_friend", _delete_friend, _schema(
        "onebot_delete_friend", "删除好友（需管理员权限）。",
        {"user_id": _int("QQ号")},
        ["user_id"],
    )),
    ("onebot_set_group_special_title", _set_group_special_title, _schema(
        "onebot_set_group_special_title", "设置群成员专属头衔（需管理员权限）。空字符串删除头衔。",
        {"group_id": _int("群号"), "user_id": _int("QQ号"), "special_title": _str("专属头衔内容")},
        ["group_id", "user_id"],
    )),
    ("onebot_set_online_status", _set_online_status, _schema(
        "onebot_set_online_status", "设置机器人在线状态（需管理员权限）。status/ext_status 参考 NapCat 状态列表。",
        {
            "status": _int("在线状态编号"),
            "ext_status": _int("扩展状态编号"),
            "battery_status": _int("电量(0-100)"),
        },
        ["status", "ext_status"],
    )),
    ("onebot_set_signature", _set_signature, _schema(
        "onebot_set_signature", "设置机器人个性签名（需管理员权限）。",
        {"longNick": _str("个性签名内容")},
        ["longNick"],
    )),
    ("onebot_set_avatar", _set_avatar, _schema(
        "onebot_set_avatar", "设置机器人头像（需管理员权限）。",
        {"file": _str("图片路径或URL")},
        ["file"],
    )),
]


def register_tools(ctx) -> None:
    """Register all OneBot API tools via the plugin context."""
    for name, handler, schema in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            is_async=True,
            description=schema["description"],
            emoji="🐧",
        )


# Names of tools that require admin (call ``_check_admin()`` in their handler).
# Used by tests and the WebUI to identify privileged tools.  Kept in sync
# with the handler implementations — each admin handler starts with
# ``err = _check_admin()``.
_ADMIN_TOOL_NAMES = frozenset({
    "onebot_delete_friend",
    "onebot_handle_friend_request",
    "onebot_handle_group_request",
    "onebot_kick_group_member",
    "onebot_leave_group",
    "onebot_mute_group_member",
    "onebot_mute_group_whole",
    "onebot_set_avatar",
    "onebot_set_group_admin",
    "onebot_set_group_card",
    "onebot_set_group_name",
    "onebot_set_group_special_title",
    "onebot_set_online_status",
    "onebot_set_signature",
})

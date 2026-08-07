"""Plugin-bundled OneBot API tools for Hermes Agent.

These tools let the LLM call OneBot 11 actions (send messages
to arbitrary groups, manage group members, fetch histories, etc.) by routing
through the adapter service's WS ``api_call`` channel.

Registration is done via ``ctx.register_tool(...)`` at plugin load time —
no dependency on the host's ``tools/qq_tool.py``.

Admin gating uses a per-message ``ContextVar`` so concurrent messages cannot
share or overwrite authorization state.
"""
from __future__ import annotations

import contextvars
import functools
import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# ── Runtime bridge to the adapter's WS api_call channel ──────────────────

_adapter: Any = None  # OneBotAdapter instance (set by register_tools)
_api_caller: contextvars.ContextVar[Callable[[str, dict[str, Any]], Any] | None] = contextvars.ContextVar(
    "_onebot_api_caller", default=None,
)
_tool_authorized: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_onebot_tool_authorized", default=False,
)


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


async def _api_call(action: str, **params: Any) -> Any:
    """Call a OneBot action and return its data payload.

    The Hermes plugin transport returns an RPC envelope
    (``{"success": bool, "data"|"error": ...}``), while the WebUI automation
    caller already returns the unwrapped OneBot data and raises on failure.
    Normalize both paths here so every tool handler has the same contract:
    return data on success and raise on any transport/API failure.
    """
    caller = _api_caller.get()
    if caller is None and _adapter is None:
        raise RuntimeError("OneBot adapter not initialized")
    # Convert kwargs to a params dict, dropping None values
    clean = {k: v for k, v in params.items() if v is not None}
    try:
        if caller is not None:
            return await caller(action, clean)
        result = await _adapter._api_call(action, clean)
        if not isinstance(result, dict):
            raise RuntimeError(f"invalid adapter response for {action}: expected object")
        if result.get("success") is not True:
            error = result.get("error") or f"{action} failed without an error message"
            raise RuntimeError(str(error))
        return result.get("data")
    except Exception:
        logger.warning("OneBot tool API call failed action=%s", action, exc_info=True)
        raise


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
        # Match Hermes' tools.registry.tool_result wire format so standalone
        # adapter/WebUI behavior does not differ from the installed plugin.
        return json.dumps(data, ensure_ascii=False, default=str)

    def tool_error(msg: str) -> str:
        return json.dumps({"error": msg}, ensure_ascii=False)


# ── Admin gating ─────────────────────────────────────────────────────────


def _check_admin() -> str | None:
    """Return an error string if the current user is not an admin."""
    if _tool_authorized.get():
        return None
    ctx = _msg_context.get()
    is_admin = ctx[0] if ctx is not None else False
    if _adapter is None and _api_caller.get() is None:
        return "OneBot adapter not initialized"
    if not is_admin:
        return "此操作需要管理员权限"
    return None


def _current_group_id() -> str:
    """获取当前消息的 group_id(供工具传给适配器侧做 real_seq→message_id 转换)。"""
    ctx = _msg_context.get()
    return ctx[1] if ctx is not None else ""


def _current_user_id() -> str:
    """获取当前消息的 user_id(DM 场景下 SeqMap 用 user_id 作 scope_id)。"""
    ctx = _msg_context.get()
    return ctx[2] if ctx is not None else ""


def _resolve_message_target(args: dict) -> tuple[str, dict[str, int]]:
    """Resolve and validate a group/private destination.

    Explicit IDs take precedence.  Missing IDs may inherit only a matching
    current-chat context: group calls inherit the current group, while private
    calls inherit the current user only when the current chat itself is a DM.
    """
    message_type = str(args.get("message_type", ""))
    group_id = args.get("group_id")
    user_id = args.get("user_id")
    if message_type == "group":
        if user_id:
            raise ValueError("message_type=group 时不能提供 user_id")
        group_id = group_id or _current_group_id()
        if not group_id:
            raise ValueError("无法确定群聊目标:需要 group_id 或当前群聊上下文")
        return message_type, {"group_id": int(group_id)}
    if message_type == "private":
        if group_id:
            raise ValueError("message_type=private 时不能提供 group_id")
        # A group sender's user_id is not an implicit private-message target.
        if not user_id and not _current_group_id():
            user_id = _current_user_id()
        if not user_id:
            raise ValueError("无法确定私聊目标:需要 user_id 或当前私聊上下文")
        return message_type, {"user_id": int(user_id)}
    raise ValueError("message_type must be 'group' or 'private'")


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
        seq_val = args.get("real_seq")
        mark_all = args.get("all") is True
        if seq_val is not None and mark_all:
            return tool_error("real_seq 和 all=true 只能选择一个")
        if seq_val is not None:
            if int(seq_val) <= 0:
                return tool_error("real_seq 必须是正整数")
            params: dict = {"real_seq": int(seq_val)}
        elif mark_all:
            params = {"message_id": 0}
        else:
            return tool_error("需要提供 real_seq；标记当前会话全部已读时请显式传 all=true")
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


async def _get_bot_blacklist(args: dict, **_) -> str:
    """Query the adapter-local dynamic blacklist."""
    try:
        data = await _api_call(
            "adapter_get_bot_blacklist",
            scope=args.get("scope"),
            group_id=args.get("group_id"),
            user_id=args.get("user_id"),
        )
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _edit_bot_blacklist(args: dict, **_) -> str:
    """Set or remove an adapter-local dynamic blacklist entry."""
    try:
        action = str(args.get("action", ""))
        scope = str(args.get("scope", ""))
        group_id = args.get("group_id")
        if scope == "group" and not group_id:
            return tool_error("scope=group 时必须提供 group_id")
        if action == "set" and (not args.get("duration_seconds") or not str(args.get("reason", "")).strip()):
            return tool_error("action=set 时必须提供正数 duration_seconds 和非空 reason")
        data = await _api_call(
            "adapter_edit_bot_blacklist",
            operation=action,
            scope=scope,
            group_id=group_id,
            user_id=args.get("user_id"),
            duration_seconds=args.get("duration_seconds"),
            reason=args.get("reason"),
            created_by_user_id=_current_user_id(),
        )
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


# ═══════════════════════════════════════════════════════════════════════════
# MESSAGING TOOLS
# ═══════════════════════════════════════════════════════════════════════════


async def _send_message(args: dict, **_) -> str:
    try:
        message_type, target = _resolve_message_target(args)
        data = await _api_call(
            "send_msg",
            message_type=message_type,
            **target,
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
        message_type, target = _resolve_message_target(args)
        data = await _api_call(
            "send_forward_msg",
            message_type=message_type,
            **target,
            messages=args["messages"],
        )
        return tool_result(data)
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _forward_single_msg(args: dict, **_) -> str:
    """单条消息转发(群聊或私聊)。action 由 group_id/user_id 决定。

    Explicit ``group_id``/``user_id`` args take precedence over the current
    chat context, allowing the LLM to forward to a *different* group/user.
    """
    try:
        params: dict = {"real_seq": int(args["real_seq"])}
        if args.get("group_id") and args.get("user_id"):
            return tool_error("group_id 和 user_id 只能选择一个")
        # Explicit args first so the LLM can target a different chat.
        if args.get("group_id"):
            params["group_id"] = int(args["group_id"])
            action = "forward_group_single_msg"
        elif args.get("user_id"):
            params["user_id"] = int(args["user_id"])
            action = "forward_friend_single_msg"
        else:
            # Fall back to the current chat context.
            gid = _current_group_id()
            uid = _current_user_id()
            if gid:
                params["group_id"] = int(gid)
                action = "forward_group_single_msg"
            elif uid:
                params["user_id"] = int(uid)
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
        # Explicit targets take precedence, but default to the current group
        # context so an omitted group_id cannot accidentally turn a group poke
        # into a private poke.
        group_id = args.get("group_id") or _current_group_id()
        if group_id:
            params["group_id"] = int(group_id)
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
        duration = int(args["duration"])
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
        enable = bool(args["enable"])
        await _api_call("set_group_whole_ban", group_id=int(args["group_id"]), enable=enable)
        return tool_result({"whole_ban": enable})
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
            enable=bool(args["enable"]),
        )
        return tool_result({"admin_set": bool(args["enable"])})
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
            card=str(args["card"]),
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
        sub_type = str(args["sub_type"])
        if sub_type not in {"add", "invite"}:
            return tool_error("sub_type must be 'add' or 'invite'")
        await _api_call(
            "set_group_add_request",
            flag=args["flag"],
            sub_type=sub_type,
            approve=bool(args["approve"]),
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
            approve=bool(args["approve"]),
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
            special_title=str(args["special_title"]),
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


async def _upload_file(args: dict, **_) -> str:
    try:
        message_type, target = _resolve_message_target(args)
        file_ref = str(args["file"])
        name = str(args.get("name") or file_ref.rsplit("/", 1)[-1])
        if message_type == "group":
            await _api_call(
                "upload_group_file", **target, file=file_ref, name=name,
            )
        else:
            await _api_call(
                "upload_private_file", **target, file=file_ref, name=name,
            )
        return tool_result({"uploaded": True, "name": name})
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


async def _run_action(action: str, **params: Any) -> str:
    try:
        return tool_result(await _api_call(action, **params))
    except Exception as e:
        logger.warning("tool call failed: %s", e)
        return tool_error(str(e))


def _group_seq_params(args: dict) -> dict[str, Any]:
    params: dict[str, Any] = {"real_seq": int(args["real_seq"])}
    group_id = args.get("group_id") or _current_group_id()
    if group_id:
        params["group_id"] = int(group_id)
    return params


async def _get_essence_msg_list(args: dict, **_) -> str:
    return await _run_action("get_essence_msg_list", group_id=int(args["group_id"]))


async def _set_essence_msg(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action("set_essence_msg", **_group_seq_params(args))


async def _delete_essence_msg(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action("delete_essence_msg", **_group_seq_params(args))


async def _get_group_notice(args: dict, **_) -> str:
    return await _run_action("_get_group_notice", group_id=int(args["group_id"]))


async def _send_group_notice(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action(
        "_send_group_notice", group_id=int(args["group_id"]), content=args["content"], image=args.get("image"),
        pinned=int(args.get("pinned", 0)), type=int(args.get("type", 1)),
        confirm_required=int(args.get("confirm_required", 1)),
        is_show_edit_card=int(args.get("is_show_edit_card", 0)), tip_window_type=int(args.get("tip_window_type", 0)),
    )


async def _del_group_notice(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action("_del_group_notice", group_id=int(args["group_id"]), notice_id=args["notice_id"])


async def _set_group_sign(args: dict, **_) -> str:
    return await _run_action("set_group_sign", group_id=int(args["group_id"]))


async def _get_group_signed_list(args: dict, **_) -> str:
    return await _run_action("get_group_signed_list", group_id=int(args["group_id"]))


async def _get_qun_album_list(args: dict, **_) -> str:
    return await _run_action(
        "get_qun_album_list", group_id=int(args["group_id"]), attach_info=args.get("attach_info", ""),
    )


async def _get_group_album_media_list(args: dict, **_) -> str:
    return await _run_action(
        "get_group_album_media_list", group_id=int(args["group_id"]), album_id=args["album_id"],
        attach_info=args.get("attach_info", ""),
    )


async def _upload_image_to_qun_album(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action(
        "upload_image_to_qun_album", group_id=int(args["group_id"]), album_id=args["album_id"],
        album_name=args["album_name"], file=args["file"],
    )


async def _set_group_album_media_like(args: dict, **_) -> str:
    return await _run_action(
        "set_group_album_media_like", group_id=int(args["group_id"]), album_id=args["album_id"],
        batch_id=args["batch_id"], lloc=args.get("lloc"),
    )


async def _cancel_group_album_media_like(args: dict, **_) -> str:
    return await _run_action(
        "cancel_group_album_media_like", group_id=int(args["group_id"]), album_id=args["album_id"],
        batch_id=args["batch_id"], lloc=args.get("lloc"),
    )


async def _do_group_album_comment(args: dict, **_) -> str:
    return await _run_action(
        "do_group_album_comment", group_id=int(args["group_id"]), album_id=args["album_id"],
        lloc=args["lloc"], content=args["content"],
    )


async def _del_group_album_media(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action(
        "del_group_album_media", group_id=int(args["group_id"]), album_id=args["album_id"], lloc=args["lloc"],
    )


async def _group_todo(args: dict, action: str) -> str:
    return await _run_action(action, **_group_seq_params(args))


async def _set_group_todo(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _group_todo(args, "set_group_todo")


async def _complete_group_todo(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _group_todo(args, "complete_group_todo")


async def _cancel_group_todo(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _group_todo(args, "cancel_group_todo")


async def _set_friend_remark(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action("set_friend_remark", user_id=int(args["user_id"]), remark=args["remark"])


async def _get_unidirectional_friend_list(args: dict, **_) -> str:
    return await _run_action("get_unidirectional_friend_list")


async def _set_qq_profile(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action(
        "set_qq_profile", nickname=args["nickname"], personal_note=args.get("personal_note"), sex=args.get("sex"),
    )


async def _nc_get_user_status(args: dict, **_) -> str:
    return await _run_action("nc_get_user_status", user_id=int(args["user_id"]))


async def _get_doubt_friends_add_request(args: dict, **_) -> str:
    return await _run_action("get_doubt_friends_add_request", count=int(args.get("count", 50)))


async def _get_group_ignore_add_request(args: dict, **_) -> str:
    return await _run_action("get_group_ignore_add_request")


async def _fetch_custom_face_detail(args: dict, **_) -> str:
    return await _run_action("fetch_custom_face_detail", count=int(args.get("count", 48)))


async def _add_custom_face(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action(
        "add_custom_face", file=args["file"], emoji_id=args.get("emoji_id"), package_id=args.get("package_id"),
        file_name=args.get("file_name"), file_size=args.get("file_size"), md5=args.get("md5"),
        is_mark_face=args.get("is_mark_face"), is_origin=args.get("is_origin"),
    )


async def _delete_custom_face(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action("delete_custom_face", res_id=args.get("res_id"), ids=args.get("ids"))


async def _set_custom_face_desc(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action(
        "set_custom_face_desc", emoji_id=args["emoji_id"], res_id=args["res_id"], md5=args["md5"], desc=args["desc"],
    )


async def _set_group_portrait(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action("set_group_portrait", group_id=int(args["group_id"]), file=args["file"])


async def _set_group_remark(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action("set_group_remark", group_id=int(args["group_id"]), remark=args["remark"])


async def _get_group_ignored_notifies(args: dict, **_) -> str:
    return await _run_action("get_group_ignored_notifies")


async def _get_group_shut_list(args: dict, **_) -> str:
    return await _run_action("get_group_shut_list", group_id=int(args["group_id"]))


async def _get_group_info_ex(args: dict, **_) -> str:
    return await _run_action("get_group_info_ex", group_id=int(args["group_id"]))


async def _get_group_detail_info(args: dict, **_) -> str:
    return await _run_action("get_group_detail_info", group_id=int(args["group_id"]))


async def _create_collection(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _run_action("create_collection", rawData=args["rawData"], brief=args["brief"])


async def _get_collection_list(args: dict, **_) -> str:
    return await _run_action("get_collection_list", category=args["category"], count=str(args.get("count", "50")))


async def _fetch_emoji_like(args: dict, **_) -> str:
    params = _group_seq_params(args)
    params.update({
        "emojiId": args["emoji_id"], "emojiType": args["emoji_type"],
        "count": int(args.get("count", 20)), "cookie": args.get("cookie", ""),
    })
    return await _run_action("fetch_emoji_like", **params)


async def _get_emoji_likes(args: dict, **_) -> str:
    params = _group_seq_params(args)
    params.update({
        "emoji_id": args["emoji_id"], "emoji_type": args.get("emoji_type"),
        "count": int(args.get("count", 0)),
    })
    return await _run_action("get_emoji_likes", **params)


async def _group_file_read(args: dict, action: str, **extra: Any) -> str:
    return await _run_action(action, group_id=int(args["group_id"]), **extra)


async def _get_group_file_system_info(args: dict, **_) -> str:
    return await _group_file_read(args, "get_group_file_system_info")


async def _get_group_root_files(args: dict, **_) -> str:
    return await _group_file_read(args, "get_group_root_files", file_count=int(args.get("file_count", 50)))


async def _get_group_files_by_folder(args: dict, **_) -> str:
    return await _group_file_read(
        args, "get_group_files_by_folder", folder_id=args["folder_id"], file_count=int(args.get("file_count", 50)),
    )


async def _get_group_file_url(args: dict, **_) -> str:
    return await _group_file_read(args, "get_group_file_url", file_id=args["file_id"])


async def _admin_group_file(args: dict, action: str, **extra: Any) -> str:
    return await _run_action(action, group_id=int(args["group_id"]), **extra)


async def _delete_group_file(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _admin_group_file(args, "delete_group_file", file_id=args["file_id"])


async def _create_group_file_folder(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _admin_group_file(args, "create_group_file_folder", folder_name=args["folder_name"])


async def _delete_group_folder(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _admin_group_file(args, "delete_group_folder", folder_id=args["folder_id"])


async def _move_group_file(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _admin_group_file(
        args, "move_group_file", file_id=args["file_id"], current_parent_directory=args["current_parent_directory"],
        target_parent_directory=args["target_parent_directory"],
    )


async def _rename_group_file(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _admin_group_file(
        args, "rename_group_file", file_id=args["file_id"], current_parent_directory=args["current_parent_directory"],
        new_name=args["new_name"],
    )


async def _trans_group_file(args: dict, **_) -> str:
    err = _check_admin()
    if err:
        return tool_error(err)
    return await _admin_group_file(args, "trans_group_file", file_id=args["file_id"])


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
        "onebot_mark_msg_as_read",
        "标记消息为已读。标记单条时传 real_seq；标记当前会话全部已读时必须显式传 all=true，两者不能同时传。",
        {"real_seq": _int("消息序号"), "all": _bool("显式确认标记当前会话全部已读")},
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
    ("onebot_get_bot_blacklist", _get_bot_blacklist, _schema(
        "onebot_get_bot_blacklist",
        "查看 bot 独立维护的临时用户黑名单。可按作用域、群号或用户筛选；不传筛选条件时返回全部有效记录。",
        {
            "scope": _str("可选：group（指定群）、dm（私聊）或 global（全部会话）"),
            "group_id": _str("群号；筛选 group 作用域时填写"),
            "user_id": _str("QQ号；留空则不过滤用户"),
        },
        [],
    )),
    ("onebot_edit_bot_blacklist", _edit_bot_blacklist, _schema(
        "onebot_edit_bot_blacklist",
        "新增、覆盖或解除 bot 的临时用户黑名单。action=set 时填写时长和原因；"
        "超过 WebUI 配置的最大时长会自动截短。不能拉黑全局管理员或目标群的群管理员。",
        {
            "action": _str("set（新增/覆盖）或 remove（解除）"),
            "scope": _str("group（指定群）、dm（私聊）或 global（全部会话）"),
            "group_id": _str("群号；scope=group 时必填"),
            "user_id": _str("目标 QQ 号"),
            "duration_seconds": _int("拉黑秒数；action=set 时必填"),
            "reason": _str("拉黑原因；action=set 时必填，会显示在拦截提示中"),
        },
        ["action", "scope", "user_id"],
    )),
    # ── Messaging ──
    ("onebot_send_message", _send_message, _schema(
        "onebot_send_message",
        "主动发送 QQ 消息到指定群或私聊。"
        "回复当前对话通常直接输出文本即可——系统会自动把你的输出送达,无需调用本工具。"
        "当你需要主动发送消息时使用本工具:在当前会话中分多条发送、推送到其他群或用户、跨会话通知等。"
        "目标 ID 省略时只会继承同类型的当前会话；目标类型冲突时会拒绝调用。"
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
        "目标 ID 省略时只会继承同类型的当前会话；目标类型冲突时会拒绝调用。"
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
        "显式传入 group_id 或 user_id 时优先使用指定目标,否则转发到当前会话;"
        "两者同时传入或在无上下文时均未传入会报错。",
        {
            "real_seq": _int("要转发的消息序号(群聊为前缀#后的群内序号,私聊为全局消息ID)"),
            "group_id": _int("目标群号(转发到群聊时填写)"),
            "user_id": _int("目标QQ号(转发到私聊时填写)"),
        },
        ["real_seq"],
    )),
    ("onebot_poke", _poke, _schema(
        "onebot_poke",
        "发送戳一戳（拍一拍）。群聊中省略 group_id 时自动使用当前群；仅跨群操作时需要显式填写。",
        {"user_id": _int("目标QQ号"), "group_id": _str("目标群号（可选；默认使用当前群聊）")},
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
        "onebot_mute_group_member", "禁言或解除禁言群成员（需管理员权限）。duration 必填，0 表示解除禁言。",
        {
            "group_id": _int("群号"),
            "user_id": _int("目标QQ号"),
            "duration": _int("必填；禁言时长（秒），0 表示解除禁言"),
        },
        ["group_id", "user_id", "duration"],
    )),
    ("onebot_mute_group_whole", _mute_group_whole, _schema(
        "onebot_mute_group_whole", "全员禁言（需管理员权限）。",
        {"group_id": _int("群号"), "enable": _bool("必填；True开启，False关闭")},
        ["group_id", "enable"],
    )),
    ("onebot_set_group_admin", _set_group_admin, _schema(
        "onebot_set_group_admin", "设置/取消群管理员（需群主权限）。",
        {"group_id": _int("群号"), "user_id": _int("目标QQ号"), "enable": _bool("必填；True设置，False取消")},
        ["group_id", "user_id", "enable"],
    )),
    ("onebot_set_group_card", _set_group_card, _schema(
        "onebot_set_group_card", "设置群名片（需管理员权限）。card 必填；显式传空字符串可清空名片。",
        {"group_id": _int("群号"), "user_id": _int("目标QQ号"), "card": _str("群名片内容")},
        ["group_id", "user_id", "card"],
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
        ["flag", "sub_type", "approve"],
    )),
    ("onebot_handle_friend_request", _handle_friend_request, _schema(
        "onebot_handle_friend_request", "处理好友请求（需管理员权限）。",
        {"flag": _str("请求flag"), "approve": _bool("是否同意"), "remark": _str("备注名")},
        ["flag", "approve"],
    )),
    ("onebot_delete_friend", _delete_friend, _schema(
        "onebot_delete_friend", "删除好友（需管理员权限）。",
        {"user_id": _int("QQ号")},
        ["user_id"],
    )),
    ("onebot_set_group_special_title", _set_group_special_title, _schema(
        "onebot_set_group_special_title", "设置群成员专属头衔（需管理员权限）。空字符串删除头衔。",
        {"group_id": _int("群号"), "user_id": _int("QQ号"), "special_title": _str("专属头衔内容")},
        ["group_id", "user_id", "special_title"],
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
    ("onebot_upload_file", _upload_file, _schema(
        "onebot_upload_file",
        "上传群文件或私聊文件。目标 ID 省略时只会继承同类型的当前会话；目标类型冲突时会拒绝调用。",
        {
            "message_type": _str("'group' 或 'private'"),
            "group_id": _int("群号(message_type=group时必填)"),
            "user_id": _int("QQ号(message_type=private时必填)"),
            "file": _str("允许目录内的绝对路径或 http(s) URL"),
            "name": _str("显示文件名(可选)"),
        },
        ["message_type", "file"],
    )),
    ("onebot_get_essence_msg_list", _get_essence_msg_list, _schema(
        "onebot_get_essence_msg_list", "获取群精华消息列表。", {"group_id": _int("群号")}, ["group_id"],
    )),
    ("onebot_set_essence_msg", _set_essence_msg, _schema(
        "onebot_set_essence_msg", "设置群精华消息（需管理员权限）。",
        {"real_seq": _int("群内消息序号"), "group_id": _int("群号；默认当前群")}, ["real_seq"],
    )),
    ("onebot_delete_essence_msg", _delete_essence_msg, _schema(
        "onebot_delete_essence_msg", "移除群精华消息（需管理员权限）。",
        {"real_seq": _int("群内消息序号"), "group_id": _int("群号；默认当前群")}, ["real_seq"],
    )),
    ("onebot_get_group_notice", _get_group_notice, _schema(
        "onebot_get_group_notice", "获取群公告列表。", {"group_id": _int("群号")}, ["group_id"],
    )),
    ("onebot_send_group_notice", _send_group_notice, _schema(
        "onebot_send_group_notice", "发布群公告（需管理员权限）。",
        {
            "group_id": _int("群号"), "content": _str("公告内容"), "image": _str("图片路径或URL"),
            "pinned": _int("是否置顶(0/1)"), "type": _int("公告类型，默认1"),
            "confirm_required": _int("是否需要确认(0/1)"),
            "is_show_edit_card": _int("是否显示修改群名片引导(0/1)"), "tip_window_type": _int("弹窗类型"),
        },
        ["group_id", "content"],
    )),
    ("onebot_del_group_notice", _del_group_notice, _schema(
        "onebot_del_group_notice", "删除群公告（需管理员权限）。",
        {"group_id": _int("群号"), "notice_id": _str("公告ID")}, ["group_id", "notice_id"],
    )),
    ("onebot_set_group_sign", _set_group_sign, _schema(
        "onebot_set_group_sign", "在指定群签到。", {"group_id": _int("群号")}, ["group_id"],
    )),
    ("onebot_get_group_signed_list", _get_group_signed_list, _schema(
        "onebot_get_group_signed_list", "获取指定群今日签到列表。", {"group_id": _int("群号")}, ["group_id"],
    )),
    ("onebot_get_qun_album_list", _get_qun_album_list, _schema(
        "onebot_get_qun_album_list", "获取群相册列表。",
        {"group_id": _int("群号"), "attach_info": _str("分页附加信息")}, ["group_id"],
    )),
    ("onebot_get_group_album_media_list", _get_group_album_media_list, _schema(
        "onebot_get_group_album_media_list", "获取群相册媒体列表。",
        {"group_id": _int("群号"), "album_id": _str("相册ID"), "attach_info": _str("分页附加信息")},
        ["group_id", "album_id"],
    )),
    ("onebot_upload_image_to_qun_album", _upload_image_to_qun_album, _schema(
        "onebot_upload_image_to_qun_album", "上传图片到群相册（需管理员权限）。",
        {
            "group_id": _int("群号"), "album_id": _str("相册ID"), "album_name": _str("相册名称"),
            "file": _str("图片路径、URL或Base64"),
        },
        ["group_id", "album_id", "album_name", "file"],
    )),
    ("onebot_set_group_album_media_like", _set_group_album_media_like, _schema(
        "onebot_set_group_album_media_like", "点赞群相册媒体。",
        {
            "group_id": _int("群号"), "album_id": _str("相册ID"), "batch_id": _str("上传批次ID"),
            "lloc": _str("媒体ID；点赞整个上传批次时省略"),
        },
        ["group_id", "album_id", "batch_id"],
    )),
    ("onebot_cancel_group_album_media_like", _cancel_group_album_media_like, _schema(
        "onebot_cancel_group_album_media_like", "取消点赞群相册媒体。",
        {
            "group_id": _int("群号"), "album_id": _str("相册ID"), "batch_id": _str("上传批次ID"),
            "lloc": _str("媒体ID；操作整个上传批次时省略"),
        },
        ["group_id", "album_id", "batch_id"],
    )),
    ("onebot_do_group_album_comment", _do_group_album_comment, _schema(
        "onebot_do_group_album_comment", "评论群相册媒体。",
        {
            "group_id": _int("群号"), "album_id": _str("相册ID"), "lloc": _str("媒体ID"),
            "content": _str("评论内容"),
        },
        ["group_id", "album_id", "lloc", "content"],
    )),
    ("onebot_del_group_album_media", _del_group_album_media, _schema(
        "onebot_del_group_album_media", "删除群相册媒体（需管理员权限）。",
        {"group_id": _int("群号"), "album_id": _str("相册ID"), "lloc": _str("媒体ID")},
        ["group_id", "album_id", "lloc"],
    )),
    ("onebot_set_group_todo", _set_group_todo, _schema(
        "onebot_set_group_todo", "将群消息设为待办（需管理员权限）。",
        {"group_id": _int("群号"), "real_seq": _int("群内消息序号")}, ["group_id", "real_seq"],
    )),
    ("onebot_complete_group_todo", _complete_group_todo, _schema(
        "onebot_complete_group_todo", "完成群消息待办（需管理员权限）。",
        {"group_id": _int("群号"), "real_seq": _int("群内消息序号")}, ["group_id", "real_seq"],
    )),
    ("onebot_cancel_group_todo", _cancel_group_todo, _schema(
        "onebot_cancel_group_todo", "取消群消息待办（需管理员权限）。",
        {"group_id": _int("群号"), "real_seq": _int("群内消息序号")}, ["group_id", "real_seq"],
    )),
    ("onebot_set_friend_remark", _set_friend_remark, _schema(
        "onebot_set_friend_remark", "设置好友备注（需管理员权限）。",
        {"user_id": _int("QQ号"), "remark": _str("备注内容")}, ["user_id", "remark"],
    )),
    ("onebot_get_unidirectional_friend_list", _get_unidirectional_friend_list, _schema(
        "onebot_get_unidirectional_friend_list", "获取单向好友列表。", {},
    )),
    ("onebot_set_qq_profile", _set_qq_profile, _schema(
        "onebot_set_qq_profile", "修改机器人QQ资料（需管理员权限）。",
        {"nickname": _str("昵称"), "personal_note": _str("个性签名"), "sex": _int("性别：0未知、1男、2女")},
        ["nickname"],
    )),
    ("onebot_nc_get_user_status", _nc_get_user_status, _schema(
        "onebot_nc_get_user_status", "获取指定QQ用户状态。", {"user_id": _int("QQ号")}, ["user_id"],
    )),
    ("onebot_get_doubt_friends_add_request", _get_doubt_friends_add_request, _schema(
        "onebot_get_doubt_friends_add_request", "获取可疑好友申请列表。", {"count": _int("数量，默认50")},
    )),
    ("onebot_get_group_ignore_add_request", _get_group_ignore_add_request, _schema(
        "onebot_get_group_ignore_add_request", "获取被忽略的加群请求。", {},
    )),
    ("onebot_fetch_custom_face_detail", _fetch_custom_face_detail, _schema(
        "onebot_fetch_custom_face_detail", "获取自定义表情详情。", {"count": _int("数量，默认48")},
    )),
    ("onebot_add_custom_face", _add_custom_face, _schema(
        "onebot_add_custom_face", "添加机器人自定义表情（需管理员权限）。",
        {
            "file": _str("本地表情文件路径"), "emoji_id": _str("表情ID"), "package_id": _int("表情包ID"),
            "file_name": _str("文件名"), "file_size": _int("文件大小"), "md5": _str("文件MD5"),
            "is_mark_face": _bool("是否商城表情"), "is_origin": _bool("是否原图"),
        },
        ["file"],
    )),
    ("onebot_delete_custom_face", _delete_custom_face, _schema(
        "onebot_delete_custom_face", "删除机器人自定义表情（需管理员权限）。",
        {
            "res_id": _str("资源ID；单个删除时填写"),
            "ids": {"type": "array", "description": "资源ID列表", "items": {"type": "string"}},
        },
    )),
    ("onebot_set_custom_face_desc", _set_custom_face_desc, _schema(
        "onebot_set_custom_face_desc", "修改机器人自定义表情描述（需管理员权限）。",
        {
            "emoji_id": _str("表情ID"), "res_id": _str("资源ID"), "md5": _str("表情MD5"),
            "desc": _str("新描述"),
        },
        ["emoji_id", "res_id", "md5", "desc"],
    )),
    ("onebot_set_group_portrait", _set_group_portrait, _schema(
        "onebot_set_group_portrait", "设置群头像（需管理员权限）。",
        {"group_id": _int("群号"), "file": _str("图片路径或URL")}, ["group_id", "file"],
    )),
    ("onebot_set_group_remark", _set_group_remark, _schema(
        "onebot_set_group_remark", "设置机器人侧群备注（需管理员权限）。",
        {"group_id": _int("群号"), "remark": _str("群备注")}, ["group_id", "remark"],
    )),
    ("onebot_get_group_ignored_notifies", _get_group_ignored_notifies, _schema(
        "onebot_get_group_ignored_notifies", "获取被忽略的入群申请和邀请通知。", {},
    )),
    ("onebot_get_group_shut_list", _get_group_shut_list, _schema(
        "onebot_get_group_shut_list", "获取群禁言成员列表。", {"group_id": _int("群号")}, ["group_id"],
    )),
    ("onebot_get_group_info_ex", _get_group_info_ex, _schema(
        "onebot_get_group_info_ex", "获取群扩展信息。", {"group_id": _int("群号")}, ["group_id"],
    )),
    ("onebot_get_group_detail_info", _get_group_detail_info, _schema(
        "onebot_get_group_detail_info", "获取群详细信息。", {"group_id": _int("群号")}, ["group_id"],
    )),
    ("onebot_create_collection", _create_collection, _schema(
        "onebot_create_collection", "创建机器人账号收藏（需管理员权限）。",
        {"rawData": _str("收藏原始数据"), "brief": _str("简要描述")}, ["rawData", "brief"],
    )),
    ("onebot_get_collection_list", _get_collection_list, _schema(
        "onebot_get_collection_list", "获取机器人账号收藏列表。",
        {"category": _str("分类ID"), "count": _int("数量，默认50")}, ["category"],
    )),
    ("onebot_fetch_emoji_like", _fetch_emoji_like, _schema(
        "onebot_fetch_emoji_like", "分页获取消息表情回应详情。",
        {
            "real_seq": _int("群内消息序号"), "group_id": _int("群号；默认当前群"),
            "emoji_id": _str("表情ID"), "emoji_type": _str("表情类型"), "count": _int("数量，默认20"),
            "cookie": _str("分页Cookie"),
        },
        ["real_seq", "emoji_id", "emoji_type"],
    )),
    ("onebot_get_emoji_likes", _get_emoji_likes, _schema(
        "onebot_get_emoji_likes", "获取消息表情回应用户列表。",
        {
            "real_seq": _int("群内消息序号"), "group_id": _int("群号；默认当前群"),
            "emoji_id": _str("表情ID"), "emoji_type": _str("表情类型；可省略"), "count": _int("数量；0表示全部"),
        },
        ["real_seq", "emoji_id"],
    )),
    ("onebot_get_group_file_system_info", _get_group_file_system_info, _schema(
        "onebot_get_group_file_system_info", "获取群文件系统容量信息。", {"group_id": _int("群号")}, ["group_id"],
    )),
    ("onebot_get_group_root_files", _get_group_root_files, _schema(
        "onebot_get_group_root_files", "获取群文件根目录内容。",
        {"group_id": _int("群号"), "file_count": _int("文件数量，默认50")}, ["group_id"],
    )),
    ("onebot_get_group_files_by_folder", _get_group_files_by_folder, _schema(
        "onebot_get_group_files_by_folder", "获取指定群文件夹内容。",
        {"group_id": _int("群号"), "folder_id": _str("文件夹ID"), "file_count": _int("文件数量，默认50")},
        ["group_id", "folder_id"],
    )),
    ("onebot_get_group_file_url", _get_group_file_url, _schema(
        "onebot_get_group_file_url", "获取群文件下载URL。",
        {"group_id": _int("群号"), "file_id": _str("文件ID")}, ["group_id", "file_id"],
    )),
    ("onebot_delete_group_file", _delete_group_file, _schema(
        "onebot_delete_group_file", "删除群文件（需管理员权限）。",
        {"group_id": _int("群号"), "file_id": _str("文件ID")}, ["group_id", "file_id"],
    )),
    ("onebot_create_group_file_folder", _create_group_file_folder, _schema(
        "onebot_create_group_file_folder", "创建群文件夹（需管理员权限）。",
        {"group_id": _int("群号"), "folder_name": _str("文件夹名称")}, ["group_id", "folder_name"],
    )),
    ("onebot_delete_group_folder", _delete_group_folder, _schema(
        "onebot_delete_group_folder", "删除群文件夹（需管理员权限）。",
        {"group_id": _int("群号"), "folder_id": _str("文件夹ID")}, ["group_id", "folder_id"],
    )),
    ("onebot_move_group_file", _move_group_file, _schema(
        "onebot_move_group_file", "移动群文件（需管理员权限）。",
        {
            "group_id": _int("群号"), "file_id": _str("文件ID"),
            "current_parent_directory": _str("当前父目录ID"), "target_parent_directory": _str("目标父目录ID"),
        },
        ["group_id", "file_id", "current_parent_directory", "target_parent_directory"],
    )),
    ("onebot_rename_group_file", _rename_group_file, _schema(
        "onebot_rename_group_file", "重命名群文件（需管理员权限）。",
        {
            "group_id": _int("群号"), "file_id": _str("文件ID"),
            "current_parent_directory": _str("当前父目录ID"), "new_name": _str("新文件名"),
        },
        ["group_id", "file_id", "current_parent_directory", "new_name"],
    )),
    ("onebot_trans_group_file", _trans_group_file, _schema(
        "onebot_trans_group_file", "转存群文件（需管理员权限）。",
        {"group_id": _int("群号"), "file_id": _str("文件ID")}, ["group_id", "file_id"],
    )),
]


# Names of tools that require admin (call ``_check_admin()`` in their handler).
# Used by tests and the WebUI to identify privileged tools.  Kept in sync
# with the handler implementations — each admin handler starts with
# ``err = _check_admin()``.
_ADMIN_TOOL_NAMES = frozenset({
    "onebot_add_custom_face",
    "onebot_cancel_group_todo",
    "onebot_complete_group_todo",
    "onebot_create_collection",
    "onebot_create_group_file_folder",
    "onebot_del_group_album_media",
    "onebot_del_group_notice",
    "onebot_delete_custom_face",
    "onebot_delete_essence_msg",
    "onebot_delete_friend",
    "onebot_delete_group_file",
    "onebot_delete_group_folder",
    "onebot_handle_friend_request",
    "onebot_handle_group_request",
    "onebot_kick_group_member",
    "onebot_leave_group",
    "onebot_mute_group_member",
    "onebot_mute_group_whole",
    "onebot_move_group_file",
    "onebot_rename_group_file",
    "onebot_send_group_notice",
    "onebot_set_custom_face_desc",
    "onebot_set_essence_msg",
    "onebot_set_friend_remark",
    "onebot_set_avatar",
    "onebot_set_group_admin",
    "onebot_set_group_card",
    "onebot_set_group_name",
    "onebot_set_group_portrait",
    "onebot_set_group_remark",
    "onebot_set_group_special_title",
    "onebot_set_group_todo",
    "onebot_set_online_status",
    "onebot_set_qq_profile",
    "onebot_set_signature",
    "onebot_trans_group_file",
    "onebot_upload_image_to_qun_album",
})

# New account-private reads and all newly added mutations default to admin,
# even when their raw handler historically had no hard-coded check.  The raw
# handlers stay policy-free so the full-privilege automation API remains
# unaffected; Hermes registration wraps them below.
_DEFAULT_ADMIN_TOOL_NAMES = _ADMIN_TOOL_NAMES | frozenset({
    "onebot_cancel_group_album_media_like",
    "onebot_do_group_album_comment",
    "onebot_fetch_custom_face_detail",
    "onebot_get_collection_list",
    "onebot_get_doubt_friends_add_request",
    "onebot_get_group_ignore_add_request",
    "onebot_get_group_ignored_notifies",
    "onebot_get_unidirectional_friend_list",
    "onebot_set_group_album_media_like",
    "onebot_set_group_sign",
})

_ACCOUNT_TOOL_NAMES = frozenset({
    "onebot_add_custom_face",
    "onebot_create_collection",
    "onebot_delete_custom_face",
    "onebot_delete_friend",
    "onebot_fetch_custom_face",
    "onebot_fetch_custom_face_detail",
    "onebot_get_bot_blacklist",
    "onebot_get_collection_list",
    "onebot_get_doubt_friends_add_request",
    "onebot_get_friend_list",
    "onebot_get_friends_with_category",
    "onebot_get_group_ignore_add_request",
    "onebot_get_group_ignored_notifies",
    "onebot_get_login_info",
    "onebot_get_profile_like",
    "onebot_get_recent_contact",
    "onebot_get_unidirectional_friend_list",
    "onebot_handle_friend_request",
    "onebot_nc_get_user_status",
    "onebot_send_like",
    "onebot_set_avatar",
    "onebot_set_custom_face_desc",
    "onebot_set_friend_remark",
    "onebot_set_online_status",
    "onebot_set_qq_profile",
    "onebot_set_signature",
})

_GROUP_CONTEXT_TOOL_NAMES = frozenset({
    "onebot_delete_essence_msg",
    "onebot_fetch_emoji_like",
    "onebot_get_emoji_likes",
    "onebot_get_msg",
    "onebot_handle_group_request",
    "onebot_mark_msg_as_read",
    "onebot_recall_message",
    "onebot_set_essence_msg",
    "onebot_set_msg_emoji_like",
})

_PACKET_TOOL_NAMES = frozenset({
    "onebot_cancel_group_todo",
    "onebot_complete_group_todo",
    "onebot_get_group_file_url",
    "onebot_get_unidirectional_friend_list",
    "onebot_move_group_file",
    "onebot_nc_get_user_status",
    "onebot_rename_group_file",
    "onebot_set_group_sign",
    "onebot_set_group_todo",
    "onebot_trans_group_file",
})

_TOOL_CAVEATS = {
    "onebot_delete_essence_msg": "NapCat 4.18.13 对合成精华消息 ID 的回退路径可能交换 seq/random 参数",
    "onebot_get_group_files_by_folder": "NapCat 4.18.13 只返回文件，folders 固定为空数组",
    "onebot_get_group_shut_list": "NapCat 查询失败或超时时同样返回空数组",
    "onebot_set_group_sign": "依赖 Packet backend，返回成功仅表示签到包已发送",
    "onebot_trans_group_file": "NapCat 4.18.13 会额外要求 Packet backend 可用",
}


def default_tool_permission(name: str) -> str:
    return "admin" if name in _DEFAULT_ADMIN_TOOL_NAMES else "everyone"


def tool_scope(name: str, schema: dict[str, Any]) -> str:
    if name in _ACCOUNT_TOOL_NAMES:
        return "account"
    properties = schema.get("parameters", {}).get("properties", {})
    if "group_id" in properties or name in _GROUP_CONTEXT_TOOL_NAMES:
        return "group"
    return "general"


def tool_category(name: str) -> str:
    if "group_file" in name or name == "onebot_upload_file":
        return "群文件"
    if "album" in name:
        return "群相册"
    if "group_todo" in name:
        return "群待办"
    if "essence" in name or "group_notice" in name:
        return "精华与公告"
    if "emoji_like" in name or "emoji_likes" in name:
        return "消息表情"
    if "collection" in name:
        return "收藏"
    if any(part in name for part in ("friend", "custom_face", "profile", "avatar", "signature", "online")):
        return "好友与账号"
    if "group" in name:
        return "群聊"
    if any(part in name for part in ("send", "msg", "poke", "recall", "forward")):
        return "消息"
    return "基础"


def tool_metadata(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "default_registered": True,
        "default_permission": default_tool_permission(name),
        "category": tool_category(name),
        "scope": tool_scope(name, schema),
        "packet": name in _PACKET_TOOL_NAMES,
        "caveat": _TOOL_CAVEATS.get(name),
    }


def _load_tool_policies() -> dict[str, dict[str, Any]]:
    """Read Hermes-only policy at plugin startup; malformed entries fall back safely."""
    try:
        from hermes_cli.config import load_config

        config = load_config()
        plugins = config.get("plugins", {}) if isinstance(config, dict) else {}
        entries = plugins.get("entries", {}) if isinstance(plugins, dict) else {}
        onebot = entries.get("onebot", {}) if isinstance(entries, dict) else {}
        raw = onebot.get("tool_policies", {}) if isinstance(onebot, dict) else {}
        if not isinstance(raw, dict):
            return {}
        return {str(name): policy for name, policy in raw.items() if isinstance(policy, dict)}
    except Exception as exc:
        logger.warning("Failed to load OneBot tool policies, using defaults: %s", exc)
        return {}


def _admin_context() -> tuple[bool, str, bool]:
    ctx = _msg_context.get()
    if ctx is None:
        return False, "", False
    is_admin = bool(ctx[0])
    group_id = str(ctx[1] or "")
    # Compatibility with older adapter frames: an admin in DM can only be a
    # global admin, while group contexts require the explicit fourth field.
    is_global_admin = bool(ctx[3]) if len(ctx) > 3 else bool(is_admin and not group_id)
    return is_admin, group_id, is_global_admin


def _permission_error(name: str, schema: dict[str, Any], args: dict[str, Any], permission: str) -> str | None:
    if permission == "everyone":
        return None
    is_admin, current_group_id, is_global_admin = _admin_context()
    if is_global_admin:
        return None
    if not is_admin:
        return "此工具需要管理员权限"
    scope = tool_scope(name, schema)
    message_type = str(args.get("message_type") or "")
    if message_type and message_type != "group":
        scope = "account"
    if name == "onebot_forward_single_msg" and args.get("user_id") and not args.get("group_id"):
        scope = "account"
    if scope != "group":
        return "此账号级工具仅允许全局管理员调用"
    if not current_group_id:
        return "群管理员只能在当前群调用此工具"
    target_group_id = str(args.get("group_id") or current_group_id)
    if target_group_id != current_group_id:
        return "群管理员不能操作其他群"
    return None


def _wrap_hermes_handler(
    name: str, handler: Callable, schema: dict[str, Any], permission: str,
) -> Callable:
    @functools.wraps(handler)
    async def wrapped(args: dict, **kwargs: Any) -> str:
        error = _permission_error(name, schema, args, permission)
        if error:
            return tool_error(error)
        token = _tool_authorized.set(True)
        try:
            return await handler(args, **kwargs)
        finally:
            _tool_authorized.reset(token)

    return wrapped


def register_tools(ctx) -> None:
    """Register the globally visible Hermes tools with execution-time policy wrappers."""
    policies = _load_tool_policies()
    for name, handler, schema in _TOOLS:
        policy = policies.get(name, {})
        registered = policy.get("registered", True)
        permission = policy.get("permission", default_tool_permission(name))
        if not isinstance(registered, bool):
            registered = True
        if not registered:
            logger.info("OneBot tool hidden by policy: %s", name)
            continue
        if permission not in {"everyone", "admin"}:
            permission = default_tool_permission(name)
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=_wrap_hermes_handler(name, handler, schema, permission),
            is_async=True,
            description=schema["description"],
            emoji="🐧",
        )

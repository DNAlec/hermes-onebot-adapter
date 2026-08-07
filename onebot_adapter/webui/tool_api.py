"""Typed HTTP facade for the plugin-bundled OneBot tool catalog."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp.web
from pydantic import BaseModel, ConfigDict, ValidationError, create_model, model_validator

from onebot_adapter.config import ConfigStore
from onebot_adapter.hermes_plugin.onebot_tools import _TOOLS, _api_caller, _msg_context

logger = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_dependent_fields(self):
        fields = type(self).model_fields
        if "message_type" in fields and {"group_id", "user_id"} <= fields.keys():
            message_type = self.message_type
            group_id = self.group_id
            user_id = self.user_id
            if message_type == "group":
                if group_id is None or user_id is not None:
                    raise ValueError("message_type=group requires group_id and forbids user_id")
            elif message_type == "private":
                if user_id is None or group_id is not None:
                    raise ValueError("message_type=private requires user_id and forbids group_id")
            else:
                raise ValueError("message_type must be 'group' or 'private'")
        if {"real_seq", "all"} <= fields.keys():
            real_seq = self.real_seq
            mark_all = self.all
            if (real_seq is None) == (mark_all is not True):
                raise ValueError("provide exactly one of real_seq or all=true")
            if real_seq is not None and real_seq <= 0:
                raise ValueError("real_seq must be positive")
        return self


def _annotation(prop: dict[str, Any]) -> Any:
    kind = prop.get("type")
    if kind == "integer":
        return int
    if kind == "boolean":
        return bool
    if kind == "array":
        item_kind = prop.get("items", {}).get("type")
        if item_kind == "string":
            return list[str]
        if item_kind == "integer":
            return list[int]
        return list[dict[str, Any]]
    if kind == "object":
        return dict[str, Any]
    return str


def _models() -> dict[str, type[BaseModel]]:
    result: dict[str, type[BaseModel]] = {}
    for name, _handler, schema in _TOOLS:
        parameters = schema["parameters"]
        required = set(parameters.get("required", []))
        fields: dict[str, Any] = {}
        for field_name, prop in parameters.get("properties", {}).items():
            annotation = _annotation(prop)
            fields[field_name] = (annotation, ...) if field_name in required else (annotation | None, None)
        result[name] = create_model(
            "".join(part.title() for part in name.split("_")) + "Request",
            __base__=_StrictModel,
            **fields,
        )
    return result


TOOL_MODELS = _models()
TOOL_MAP = {name: (handler, schema) for name, handler, schema in _TOOLS}


def key_matches(raw: str, expected_hash: str) -> bool:
    if not raw or not expected_hash:
        return False
    return hmac.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), expected_hash)


def _error(code: str, message: str, status: int, *, details: Any = None) -> aiohttp.web.Response:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return aiohttp.web.json_response(body, status=status)


def _validate_file_ref(value: str, roots: list[str]) -> None:
    parsed = urlparse(value)
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"}:
            raise PermissionError("only http(s) URLs are allowed")
        return
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise PermissionError("local file path must be absolute")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise PermissionError("local file must be a regular file")
    allowed = False
    for root in roots:
        try:
            resolved.relative_to(Path(root).expanduser().resolve(strict=False))
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise PermissionError("local file is outside automation_upload_allowed_roots")


def _validate_file_refs(value: Any, roots: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"file", "image"} and isinstance(child, str):
                _validate_file_ref(child, roots)
            else:
                _validate_file_refs(child, roots)
    elif isinstance(value, list):
        for child in value:
            _validate_file_refs(child, roots)


async def _call_tool(
    name: str, args: dict[str, Any], state: dict[str, Any],
) -> Any:
    api = state.get("api")
    if api is None:
        raise RuntimeError("OneBot API is not ready")
    if getattr(api, "connected", True) is False:
        raise RuntimeError("OneBot WS is not connected")
    relay = state.get("relay")
    local_api_call = state.get("local_api_call")

    async def caller(action: str, params: dict[str, Any]) -> Any:
        if action.startswith("adapter_"):
            if local_api_call is None:
                raise RuntimeError("adapter action service is not ready")
            return await local_api_call(action, params)
        if relay is not None:
            params = relay._resolve_seq_params(action, dict(params))
        response = await api.call(action, params)
        return response.get("data")

    handler, _schema = TOOL_MAP[name]
    caller_token = _api_caller.set(caller)
    context_token = _msg_context.set((True, str(args.get("group_id") or ""), str(args.get("user_id") or ""), True))
    try:
        raw = await handler(args)
    finally:
        _msg_context.reset(context_token)
        _api_caller.reset(caller_token)
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(decoded, dict):
        # Hermes' tool_error() emits {"error": ...} without a success field.
        # Also accept the legacy {"success": false, "error": ...} shape and
        # non-string handler results defensively.
        if "error" in decoded or decoded.get("success") is False:
            raise ValueError(str(decoded.get("error", "tool call failed")))
        # Backward compatibility for results produced by older installed
        # plugin files; current tool_result() returns the data directly.
        if decoded.get("success") is True and "data" in decoded:
            return decoded["data"]
    return decoded


def _tool_handler(name: str, store: ConfigStore, state: dict[str, Any]):
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid_json", "request body must be valid JSON", 400)
        try:
            args = TOOL_MODELS[name].model_validate(payload).model_dump(exclude_none=True)
        except ValidationError as exc:
            return _error(
                "validation_error",
                "request validation failed",
                400,
                details=exc.errors(include_context=False),
            )
        try:
            _validate_file_refs(args, store.config.automation_upload_allowed_roots)
            result = await _call_tool(name, args, state)
        except PermissionError as exc:
            return _error("file_not_allowed", str(exc), 403)
        except RuntimeError as exc:
            return _error("onebot_unavailable", str(exc), 503)
        except Exception:
            logger.exception("automation tool failed: %s", name)
            return _error("tool_call_failed", "OneBot tool call failed", 500)
        return aiohttp.web.json_response({"ok": True, "data": result})

    return handler


def add_tool_routes(
    app: aiohttp.web.Application, store: ConfigStore, state: dict[str, Any],
) -> None:
    async def catalog(_: aiohttp.web.Request) -> aiohttp.web.Response:
        tools = [
            {
                "name": name,
                "description": schema["description"],
                "parameters": TOOL_MODELS[name].model_json_schema(),
            }
            for name, _handler, schema in _TOOLS
        ]
        return aiohttp.web.json_response({"tools": tools})

    app.router.add_get("/api/v1/tools", catalog)
    for name in TOOL_MAP:
        app.router.add_post(f"/api/v1/tools/{name}", _tool_handler(name, store, state))

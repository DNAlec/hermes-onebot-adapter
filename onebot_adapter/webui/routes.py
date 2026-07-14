"""WebUI backend HTTP API + static SPA hosting."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web

from onebot_adapter import __version__
from onebot_adapter.config import AdapterConfig, ConfigStore, GroupConfig, save_config
from onebot_adapter.relay.protocol import parse_chat_id

logger = logging.getLogger(__name__)

# Search paths for built SPA assets, checked in order:
# 1. source-tree static dir (e.g. pip install -e . or editable installs)
# 2. frontend/dist after manual npm build
# We deliberately do NOT scan ``sys.path`` for static dirs — that would allow
# a malicious ``onebot_adapter/webui/static/index.html`` in the current working
# directory (or an attacker-controlled PYTHONPATH entry) to shadow the real
# SPA. Only the package's own location and the explicit frontend/dist
# fallback are trusted.
_PKG_ROOT = Path(__file__).parent.parent
_WEBUI_STATIC = Path(__file__).parent / "static"
_STATIC_CANDIDATES = [
    _WEBUI_STATIC,
    _PKG_ROOT.parent / "frontend" / "dist",
]


def _find_static() -> Path | None:
    for p in _STATIC_CANDIDATES:
        if (p / "index.html").exists():
            return p
    return None


_STATIC_DIR = _find_static() or _WEBUI_STATIC

# ── /api/login rate limiting (in-memory, per-IP) ─────────────────────────
_LOGIN_MAX_FAILS = 5
_LOGIN_BAN_SECONDS = 900  # 15 minutes


def add_routes(app: aiohttp.web.Application, store: ConfigStore, state: dict[str, Any]) -> None:
    app.middlewares.append(_make_auth_middleware(store))
    app.router.add_get("/api/health", _health)
    app.router.add_post("/api/login", _login(store, state))
    app.router.add_get("/api/status", _status(store, state))
    app.router.add_get("/api/config", _get_config(store))
    app.router.add_put("/api/config", _put_config(store, state))
    app.router.add_get("/api/hermes_dir_status", _hermes_dir_status(store))
    app.router.add_post("/api/install_plugin", _install_plugin(store, state))
    app.router.add_post("/api/uninstall_plugin", _uninstall_plugin(state))
    app.router.add_post("/api/send", _send(store, state))
    app.router.add_get("/api/logs", _logs(state))
    # Group management
    app.router.add_get("/api/groups", _get_groups(store))
    app.router.add_put("/api/groups/{group_id}", _put_group(store))
    app.router.add_delete("/api/groups/{group_id}", _delete_group(store))
    app.router.add_post("/api/groups/sync", _sync_groups(store, state))
    # Command filter
    app.router.add_get("/api/commands", _get_commands(state))
    app.router.add_post("/api/commands/refresh", _refresh_commands(state))
    # Hermes tools management (OneBot platform)
    app.router.add_get("/api/hermes_tools", _get_hermes_tools(store))
    app.router.add_put("/api/hermes_tools", _put_hermes_tools(store))
    app.router.add_post("/api/hermes_tools/reset", _reset_hermes_tools(store))
    # Hermes session-isolation mode (group_sessions_per_user)
    app.router.add_get("/api/hermes_mode", _get_hermes_mode(store, state))
    app.router.add_put("/api/hermes_mode", _put_hermes_mode(store))
    app.router.add_post("/api/hermes_mode/refresh", _refresh_hermes_mode(state))
    app.router.add_get("/api/update_check", _update_check)
    app.router.add_get("/", _index)
    app.router.add_get("/{tail:.*}", _spa_handler)


async def _health(_: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.json_response({"status": "ok"})


def _login(store: ConfigStore, state: dict[str, Any]):
    """POST /api/login — exchange the raw webui_token for a signed session token.

    Request body: ``{"token": "<raw webui_token>"}``.
    Response: ``{"session_token": "<signed token>", "expires_in": <seconds>}``.

    Rate-limited per client IP: after ``_LOGIN_MAX_FAILS`` failed attempts
    within ``_LOGIN_BAN_SECONDS``, the IP is banned for the remainder of the
    window and receives ``429 Too Many Requests``. State lives in
    ``state["login_failures"]`` (in-memory; cleared on restart).

    The client IP is taken from ``X-Forwarded-For`` (first hop) when present
    so that deployments behind a reverse proxy rate-limit the *real* client
    rather than the shared proxy IP. Without ``X-Forwarded-For`` the direct
    ``request.remote`` is used.
    """
    failures: dict[str, tuple[int, float]] = state.setdefault("login_failures", {})

    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        # Prefer the first X-Forwarded-For hop for reverse-proxy deployments;
        # fall back to the direct peer for non-proxied setups.
        xff = request.headers.get("X-Forwarded-For", "")
        ip = xff.split(",")[0].strip() if xff.strip() else (request.remote or "unknown")
        now = time.time()
        # Garbage-collect expired entries to keep the dict bounded.
        for k in list(failures):
            fails, first_ts = failures[k]
            if fails < _LOGIN_MAX_FAILS and now - first_ts > _LOGIN_BAN_SECONDS:
                del failures[k]
        # Check ban
        entry = failures.get(ip)
        if entry and entry[0] >= _LOGIN_MAX_FAILS and now - entry[1] < _LOGIN_BAN_SECONDS:
            retry_after = int(_LOGIN_BAN_SECONDS - (now - entry[1])) + 1
            return aiohttp.web.json_response(
                {"error": "too many attempts", "retry_after": retry_after}, status=429,
            )
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
        token = data.get("token", "")
        if not token:
            return aiohttp.web.json_response({"error": "token required"}, status=401)
        cfg = store.config
        if not cfg.webui_token:
            return aiohttp.web.json_response(
                {"error": "webui_token not configured — restart the adapter service to regenerate it"},
                status=401,
            )
        if token != cfg.webui_token:
            fails, first_ts = failures.get(ip, (0, now))
            failures[ip] = (fails + 1, first_ts)
            return aiohttp.web.json_response({"error": "invalid token"}, status=401)
        # Success: clear this IP's counter.
        failures.pop(ip, None)
        issued_at = int(time.time())
        msg = f"{cfg.webui_token_epoch}:{issued_at}".encode()
        sig = hmac.new(cfg.webui_token.encode(), msg, hashlib.sha256).hexdigest()
        payload = f"{issued_at}.{sig}".encode()
        session_token = base64.urlsafe_b64encode(payload).decode("ascii")
        return aiohttp.web.json_response({
            "session_token": session_token,
            "expires_in": cfg.webui_token_lifetime_hours * 3600,
        })

    return handler


def _extract_token(request: aiohttp.web.Request) -> str:
    """Extract a bearer token from Authorization header or query param."""
    token = request.headers.get("Authorization", "")
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return request.query.get("token", "")


def _verify_session_token(token: str, secret: str, epoch: int, lifetime_hours: int) -> bool:
    """Verify an HMAC-signed session token issued by ``/api/login``.

    Token format: ``base64url("{issued_at}.{hmac_hex}")`` where the HMAC is
    computed over ``f"{epoch}:{issued_at}"`` using *secret* as the key. Returns
    ``True`` when the signature matches and the token has not expired.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode("utf-8")
        issued_str, sig = raw.split(".", 1)
        issued_at = int(issued_str)
    except Exception:
        return False
    if time.time() - issued_at > lifetime_hours * 3600:
        return False
    msg = f"{epoch}:{issued_at}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _make_auth_middleware(store: ConfigStore):
    """aiohttp middleware: require a valid signed session token for all /api/* requests.

    ``/api/health`` and ``/api/login`` are exempt (public health check + login
    endpoint). Static-file routes (``/``, ``/{tail:.*}``) are outside ``/api/``
    and thus unaffected — the SPA shell and login page always load without auth.

    The token must be an HMAC-signed session token issued by ``/api/login``;
    the raw ``webui_token`` is never accepted by other endpoints.
    """
    @aiohttp.web.middleware
    async def auth_middleware(request: aiohttp.web.Request, handler):
        if request.path.startswith("/api/") and request.path not in ("/api/health", "/api/login"):
            token = _extract_token(request)
            cfg = store.config
            ok = _verify_session_token(
                token, cfg.webui_token, cfg.webui_token_epoch, cfg.webui_token_lifetime_hours,
            )
            if not ok:
                return aiohttp.web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)
    return auth_middleware


def _status(store: ConfigStore, state: dict[str, Any]):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        cfg = store.config
        relay = state.get("relay")
        per_user = getattr(relay, "hermes_group_sessions_per_user", True) if relay else True
        plugin_ver = getattr(relay, "plugin_version", None) if relay else None
        mismatch = getattr(relay, "version_mismatch", True) if relay else True
        return aiohttp.web.json_response(
            {
                "adapter_version": __version__,
                "plugin_version": plugin_ver,
                "version_mismatch": mismatch,
                "onebot_connected": bool(state.get("onebot_connected")),
                "hermes_plugin_connected": bool(state.get("hermes_plugin_connected")),
                "onebot_mode": cfg.onebot_mode,
                "self_id": cfg.self_id,
                "onebot_ws_port": cfg.onebot_reverse_ws_port,
                "hermes_ws_port": cfg.hermes_ws_port,
                "webui_port": cfg.webui_port,
                "hermes_group_sessions_per_user": per_user,
            }
        )

    return handler


def _hermes_dir_status(store: ConfigStore):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        from onebot_adapter.installer import _resolve_hermes_dir

        cfg = store.config
        hermes_dir = _resolve_hermes_dir(cfg.hermes_install_dir or None)
        return aiohttp.web.json_response(
            {
                "hermes_dir": str(hermes_dir),
                "exists": hermes_dir.exists(),
            }
        )

    return handler


def _public_config(cfg: AdapterConfig) -> dict[str, Any]:
    """Config dict with the raw ``webui_token`` scrubbed for API responses.

    The WebUI login token is the password to this very UI and must never be
    readable over the API — verify it through ``POST /api/login`` instead.
    Other tokens (onebot_ws_token, hermes_ws_token) are operational values
    the user needs to see/copy in the WebUI and are returned as-is.
    """
    d = cfg.to_dict()
    d.pop("webui_token", None)
    return d


def _get_config(store: ConfigStore):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response(_public_config(store.config))

    return handler


def _put_config(store: ConfigStore, state: dict[str, Any]):
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
        try:
            # Bump the token epoch when the lifetime changes so that every
            # already-issued HMAC session token becomes invalid immediately.
            if "webui_token_lifetime_hours" in data and \
                    data["webui_token_lifetime_hours"] != store.config.webui_token_lifetime_hours:
                data["webui_token_epoch"] = store.config.webui_token_epoch + 1
            new_cfg = AdapterConfig.from_dict({**store.config.to_dict(), **data})
            errors = new_cfg.validate()
            if errors:
                return aiohttp.web.json_response({"error": "; ".join(errors)}, status=400)
            store.update(new_cfg)
            save_config(new_cfg)
        except Exception as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=500)
        return aiohttp.web.json_response(_public_config(new_cfg))

    return handler


def _install_plugin(store: ConfigStore, state: dict[str, Any]):
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
        install_dir = data.get("hermes_install_dir")
        cfg = store.config
        from onebot_adapter.installer import _resolve_hermes_dir

        target = _resolve_hermes_dir(install_dir)
        adapter_url = f"ws://127.0.0.1:{cfg.hermes_ws_port}{cfg.hermes_ws_path}"
        adapter_token = cfg.hermes_ws_token
        from onebot_adapter import installer

        try:
            result = installer.install(
                str(target),
                adapter_url=adapter_url,
                adapter_token=adapter_token,
            )
            return aiohttp.web.json_response(result)
        except Exception as exc:
            logger.exception("plugin install failed")
            return aiohttp.web.json_response({"error": str(exc)}, status=500)

    return handler


def _uninstall_plugin(state: dict[str, Any]):
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
        install_dir = data.get("hermes_install_dir")
        from onebot_adapter.installer import _resolve_hermes_dir

        target = _resolve_hermes_dir(install_dir)
        from onebot_adapter import installer

        try:
            result = installer.uninstall(str(target))
            return aiohttp.web.json_response(result)
        except Exception as exc:
            logger.exception("plugin uninstall failed")
            return aiohttp.web.json_response({"error": str(exc)}, status=500)

    return handler


def _send(store: ConfigStore, state: dict[str, Any]):
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
        chat_id = data.get("chat_id", "")
        message = data.get("message", "")
        if not chat_id or not message:
            return aiohttp.web.json_response({"error": "chat_id and message required"}, status=400)
        api = state.get("api")
        if api is None:
            return aiohttp.web.json_response({"error": "adapter not ready"}, status=503)
        try:
            from onebot_adapter.onebot.api import text_segment

            is_group, num_id = parse_chat_id(chat_id)
            segs = [text_segment(message)]
            if is_group:
                resp = await api.send_group_msg(num_id, segs)
            else:
                resp = await api.send_private_msg(num_id, segs)
            return aiohttp.web.json_response({"success": True, "message_id": str(resp.get("message_id", ""))})
        except Exception as exc:
            logger.exception("send failed")
            return aiohttp.web.json_response({"error": str(exc)}, status=500)

    return handler


def _logs(state: dict[str, Any]):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        records = list(state.get("log_buffer", []))
        return aiohttp.web.json_response({"logs": records})

    return handler


# ── Command filter ───────────────────────────────────────────────────────


def _get_commands(state: dict[str, Any]):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        relay = state.get("relay")
        commands: list[dict[str, Any]] = []
        if relay is not None:
            commands = relay.commands  # type: ignore[attr-defined]
        return aiohttp.web.json_response({"commands": commands})

    return handler


def _refresh_commands(state: dict[str, Any]):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        relay = state.get("relay")
        if relay is None:
            return aiohttp.web.json_response({"error": "relay not ready"}, status=503)
        # Ask each connected Hermes plugin client to re-push a snapshot.
        try:
            await relay.broadcast_commands_refresh()  # type: ignore[attr-defined]
        except Exception as exc:
            logger.exception("commands refresh failed")
            return aiohttp.web.json_response({"error": str(exc)}, status=500)
        return aiohttp.web.json_response({"sent": True})

    return handler


# ── Group management ─────────────────────────────────────────────────────


def _get_groups(store: ConfigStore):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        cfg = store.config
        groups = []
        for _gid, raw in cfg.groups.items():
            gc = GroupConfig.from_dict(raw)
            groups.append(gc.to_dict())
        return aiohttp.web.json_response({"groups": groups})

    return handler


def _put_group(store: ConfigStore):
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        group_id = request.match_info.get("group_id", "")
        if not group_id:
            return aiohttp.web.json_response({"error": "group_id required"}, status=400)
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
        gc = GroupConfig.from_dict({**data, "group_id": str(group_id)})
        cfg = store.config
        new_groups = {**cfg.groups, str(group_id): gc.to_dict()}
        store.patch(groups=new_groups)
        save_config(store.config)
        return aiohttp.web.json_response(gc.to_dict())

    return handler


def _delete_group(store: ConfigStore):
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        group_id = request.match_info.get("group_id", "")
        cfg = store.config
        new_groups = {k: v for k, v in cfg.groups.items() if k != str(group_id)}
        store.patch(groups=new_groups)
        save_config(store.config)
        return aiohttp.web.json_response({"deleted": group_id})

    return handler


def _sync_groups(store: ConfigStore, state: dict[str, Any]):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        api = state.get("api")
        if api is None:
            return aiohttp.web.json_response({"error": "OneBot API not available"}, status=503)
        try:
            info = await api.call("get_group_list")
            remote_groups = info.get("data", [])
        except Exception as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=500)
        cfg = store.config
        new_groups = dict(cfg.groups)
        added = []
        for g in remote_groups:
            gid = str(g.get("group_id", ""))
            if not gid or gid in new_groups:
                continue
            gc = GroupConfig(group_id=gid, name=g.get("group_name", ""))
            new_groups[gid] = gc.to_dict()
            added.append(gid)
        if added:
            store.patch(groups=new_groups)
            save_config(store.config)
        return aiohttp.web.json_response({"added": added, "total": len(new_groups)})

    return handler


# ── Hermes tools management (OneBot platform) ─────────────────────────────


def _get_hermes_tools(store: ConfigStore):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        from onebot_adapter.hermes_config import (
            list_available_toolsets,
            read_current_enabled,
            resolve_hermes_config_path,
        )

        cfg = store.config
        config_path = resolve_hermes_config_path(cfg.hermes_install_dir or None)
        if config_path is None:
            return aiohttp.web.json_response(
                {"error": "hermes_install_dir 未配置或目录不存在,请先在插件管理页配置"},
                status=400,
            )
        available = list_available_toolsets(cfg.hermes_install_dir or None)
        if "error" in available:
            return aiohttp.web.json_response(available, status=500)
        current = read_current_enabled(cfg.hermes_install_dir or None)
        return aiohttp.web.json_response({
            "configurable": available.get("configurable", []),
            "mcp_servers": available.get("mcp_servers", []),
            "current_enabled": current,
            "hermes_dir_ok": True,
        })

    return handler


def _put_hermes_tools(store: ConfigStore):
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        from onebot_adapter.hermes_config import (
            NO_MCP_SENTINEL,
            PLATFORM,
            PLUGIN_TOOLSET_KEY,
            list_available_toolsets,
            resolve_hermes_config_path,
            write_platform_toolsets,
        )

        cfg = store.config
        config_path = resolve_hermes_config_path(cfg.hermes_install_dir or None)
        if config_path is None:
            return aiohttp.web.json_response(
                {"error": "hermes_install_dir 未配置或目录不存在,请先在插件管理页配置"},
                status=400,
            )
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)

        toolsets = data.get("toolsets", []) or []
        mcp_servers = data.get("mcp_servers", []) or []
        no_mcp = bool(data.get("no_mcp", False))

        # 校验:每个 key 必须在 configurable ∪ plugin_keys ∪ mcp_names 中
        available = list_available_toolsets(cfg.hermes_install_dir or None)
        if "error" in available:
            return aiohttp.web.json_response(available, status=500)
        valid_keys: set[str] = set()
        for item in available.get("configurable", []):
            valid_keys.add(item["key"])
        for item in available.get("mcp_servers", []):
            valid_keys.add(item["name"])
        valid_keys.add(PLUGIN_TOOLSET_KEY)
        # NO_MCP_SENTINEL is intentionally NOT in valid_keys — it must only
        # enter via the no_mcp flag, never as a toolset key.

        invalid = [k for k in (list(toolsets) + list(mcp_servers)) if k not in valid_keys]
        if invalid:
            return aiohttp.web.json_response(
                {"error": f"无效的工具集 key: {invalid}"}, status=400,
            )

        final = sorted(set(str(k) for k in toolsets) | set(str(k) for k in mcp_servers))
        if no_mcp:
            final = sorted(set(final) | {NO_MCP_SENTINEL})

        try:
            write_platform_toolsets(cfg.hermes_install_dir or None, final)
        except FileNotFoundError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("write platform_toolsets failed")
            return aiohttp.web.json_response({"error": str(exc)}, status=500)
        return aiohttp.web.json_response({"ok": True, "saved": final, "platform": PLATFORM})

    return handler


def _reset_hermes_tools(store: ConfigStore):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        from onebot_adapter.hermes_config import reset_platform_toolsets, resolve_hermes_config_path

        cfg = store.config
        config_path = resolve_hermes_config_path(cfg.hermes_install_dir or None)
        if config_path is None:
            return aiohttp.web.json_response(
                {"error": "hermes_install_dir 未配置或目录不存在,请先在插件管理页配置"},
                status=400,
            )
        try:
            reset_platform_toolsets(cfg.hermes_install_dir or None)
        except Exception as exc:
            logger.exception("reset platform_toolsets failed")
            return aiohttp.web.json_response({"error": str(exc)}, status=500)
        return aiohttp.web.json_response({"ok": True})

    return handler


# ── Hermes session-isolation mode (group_sessions_per_user) ──────────────


def _get_hermes_mode(store: ConfigStore, state: dict[str, Any]):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        cfg = store.config
        # 当前生效值:来自插件上报(relay 缓存);未连接时回退读 Hermes config.yaml
        relay = state.get("relay")
        reported = getattr(relay, "hermes_group_sessions_per_user", None) if relay else None
        if reported is not None:
            return aiohttp.web.json_response({
                "group_sessions_per_user": reported,
                "source": "plugin_report",
                "plugin_connected": bool(relay and relay.has_clients),
            })
        # 插件未上报:回退读 Hermes config.yaml 顶层值
        from onebot_adapter.hermes_config import read_group_sessions_per_user

        try:
            file_value = read_group_sessions_per_user(cfg.hermes_install_dir or None)
        except Exception as exc:
            return aiohttp.web.json_response(
                {"error": f"读取 Hermes config.yaml 失败: {exc}"}, status=500,
            )
        # 文件不存在该字段时按 Hermes 默认 True 处理
        return aiohttp.web.json_response({
            "group_sessions_per_user": True if file_value is None else file_value,
            "source": "hermes_config_yaml" if file_value is not None else "default",
            "plugin_connected": False,
        })

    return handler


def _put_hermes_mode(store: ConfigStore):
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
        value = data.get("group_sessions_per_user")
        if not isinstance(value, bool):
            return aiohttp.web.json_response(
                {"error": "group_sessions_per_user 必须是布尔值"}, status=400,
            )
        cfg = store.config
        from onebot_adapter.hermes_config import write_group_sessions_per_user

        try:
            write_group_sessions_per_user(cfg.hermes_install_dir or None, value)
        except FileNotFoundError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("write group_sessions_per_user failed")
            return aiohttp.web.json_response({"error": str(exc)}, status=500)
        return aiohttp.web.json_response({
            "ok": True,
            "written": value,
            "restart_required": True,
            "note": "已写入 Hermes config.yaml,需重启 Hermes 网关生效。重启后请点击'刷新上报值'更新显示。",
        })

    return handler


def _refresh_hermes_mode(state: dict[str, Any]):
    async def handler(_: aiohttp.web.Request) -> aiohttp.web.Response:
        relay = state.get("relay")
        if not relay:
            return aiohttp.web.json_response({"error": "relay 未就绪"}, status=503)
        if not relay.has_clients:
            return aiohttp.web.json_response({
                "ok": False,
                "error": "Hermes 插件未连接,无法刷新。请先确保插件已连接。",
            })
        try:
            await relay.broadcast_mode_refresh()
        except Exception as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=500)
        return aiohttp.web.json_response({"ok": True, "note": "已请求插件重新上报,稍后刷新页面查看"})

    return handler


async def _update_check(_: aiohttp.web.Request) -> aiohttp.web.Response:
    from onebot_adapter.update_check import check_for_updates

    try:
        result = await check_for_updates()
    except Exception as exc:
        logger.exception("update check failed")
        return aiohttp.web.json_response({"error": str(exc)}, status=500)
    return aiohttp.web.json_response(result)


async def _index(_: aiohttp.web.Request) -> aiohttp.web.Response:
    index = _STATIC_DIR / "index.html"
    if index.exists():
        return aiohttp.web.FileResponse(index)
    return aiohttp.web.Response(text=_PLACEHOLDER_HTML, content_type="text/html")


async def _spa_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
    tail = request.match_info.get("tail", "")
    clean = tail.lstrip("/")
    file_path = _STATIC_DIR / clean
    # ``is_relative_to`` is the real path-traversal guard; it rejects any
    # ``..``-based escape (decoded by aiohttp's match_info) that would land
    # outside the static dir. No extra ``".." in clean`` check is needed.
    if file_path.exists() and file_path.is_file() and file_path.is_relative_to(_STATIC_DIR):
        return aiohttp.web.FileResponse(file_path)
    return await _index(request)


_PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Hermes OneBot Adapter</title>
<style>
body{font-family:system-ui,sans-serif;max-width:720px;margin:3rem auto;padding:0 1rem;color:#222}
h1{font-size:1.4rem}
code{background:#f4f4f4;padding:.1rem .3rem;border-radius:4px}
.card{border:1px solid #eee;border-radius:8px;padding:1rem;margin:1rem 0}
.muted{color:#888;font-size:.85rem}
</style></head><body>
<h1>Hermes OneBot Adapter</h1>
<p class="muted">P0 skeleton &mdash; SPA 尚未构建，API 已就绪。</p>
<div class="card"><strong>API</strong><ul>
<li><code>GET /api/status</code></li>
<li><code>GET /api/config</code> &middot; <code>PUT /api/config</code></li>
<li><code>POST /api/install_plugin</code></li>
<li><code>GET /api/logs</code></li>
</ul></div>
<div class="card"><strong>WebSocket</strong><ul>
<li>OneBot 反向WS: <code>ws://&lt;host&gt;:18800/onebot</code></li>
<li>Hermes 插件: <code>ws://&lt;host&gt;:18810/hermes?token=&lt;hermes_ws_token&gt;</code></li>
</ul></div>
</body></html>"""

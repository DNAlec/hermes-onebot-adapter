"""Hermes config.yaml 读写桥。

适配器通过本模块直接读写 Hermes 安装目录下的 ``config.yaml``,管理 OneBot
平台的工具集配置(``platform_toolsets.onebot`` + ``known_plugin_toolsets.onebot``)。

- 读写使用 ``ruamel.yaml`` round-trip 模式,保留用户已有的注释和顶层 key 顺序。
- 工具集列表(``list_available_toolsets``)优先用 Hermes 自带的 venv Python 跑子进程
  import ``hermes_cli.tools_config`` / ``toolsets``,彻底绕开适配器自身 Python 环境
  与 Hermes 依赖不匹配的问题(如 PyYAML/tiktoken 等只在 Hermes venv 里)。
  venv 不存在时 fallback 到 ``sys.path`` 方案(pip 安装场景)。
- 写入操作用 tmp + ``os.replace`` 原子写,避免 Hermes 网关读到半写状态。
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from onebot_adapter.installer import _resolve_hermes_dir

logger = logging.getLogger(__name__)

PLATFORM = "onebot"
PLUGIN_TOOLSET_KEY = "onebot"  # 与 onebot_tools.py 的 TOOLSET 常量一致
NO_MCP_SENTINEL = "no_mcp"


# ── 路径解析 ──────────────────────────────────────────────────────────────


def resolve_hermes_config_path(hermes_install_dir: str | None) -> Path | None:
    """返回 ``<hermes_dir>/config.yaml`` 的路径;目录不存在返回 None。

    目录存在但 config.yaml 不存在时,返回路径对象(调用方据此决定是新建还是报错)。
    """
    hermes_dir = _resolve_hermes_dir(hermes_install_dir)
    if not hermes_dir.exists():
        return None
    return hermes_dir / "config.yaml"


# ── YAML 读写 ──────────────────────────────────────────────────────────────


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


class HermesConfigParseError(Exception):
    """config.yaml 存在但解析失败时抛出,防止写入逻辑覆盖损坏的原始文件。"""


@contextmanager
def _locked(config_path: Path):
    """对 config.yaml 加文件锁(进程级),避免与 Hermes 网关并发写互相覆盖。

    使用 ``fcntl.flock`` 阻塞式排他锁,作用域为本上下文。Linux/macOS 可用;
    Windows 无 flock 但有 msvcrt,此处仅支持 POSIX(适配器部署目标)。
    """
    import fcntl

    lock_path = config_path.with_suffix(config_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def read_config(hermes_install_dir: str | None) -> Any:
    """Round-trip load Hermes config.yaml;不存在返回空 dict-like。

    返回 ruamel.yaml 的 CommentedMap(支持注释保留);文件不存在时返回空 CommentedMap。

    文件存在但解析失败时抛 :class:`HermesConfigParseError`,避免调用方(写入逻辑)
    基于空数据覆盖用户的(可恢复的)损坏 YAML。
    """
    yaml = _yaml()
    config_path = resolve_hermes_config_path(hermes_install_dir)
    if config_path is None or not config_path.exists():
        from ruamel.yaml.comments import CommentedMap

        return CommentedMap()
    try:
        return yaml.load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Hermes config.yaml 解析失败 (%s): %s", config_path, exc)
        raise HermesConfigParseError(
            f"Hermes config.yaml 解析失败 ({config_path}): {exc}"
        ) from exc


def _atomic_write(config_path: Path, content: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, config_path)


def _read_modify_write(
    hermes_install_dir: str | None,
    *,
    modify: Any,
) -> None:
    """在文件锁保护下完成读取-修改-写入,避免 TOCTOU 丢失更新。

    *modify* 是一个接收已解析的 ``data`` (CommentedMap) 并就地修改它的 callable。
    整个 read-modify-write 都在 ``_locked`` 作用域内,保证 Hermes 网关与适配器
    的并发写入不会互相覆盖。

    当目录不存在时抛 ``FileNotFoundError``;解析失败抛 ``HermesConfigParseError``。
    """
    config_path = resolve_hermes_config_path(hermes_install_dir)
    if config_path is None:
        raise FileNotFoundError(
            f"Hermes 安装目录未配置或不存在: {hermes_install_dir!r}; "
            "请先在 WebUI 设置 hermes_install_dir"
        )

    with _locked(config_path):
        # Read inside the lock so no concurrent writer can slip in between
        # our read and our write.
        yaml = _yaml()
        if not config_path.exists():
            from ruamel.yaml.comments import CommentedMap

            data: Any = CommentedMap()
        else:
            try:
                data = yaml.load(config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Hermes config.yaml 解析失败 (%s): %s", config_path, exc)
                raise HermesConfigParseError(
                    f"Hermes config.yaml 解析失败 ({config_path}): {exc}"
                ) from exc

        modify(data)

        buf = io.StringIO()
        yaml.dump(data, buf)
        _atomic_write(config_path, buf.getvalue())


def write_platform_toolsets(hermes_install_dir: str | None, toolsets: list[str]) -> None:
    """写入 ``platform_toolsets.onebot`` 和 ``known_plugin_toolsets.onebot``。

    - ``platform_toolsets.onebot`` = sorted(set(toolsets))
    - ``known_plugin_toolsets.onebot`` 至少含 ``"onebot"``(若已存在则补入,保留其它条目)

    保留 config.yaml 其它顶层 key、注释和顺序。整个 read-modify-write 在文件锁
    保护下完成,避免与 Hermes 网关并发写互相覆盖。解析失败时抛
    :class:`HermesConfigParseError`,不覆盖原始文件。
    """
    def _modify(data: Any) -> None:
        platform_toolsets = data.get("platform_toolsets")
        if platform_toolsets is None:
            from ruamel.yaml.comments import CommentedMap

            platform_toolsets = CommentedMap()
            data["platform_toolsets"] = platform_toolsets
        platform_toolsets[PLATFORM] = sorted(set(toolsets))

        known = data.get("known_plugin_toolsets")
        if known is None:
            from ruamel.yaml.comments import CommentedMap

            known = CommentedMap()
            data["known_plugin_toolsets"] = known
        existing = list(known.get(PLATFORM, []) or [])
        if PLUGIN_TOOLSET_KEY not in existing:
            existing.append(PLUGIN_TOOLSET_KEY)
        known[PLATFORM] = sorted(set(existing))

    _read_modify_write(hermes_install_dir, modify=_modify)


def reset_platform_toolsets(hermes_install_dir: str | None) -> None:
    """删除 ``platform_toolsets.onebot`` 条目(其它平台保留)。

    不删 ``known_plugin_toolsets`` —— 删除 known 会让 Hermes 把 onebot 当"新插件"
    默认启用,与 reset 的"回到未配置状态"语义不符。整个 read-modify-write 在文件锁
    保护下完成。
    """
    config_path = resolve_hermes_config_path(hermes_install_dir)
    if config_path is None or not config_path.exists():
        return

    def _modify(data: Any) -> None:
        platform_toolsets = data.get("platform_toolsets")
        if platform_toolsets is not None and PLATFORM in platform_toolsets:
            del platform_toolsets[PLATFORM]

    _read_modify_write(hermes_install_dir, modify=_modify)


# ── 顶层 group_sessions_per_user 读写(供 WebUI 管理)────────────────────


def read_group_sessions_per_user(hermes_install_dir: str | None) -> bool | None:
    """读取 Hermes ``config.yaml`` 顶层 ``group_sessions_per_user`` 字段。

    返回 ``True`` / ``False``;字段不存在时返回 ``None``(调用方按 Hermes
    默认值 ``True`` 处理,即每用户独立 session)。
    """
    config_path = resolve_hermes_config_path(hermes_install_dir)
    if config_path is None or not config_path.exists():
        return None
    data = read_config(hermes_install_dir)
    if "group_sessions_per_user" not in data:
        return None
    value = data.get("group_sessions_per_user")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value) if value is not None else None


def write_group_sessions_per_user(hermes_install_dir: str | None, value: bool) -> None:
    """写入 Hermes ``config.yaml`` 顶层 ``group_sessions_per_user`` 字段。

    保留其它顶层 key、注释和顺序。整个 read-modify-write 在文件锁保护下完成。
    修改后需重启 Hermes 网关生效。
    """

    def _modify(data: Any) -> None:
        data["group_sessions_per_user"] = bool(value)

    _read_modify_write(hermes_install_dir, modify=_modify)


# ── 工具集列表(从 Hermes 安装目录 import)──────────────────────────────


def _agent_syspath(hermes_dir: Path) -> list[str]:
    """返回可能包含 Hermes agent Python 模块的 sys.path 条目列表。

    优先检查 ``hermes-agent`` 子目录(常见于 git clone 安装),再回退到
    ``hermes_dir`` 自身(pip 安装)。
    """
    agent_dir = hermes_dir / "hermes-agent"
    if agent_dir.is_dir() and (agent_dir / "hermes_cli").is_dir():
        return [str(agent_dir), str(hermes_dir)]
    return [str(hermes_dir)]


def _find_venv(hermes_dir: Path) -> tuple[str, Path] | None:
    """检测 Hermes venv 的 Python 解释器和 agent 源码目录。

    返回 ``(venv_python_path, agent_dir)`` 或 ``None``。支持两种布局:

    - ``~/.hermes/`` (数据) + ``~/.hermes/hermes-agent/`` (代码) + ``.../venv/``
    - ``<hermes_install_dir>/`` (直接是 agent 代码) + ``.../venv/``
    """
    for agent_dir in (hermes_dir / "hermes-agent", hermes_dir):
        if not agent_dir.is_dir():
            continue
        for rel in ("venv/bin/python", "venv/Scripts/python.exe", ".venv/bin/python"):
            py = agent_dir / rel
            if py.exists():
                return (str(py), agent_dir)
    return None


# ── 子进程脚本(在 Hermes venv 中执行,输出 JSON 到 stdout)─────────────────

_LIST_TOOLSETS_SCRIPT = (
    "import json, sys\n"
    "import hermes_cli.tools_config as tc\n"
    "import toolsets as ts\n"
    "configurable = []\n"
    "for key, label, desc in tc._get_effective_configurable_toolsets():\n"
    "    try:\n"
    "        tools = list(ts.resolve_toolset(key))\n"
    "    except Exception:\n"
    "        tools = []\n"
    "    configurable.append({'key': key, 'label': label, 'description': desc,\n"
    "                        'tools': tools, 'is_plugin': key in tc._get_plugin_toolset_keys()})\n"
    "json.dump({'configurable': configurable}, sys.stdout, ensure_ascii=False)\n"
)

_DEFAULT_TOOLSETS_SCRIPT = (
    "import json, sys\n"
    "import hermes_cli.tools_config as tc\n"
    "keys = [k for k, _, _ in tc.CONFIGURABLE_TOOLSETS]\n"
    "default_off = list(tc._DEFAULT_OFF_TOOLSETS)\n"
    "json.dump({'keys': keys, 'default_off': default_off}, sys.stdout, ensure_ascii=False)\n"
)


def _run_hermes_subprocess(venv_python: str, agent_dir: Path, script: str) -> dict | None:
    """用 Hermes venv 的 Python 执行脚本,返回解析后的 JSON dict。

    失败(启动失败/非零退出/JSON 解析失败)返回 ``None`` 并记录日志。
    """
    try:
        proc = subprocess.run(
            [venv_python, "-c", script],
            cwd=str(agent_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        logger.warning("hermes subprocess 启动失败 (%s): %s", venv_python, exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "hermes subprocess 退出码 %d, stderr: %s",
            proc.returncode, proc.stderr.strip()[:500],
        )
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("hermes subprocess 输出 JSON 解析失败: %s", exc)
        return None


def _read_mcp_servers(hermes_install_dir: str | None) -> list[dict]:
    """从 config.yaml 读取 MCP 服务器列表(不依赖 Hermes Python 模块)。"""
    mcp_servers: list[dict] = []
    try:
        data = read_config(hermes_install_dir)
        mcp_cfg = data.get("mcp_servers") or {}
        if hasattr(mcp_cfg, "items"):
            for name, srv_cfg in mcp_cfg.items():
                srv_cfg_dict = dict(srv_cfg) if hasattr(srv_cfg, "items") else {}
                enabled = srv_cfg_dict.get("enabled", True)
                mcp_servers.append({"name": str(name), "enabled": bool(enabled)})
    except Exception as exc:
        logger.warning("mcp_servers 读取失败: %s", exc)
    return mcp_servers


def list_available_toolsets(hermes_install_dir: str | None) -> dict:
    """返回 OneBot 平台可配置的工具集 + MCP 服务器清单。

    返回结构::

        {
            "configurable": [
                {"key": "web", "label": "🔍 Web Search & Scraping",
                 "description": "...", "tools": ["web_search", "web_extract"],
                 "is_plugin": false},
                ...
            ],
            "mcp_servers": [{"name": "github", "enabled": true}, ...],
        }

    优先用 Hermes venv Python 跑子进程获取工具集(绕开依赖不匹配问题);
    venv 不存在时 fallback 到 ``sys.path`` 方案。两者都失败时返回
    ``{"error": "...", "detail": "..."}``。
    """
    hermes_dir = _resolve_hermes_dir(hermes_install_dir)

    # ── 主路径:子进程(用 Hermes 自己的 venv Python)──────────────────────
    venv = _find_venv(hermes_dir)
    if venv is not None:
        venv_python, agent_dir = venv
        result = _run_hermes_subprocess(venv_python, agent_dir, _LIST_TOOLSETS_SCRIPT)
        if result is not None and "configurable" in result:
            result["mcp_servers"] = _read_mcp_servers(hermes_install_dir)
            return result
        # 子进程失败 → 继续 fallback 到 sys.path

    # ── fallback:sys.path 方案(pip 安装或子进程失败时)──────────────────
    agent_paths = _agent_syspath(hermes_dir)
    added: list[str] = []
    for p in agent_paths:
        if p not in sys.path:
            sys.path.insert(0, p)
            added.append(p)

    try:
        try:
            import hermes_cli.tools_config as tc  # noqa: F401
            import toolsets as ts  # noqa: F401
        except Exception as exc:
            logger.warning("list_available_toolsets: import 失败 (sys.path=%s): %s", agent_paths, exc)
            return {"error": "hermes not importable", "detail": str(exc)}

        configurable: list[dict] = []
        try:
            for key, label, desc in tc._get_effective_configurable_toolsets():
                try:
                    tools = ts.resolve_toolset(key)
                except Exception:
                    tools = []
                is_plugin = key in tc._get_plugin_toolset_keys()
                configurable.append({
                    "key": key,
                    "label": label,
                    "description": desc,
                    "tools": list(tools),
                    "is_plugin": is_plugin,
                })
        except Exception as exc:
            return {"error": "failed to list toolsets", "detail": str(exc)}

        return {"configurable": configurable, "mcp_servers": _read_mcp_servers(hermes_install_dir)}
    finally:
        for p in added:
            try:
                sys.path.remove(p)
            except ValueError:
                pass


# ── 默认工具集(首次安装用)──────────────────────────────────────────


# Fallback 默认列表:核心工具集(与 Hermes 的 CONFIGURABLE_TOOLSETS 对齐)减去
# _DEFAULT_OFF_TOOLSETS。当 hermes_install_dir 无法 import 时使用。
_FALLBACK_DEFAULT_TOOLSETS = [
    "web", "browser", "terminal", "file", "code_execution",
    "vision", "image_gen", "skills", "todo", "memory",
    "session_search", "clarify", "delegation", "cronjob",
    "computer_use",
]
_FALLBACK_DEFAULT_OFF = {
    "moa", "homeassistant", "spotify", "discord", "discord_admin",
    "video", "video_gen", "x_search",
}


def default_onebot_toolsets(hermes_install_dir: str | None) -> list[str]:
    """计算首次安装时为 OneBot 平台启用的默认工具集列表。

    优先用 Hermes venv Python 跑子进程获取 ``CONFIGURABLE_TOOLSETS`` +
    ``_DEFAULT_OFF_TOOLSETS``,取差集后加上 ``"onebot"`` 插件 toolset key。
    venv 不存在时 fallback 到 ``sys.path`` 方案;均失败时 fallback 到静态列表。

    不含 MCP 服务器条目(让其走"全局 MCP 默认全启"路径)。
    """
    hermes_dir = _resolve_hermes_dir(hermes_install_dir)

    # ── 主路径:子进程 ──────────────────────────────────────────────────
    venv = _find_venv(hermes_dir)
    if venv is not None:
        venv_python, agent_dir = venv
        result = _run_hermes_subprocess(venv_python, agent_dir, _DEFAULT_TOOLSETS_SCRIPT)
        if result is not None and "keys" in result:
            configurable_keys = set(result["keys"])
            default_off = set(result.get("default_off", []))
            out = sorted(configurable_keys - default_off)
            out.append(PLUGIN_TOOLSET_KEY)
            return sorted(set(out))
        # 子进程失败 → 继续 fallback

    # ── fallback:sys.path 方案 ──────────────────────────────────────────
    agent_paths = _agent_syspath(hermes_dir)
    added: list[str] = []
    for p in agent_paths:
        if p not in sys.path:
            sys.path.insert(0, p)
            added.append(p)
    try:
        try:
            import hermes_cli.tools_config as tc  # noqa: F401
        except Exception:
            # fallback 到静态列表
            result = [k for k in _FALLBACK_DEFAULT_TOOLSETS if k not in _FALLBACK_DEFAULT_OFF]
            result.append(PLUGIN_TOOLSET_KEY)
            return sorted(set(result))

        configurable_keys = {k for k, _, _ in tc.CONFIGURABLE_TOOLSETS}
        default_off = set(tc._DEFAULT_OFF_TOOLSETS)
        result = sorted(configurable_keys - default_off)
        result.append(PLUGIN_TOOLSET_KEY)
        return sorted(set(result))
    finally:
        for p in added:
            try:
                sys.path.remove(p)
            except ValueError:
                pass


# ── 当前启用状态(读取现有配置)──────────────────────────────────────


def read_current_enabled(hermes_install_dir: str | None) -> list[str]:
    """读取 ``platform_toolsets.onebot``;不存在返回空列表。"""
    data = read_config(hermes_install_dir)
    platform_toolsets = data.get("platform_toolsets") or {}
    if hasattr(platform_toolsets, "get"):
        val = platform_toolsets.get(PLATFORM, [])
    else:
        val = []
    if val is None:
        return []
    return [str(x) for x in val]

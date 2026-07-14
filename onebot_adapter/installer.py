"""Installer: copies the bundled Hermes plugin into a Hermes install directory.

Also writes ONEBOT_ADAPTER_URL and ONEBOT_ADAPTER_TOKEN into the Hermes
``.env`` file so the plugin works immediately upon gateway restart.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ruamel.yaml import YAML

from onebot_adapter import __version__

logger = logging.getLogger(__name__)

PLUGIN_SRC = Path(__file__).parent / "hermes_plugin"
_PLUGIN_FILES = ("__init__.py", "adapter.py", "markdown.py", "onebot_tools.py", "plugin.yaml")
_ENV_VAR_URL = "ONEBOT_ADAPTER_URL"
_ENV_VAR_TOKEN = "ONEBOT_ADAPTER_TOKEN"


def _is_safe_install_path(target: Path) -> bool:
    """Return True if *target* is safe to use as an install target.

    Only allow writes under the user's home directory, /home, or /tmp.
    Rejects system paths (/, /etc, /usr, etc.) to prevent accidental
    writes via the CLI or WebUI.
    """
    allowed_roots = {Path.home(), Path("/home"), Path("/tmp")}
    resolved = target.resolve(strict=False)
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            pass
    return False


def _resolve_hermes_dir(install_dir: str | None) -> Path:
    if install_dir:
        return Path(install_dir).expanduser()
    explicit = os.getenv("HERMES_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".hermes"


def _env_path(hermes_dir: Path) -> Path:
    return hermes_dir / ".env"


def _read_env(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    env: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _write_env(env_path: Path, updates: dict[str, str]) -> dict[str, str]:
    """Merge *updates* into an existing env file and persist.

    Returns the final env dict.
    """
    env = _read_env(env_path)
    env.update(updates)
    lines: list[str] = []
    for k, v in env.items():
        lines.append(f"{k}={v}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env


# ── Install ──────────────────────────────────────────────────────────────


def install(
    install_dir: str | None = None,
    adapter_url: str = "",
    adapter_token: str = "",
) -> dict:
    hermes_dir = _resolve_hermes_dir(install_dir)
    if not _is_safe_install_path(hermes_dir):
        return {
            "adapter_version": __version__,
            "hermes_dir": str(hermes_dir),
            "error": f"install_dir resolved to {hermes_dir}, which is outside $HOME",
        }
    dest = hermes_dir / "plugins" / "onebot"
    result: dict = {
        "adapter_version": __version__,
        "hermes_dir": str(hermes_dir),
        "plugin_dest": str(dest),
        "source": str(PLUGIN_SRC),
        "copied": [],
        "env_vars": {},
    }

    if not PLUGIN_SRC.exists():
        result["error"] = f"plugin source not found: {PLUGIN_SRC}"
        return result

    # Copy plugin files
    dest.mkdir(parents=True, exist_ok=True)
    _yaml = YAML()
    for fname in _PLUGIN_FILES:
        src_file = PLUGIN_SRC / fname
        if not src_file.exists():
            continue
        if fname == "plugin.yaml":
            data = _yaml.load(src_file.read_text(encoding="utf-8"))
            data["version"] = __version__
            out_path = dest / fname
            _yaml.dump(data, out_path)
        else:
            shutil.copy2(src_file, dest / fname)
        result["copied"].append(fname)

    # Clean stale .pyc
    pycache = dest / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache, ignore_errors=True)

    # Write env vars
    env_updates: dict[str, str] = {}
    if adapter_url:
        env_updates[_ENV_VAR_URL] = adapter_url
    if adapter_token:
        env_updates[_ENV_VAR_TOKEN] = adapter_token

    final_env: dict[str, str] = {}
    if env_updates:
        final_env = _write_env(_env_path(hermes_dir), env_updates)
        result["env_vars"] = {
            k: v for k, v in final_env.items() if k in env_updates
        }
        logger.info("Env vars written: %s", list(env_updates.keys()))

    result["note"] = (
        f"Plugin installed to {dest}. "
        f"Environment variables written to {_env_path(hermes_dir)}. "
        "Restart the Hermes gateway for changes to take effect."
    )
    logger.info("Plugin installed to %s (%d files)", dest, len(result["copied"]))

    # 初始化 OneBot 平台默认工具集配置(写入 platform_toolsets.onebot +
    # known_plugin_toolsets.onebot)。失败不阻断安装,WebUI 工具管理页可补救。
    try:
        from onebot_adapter.hermes_config import default_onebot_toolsets, write_platform_toolsets

        defaults = default_onebot_toolsets(install_dir)
        write_platform_toolsets(install_dir, defaults)
        result["note"] += (
            " 已为 OneBot 平台启用默认工具集;请运行 hermes plugins enable onebot-platform"
            " 并重启 Hermes 网关后生效。"
        )
        logger.info("platform_toolsets.onebot initialized: %s", defaults)
    except Exception as exc:
        logger.warning("could not init platform_toolsets.onebot: %s", exc)
        result["note"] += " (工具集默认配置写入失败,请用 WebUI 工具管理页手动配置)"

    return result


# ── Uninstall ────────────────────────────────────────────────────────────


def uninstall(install_dir: str | None = None) -> dict:
    hermes_dir = _resolve_hermes_dir(install_dir)
    if not _is_safe_install_path(hermes_dir):
        return {
            "adapter_version": __version__,
            "hermes_dir": str(hermes_dir),
            "error": f"install_dir resolved to {hermes_dir}, which is outside $HOME",
        }
    dest = hermes_dir / "plugins" / "onebot"
    env_path = _env_path(hermes_dir)

    result: dict = {
        "adapter_version": __version__,
        "hermes_dir": str(hermes_dir),
        "plugin_dest": str(dest),
        "removed": False,
        "env_cleaned": False,
    }

    # Remove plugin directory
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
        result["removed"] = True
        logger.info("Plugin directory removed: %s", dest)
    else:
        logger.info("Plugin directory not found: %s", dest)

    # Remove our vars from .env (keep other vars intact)
    env = _read_env(env_path)
    removed_any = False
    for key in (_ENV_VAR_URL, _ENV_VAR_TOKEN):
        if key in env:
            del env[key]
            removed_any = True
    if removed_any:
        lines = [f"{k}={v}" for k, v in env.items()]
        if lines:
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            env_path.unlink(missing_ok=True)
        result["env_cleaned"] = True
        logger.info("Env vars removed from %s", env_path)

    result["note"] = (
        f"Plugin removed from {dest}. "
        f"{'Env vars cleaned. ' if removed_any else ''}"
        "Restart the Hermes gateway."
    )
    return result

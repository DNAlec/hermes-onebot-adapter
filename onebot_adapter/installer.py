"""Installer: copies the bundled Hermes plugin into a Hermes install directory.

Also writes ONEBOT_ADAPTER_URL and ONEBOT_ADAPTER_TOKEN into the Hermes
``.env`` file so the plugin works immediately upon gateway restart.
"""
from __future__ import annotations

import io
import logging
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from onebot_adapter import __version__

logger = logging.getLogger(__name__)

PLUGIN_SRC = Path(__file__).parent / "hermes_plugin"
_PLUGIN_FILES = ("__init__.py", "adapter.py", "markdown.py", "onebot_tools.py", "plugin.yaml")
_ENV_VAR_URL = "ONEBOT_ADAPTER_URL"
_ENV_VAR_TOKEN = "ONEBOT_ADAPTER_TOKEN"

HERMES_DIR_OUTSIDE_MSG = "hermes_install_dir is outside the allowed Hermes install roots"
_FORBIDDEN_SYSTEM_NAMES = ("etc", "proc", "sys")
DEFAULT_AUTOMATION_UPLOAD_ROOT = "/tmp/hermes-onebot-adapter-uploads"


class HermesDirNotAllowed(ValueError):
    """Raised when a Hermes install path is outside the allowlist."""


def _is_fs_root(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return resolved.parent == resolved


def _is_forbidden_system_path(resolved: Path) -> bool:
    """True for filesystem roots and /etc, /proc, /sys (and their children)."""
    if _is_fs_root(resolved):
        return True
    parts = resolved.parts
    if len(parts) >= 2 and parts[0] == "/" and parts[1] in _FORBIDDEN_SYSTEM_NAMES:
        return True
    return False


def _expand_absolute(raw: str) -> Path | None:
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        return None
    return expanded


def validate_hermes_extra_root(root: str) -> str | None:
    """Return an error string if *root* is not a usable extra Hermes root."""
    expanded = _expand_absolute(root)
    if expanded is None:
        return f"hermes_install_allowed_roots entry must be an absolute path: {root}"
    resolved = expanded.resolve(strict=False)
    if _is_forbidden_system_path(resolved):
        return f"hermes_install_allowed_roots entry is a forbidden system path: {root}"
    return None


def validate_upload_root(root: str) -> str | None:
    """Return an error string if *root* is not a usable automation upload root."""
    expanded = _expand_absolute(root)
    if expanded is None:
        return "automation_upload_allowed_roots entries must be absolute paths"
    resolved = expanded.resolve(strict=False)
    if _is_forbidden_system_path(resolved):
        return f"automation_upload_allowed_roots must not include system roots: {root}"
    if resolved == Path("/tmp"):
        return "automation_upload_allowed_roots must not be the whole /tmp directory"
    return None


def _iter_allowed_hermes_roots(extra_roots: Sequence[str] | None) -> list[Path]:
    roots: list[Path] = []
    home = Path.home().expanduser().resolve(strict=False)
    # HOME=/ would otherwise allow the entire filesystem.
    if not _is_fs_root(home):
        roots.append(home)
    for raw in extra_roots or ():
        if validate_hermes_extra_root(raw) is not None:
            continue
        expanded = _expand_absolute(raw)
        if expanded is None:
            continue
        roots.append(expanded.resolve(strict=False))
    return roots


def is_allowed_hermes_dir(target: Path, extra_roots: Sequence[str] | None = None) -> bool:
    """True if *target* resolves under $HOME or an extra allowed root.

    ``/home`` (other users) and ``/tmp`` are not allowed by default.
    Extra roots that themselves fail :func:`validate_hermes_extra_root` are ignored.
    Relative paths are rejected so they cannot sneak in via the process cwd.
    """
    expanded = target.expanduser()
    if not expanded.is_absolute():
        return False
    resolved = expanded.resolve(strict=False)
    for root in _iter_allowed_hermes_roots(extra_roots):
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def validate_hermes_dir(target: Path | str, extra_roots: Sequence[str] | None = None) -> str | None:
    """Return :data:`HERMES_DIR_OUTSIDE_MSG` when *target* is not allowed."""
    path = target if isinstance(target, Path) else Path(target)
    if is_allowed_hermes_dir(path, extra_roots):
        return None
    return HERMES_DIR_OUTSIDE_MSG


def venv_python_is_inside_hermes(venv_python: str, hermes_dir: Path) -> bool:
    """True if the venv interpreter's parent directory stays inside *hermes_dir*.

    The interpreter file itself is often a symlink to a system Python; we
    resolve only the parent so a normal venv is accepted, but a ``venv/``
    directory that is a symlink pointing outside the Hermes tree is not.
    """
    py = Path(venv_python)
    try:
        parent = py.parent.resolve(strict=False)
        root = hermes_dir.expanduser().resolve(strict=False)
        parent.relative_to(root)
        return True
    except (ValueError, OSError):
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


def _strip_quotes(value: str) -> str:
    """Strip a single layer of surrounding quotes from a .env value.

    Handles both ``"..."`` and ``'...'`` quoting. If the value is not
    quoted, returns it unchanged.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _read_env(env_path: Path) -> dict[str, str]:
    """Read a .env file into a dict. Comments and blank lines are dropped.

    Surrounding quotes (``"..."`` or ``'...'``) are stripped so the returned
    value is the raw string. This makes read-modify-write idempotent: the
    writer re-quotes values that need it, so a round-trip doesn't accumulate
    extra layers of quoting.
    """
    if not env_path.exists():
        return {}
    env: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = _strip_quotes(v.strip())
    return env


def _persist_env(env_path: Path, env: dict[str, str]) -> None:
    """Atomically write a full env mapping with the same quoting as ``_write_env``."""
    lines: list[str] = []
    for k, v in env.items():
        if v and any(c in v for c in (" ", "\t", "'", '"', "#", "$", "\\")):
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k}="{escaped}"')
        else:
            lines.append(f"{k}={v}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    _atomic_write_nofollow(env_path, payload)


def _write_env(env_path: Path, updates: dict[str, str]) -> dict[str, str]:
    """Merge *updates* into an existing env file and persist atomically.

    Values that contain spaces or shell-special characters are quoted with
    double quotes to ensure correct parsing by dotenv loaders. The write is
    atomic (tmp + ``os.replace``) so a crash mid-write doesn't corrupt the
    existing .env.  Returns the final env dict.
    """
    env = _read_env(env_path)
    env.update(updates)
    _persist_env(env_path, env)
    return env


def _atomic_write_nofollow(dest: Path, data: bytes) -> None:
    """Write *data* to *dest* without following a pre-planted symlink."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp.{os.getpid()}.{os.urandom(4).hex()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, dest)


def _lstat_not_symlink(path: Path, *, what: str) -> str | None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot stat {what}: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return f"{what} is a symlink, refusing to overwrite: {path}"
    return None


def _ensure_real_dir(path: Path) -> str | None:
    err = _lstat_not_symlink(path, what="install target")
    if err:
        return err
    path.mkdir(parents=True, exist_ok=True)
    err = _lstat_not_symlink(path, what="install target")
    if err:
        return err
    try:
        st = path.lstat()
    except OSError as exc:
        return f"cannot stat install target: {exc}"
    if not stat.S_ISDIR(st.st_mode):
        return f"install target is not a directory: {path}"
    return None


def _rmtree_if_not_symlink(path: Path, *, ignore_errors: bool = False) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    err = _lstat_not_symlink(path, what="install target")
    if err:
        return err
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=ignore_errors)
    except OSError as exc:
        if ignore_errors:
            logger.warning("could not remove %s: %s", path, exc)
            return None
        return f"cannot remove {path}: {exc}"
    return None


# ── Install ──────────────────────────────────────────────────────────────


def install(
    install_dir: str | None = None,
    adapter_url: str = "",
    adapter_token: str = "",
    extra_roots: Sequence[str] | None = None,
) -> dict:
    hermes_dir = _resolve_hermes_dir(install_dir)
    if not is_allowed_hermes_dir(hermes_dir, extra_roots):
        return {
            "adapter_version": __version__,
            "hermes_dir": str(hermes_dir),
            "error": f"install_dir resolved to {hermes_dir}, which is outside allowed Hermes roots",
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

    plugins_dir = dest.parent
    for directory in (hermes_dir, plugins_dir, dest):
        err = _ensure_real_dir(directory)
        if err:
            result["error"] = err
            return result
    # Use round-trip YAML to preserve string quoting (e.g. version: "0.0.0"
    # stays quoted so it isn't parsed as float 0.0).
    _yaml = YAML(typ="rt")
    for fname in _PLUGIN_FILES:
        src_file = PLUGIN_SRC / fname
        if not src_file.exists():
            continue
        out_path = dest / fname
        if fname == "plugin.yaml":
            data = _yaml.load(src_file.read_text(encoding="utf-8"))
            if data is None:
                data = CommentedMap()
            data["version"] = __version__
            buf = io.StringIO()
            _yaml.dump(data, buf)
            _atomic_write_nofollow(out_path, buf.getvalue().encode("utf-8"))
        else:
            _atomic_write_nofollow(out_path, src_file.read_bytes())
        result["copied"].append(fname)

    sanitize_src = Path(__file__).parent / "logging_utils.py"
    sanitize_dest = dest / "log_sanitize.py"
    if sanitize_src.exists():
        _atomic_write_nofollow(sanitize_dest, sanitize_src.read_bytes())
        result["copied"].append("log_sanitize.py")

    pycache = dest / "__pycache__"
    # Stale bytecode is best-effort. Hermes may have the dir open or own
    # files the adapter cannot delete; that must not abort a successful copy.
    _rmtree_if_not_symlink(pycache, ignore_errors=True)

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

    # 仅在尚未配置时写入默认工具集。重装插件不得覆盖 WebUI 里已保存的选择。
    try:
        from onebot_adapter.hermes_config import ensure_default_platform_toolsets

        created = ensure_default_platform_toolsets(install_dir, extra_roots=extra_roots)
        if created:
            result["note"] += (
                " 已为 OneBot 平台启用默认工具集;请运行 hermes plugins enable onebot-platform"
                " 并重启 Hermes 网关后生效。"
            )
            logger.info("platform_toolsets.onebot initialized with defaults")
        else:
            result["note"] += " 已保留现有 OneBot 工具集配置。"
            logger.info("platform_toolsets.onebot already present, not overwritten")
    except Exception as exc:
        logger.warning("could not init platform_toolsets.onebot: %s", exc)
        result["note"] += " (工具集默认配置写入失败,请用 WebUI 工具管理页手动配置)"

    return result


# ── Uninstall ────────────────────────────────────────────────────────────


def uninstall(install_dir: str | None = None, extra_roots: Sequence[str] | None = None) -> dict:
    hermes_dir = _resolve_hermes_dir(install_dir)
    if not is_allowed_hermes_dir(hermes_dir, extra_roots):
        return {
            "adapter_version": __version__,
            "hermes_dir": str(hermes_dir),
            "error": f"install_dir resolved to {hermes_dir}, which is outside allowed Hermes roots",
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

    if dest.exists() or dest.is_symlink():
        err = _lstat_not_symlink(dest, what="install target")
        if err:
            result["error"] = err
            return result
        # Root-owned __pycache__ (gateway once ran as root) must not abort uninstall.
        _rmtree_if_not_symlink(dest, ignore_errors=True)
        result["removed"] = not dest.exists() and not dest.is_symlink()
        if result["removed"]:
            logger.info("Plugin directory removed: %s", dest)
        else:
            logger.warning("plugin directory not fully removed: %s", dest)
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
        if env:
            _persist_env(env_path, env)
        else:
            env_path.unlink(missing_ok=True)
        result["env_cleaned"] = True
        logger.info("Env vars removed from %s", env_path)

    leftover = dest.exists() or dest.is_symlink()
    result["note"] = (
        f"Plugin removed from {dest}. "
        f"{'Env vars cleaned. ' if removed_any else ''}"
        "Restart the Hermes gateway."
    )
    if leftover:
        result["note"] += (
            " Some files could not be deleted (often root-owned __pycache__); remove them manually."
        )
    return result

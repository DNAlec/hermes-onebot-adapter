"""Hermes OneBot Adapter service.

A standalone process that bridges OneBot (OneBot 11) and the Hermes Agent
plugin via WebSocket. Run with ``python -m onebot_adapter`` or ``hermes-onebot-adapter``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]


def _git_describe() -> str | None:
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--dirty"],
            cwd=_repo_root,
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout:
            desc = result.stdout.strip()
            if desc.startswith("v"):
                desc = desc[1:]
            return desc
    except (FileNotFoundError, OSError):
        pass
    return None


_version = _git_describe()

if _version is None:
    try:
        from importlib.metadata import version as _pkg_version
        _version = _pkg_version("hermes-onebot-adapter")
    except Exception:
        _pyproject = _repo_root / "pyproject.toml"
        if _pyproject.is_file():
            import tomllib
            _version = tomllib.loads(_pyproject.read_text())["project"]["version"]
        else:
            _version = "0.0.0"

__version__ = _version

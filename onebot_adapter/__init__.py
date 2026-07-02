"""Hermes OneBot Adapter service.

A standalone process that bridges OneBot (OneBot 11) and the Hermes Agent
plugin via WebSocket. Run with ``python -m onebot_adapter`` or ``hermes-onebot-adapter``.
"""
from __future__ import annotations

from pathlib import Path

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("hermes-onebot-adapter")
except Exception:  # pragma: no cover - not installed (editable / direct run)
    _pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if _pyproject.is_file():
        import tomllib
        __version__ = tomllib.loads(_pyproject.read_text())["project"]["version"]
    else:
        __version__ = "0.0.0"

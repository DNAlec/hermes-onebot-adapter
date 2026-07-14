"""Hermes OneBot Adapter service.

A standalone process that bridges OneBot (OneBot 11) and the Hermes Agent
plugin via WebSocket. Run with ``python -m onebot_adapter`` or ``hermes-onebot-adapter``.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]

try:
    from ._version_scm import __version__  # noqa: F811
except ImportError:
    # Source checkout without a build — derive version from git.
    desc: str | None = None
    try:
        r = subprocess.run(
            ["git", "describe", "--tags", "--long", "--dirty", "--always"],
            cwd=_repo_root, capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout:
            desc = r.stdout.strip()
    except (FileNotFoundError, OSError):
        pass
    if desc is None:
        # git missing / failed / non-zero exit: fall back to package metadata.
        try:
            from importlib.metadata import version as _pkg_version

            __version__ = _pkg_version("hermes-onebot-adapter")
        except Exception:
            __version__ = "0.0.0"
    else:
        dirty = desc.endswith("-dirty")
        if dirty:
            desc = desc[:-6]
        m = re.match(r"^(?:v?(\d+\.\d+\.\S+)-(\d+)-)?g([0-9a-f]+)$", desc)
        if not m:
            __version__ = desc
        else:
            tag = m.group(1)
            offset = m.group(2)
            short_hash = m.group(3)
            if tag is None:
                __version__ = f"0.0.0.dev0+g{short_hash}"
            elif offset is not None and int(offset) == 0 and not dirty:
                __version__ = tag
            else:
                count = int(offset) if offset else 0
                __version__ = f"{tag}.dev{count}+g{short_hash}"
            if dirty:
                __version__ += ".dirty"

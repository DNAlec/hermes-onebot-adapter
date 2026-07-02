"""Logging handler that buffers recent log lines for the WebUI /api/logs endpoint."""
from __future__ import annotations

import logging
from collections import deque
from typing import Any


class WebUILogHandler(logging.Handler):
    """Stores formatted log records in a deque for the WebUI to serve."""

    def __init__(self, buffer: deque, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._buffer = buffer
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def update_level(self, level: int) -> None:
        """Hot-reload the handler level."""
        self.setLevel(level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self.format(record))
        except Exception:
            pass


def attach_log_handler(state: dict[str, Any], level: str = "INFO") -> WebUILogHandler:
    """Create and attach a WebUILogHandler to the root logger.

    Returns the handler so it can be detached on shutdown if needed.
    """
    buffer: deque = state.setdefault("log_buffer", deque(maxlen=500))
    handler = WebUILogHandler(buffer, level=getattr(logging, level.upper(), logging.INFO))
    logging.getLogger().addHandler(handler)
    return handler


def _level_from_str(level_str: str) -> int:
    """Convert a level name string to its numeric logging level (INFO fallback)."""
    return getattr(logging, level_str.upper(), logging.INFO)

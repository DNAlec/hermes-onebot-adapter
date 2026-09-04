"""Listen-address helpers shared by the service entrypoint and tests."""
from __future__ import annotations

import errno
import ipaddress
import logging

import aiohttp.web

logger = logging.getLogger(__name__)


def is_loopback_bind(host: str) -> bool:
    """True for 127.0.0.1, ::1, localhost, and other loopback addresses."""
    raw = (host or "").strip().lower().strip("[]")
    if raw in {"127.0.0.1", "::1", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def resolve_bind_hosts(
    host: str,
    onebot_host: str | None,
    hermes_host: str | None,
    webui_host: str | None,
    cascade_host: str | None = None,
) -> tuple[str, str, str, str]:
    """Resolve per-listener hosts. Unspecified values fall back to *host*.

    Cascade does not follow ``onebot_host``: exposing NapCat must not also
    bind a full-privilege OneBot API on the cascade port unless asked.
    """
    onebot = host if onebot_host is None else onebot_host
    hermes = host if hermes_host is None else hermes_host
    webui = host if webui_host is None else webui_host
    cascade = host if cascade_host is None else cascade_host
    return onebot, hermes, webui, cascade


async def try_port(
    runner: aiohttp.web.AppRunner, host: str, port: int, label: str, max_retries: int = 50,
) -> aiohttp.web.TCPSite:
    """Bind *runner* to *port*; if busy try the next port up to *max_retries* times."""
    for attempt in range(max_retries):
        try:
            site = aiohttp.web.TCPSite(runner, host, port + attempt)
            await site.start()
            logger.info("%s listening on %s:%d", label, host, site.port)
            return site
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            if attempt == max_retries - 1:
                raise
            logger.debug("%s port %d busy, trying %d", label, port + attempt, port + attempt + 1)
    raise OSError("try_port exhausted retries")

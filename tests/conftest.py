"""Shared pytest fixtures/helpers for the onebot_adapter test suite."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time


def make_session_token(secret: str, epoch: int, issued_at: int | None = None) -> str:
    """Mint an HMAC-signed session token identical to what /api/login issues.

    Mirrors ``onebot_adapter.webui.routes._login`` so tests can authenticate
    without going through the login endpoint.
    """
    if issued_at is None:
        issued_at = int(time.time())
    msg = f"{epoch}:{issued_at}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    payload = f"{issued_at}.{sig}".encode()
    return base64.urlsafe_b64encode(payload).decode("ascii")

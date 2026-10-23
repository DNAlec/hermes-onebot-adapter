from __future__ import annotations

import json

import pytest

from onebot_adapter.logging_utils import safe_json, text_summary


def test_safe_json_redacts_secrets_message_bodies_and_signed_urls():
    rendered = safe_json({
        "token": "secret-value",
        "content": "private message",
        "image_url": "https://example.test/a.png?sig=secret",
        "nested": {"authorization": "Bearer abc", "text": "hello"},
    })
    assert "secret-value" not in rendered
    assert "private message" not in rendered
    assert "sig=secret" not in rendered
    assert "Bearer abc" not in rendered
    assert '"<redacted>"' in rendered
    assert "<text len=" in rendered


def test_safe_json_is_bounded():
    assert len(safe_json({"value": "x" * 1000}, limit=80)) == 80


def test_text_summary_hides_body_by_default():
    rendered = text_summary("private message")
    assert rendered == "<text len=15>"
    assert "private" not in rendered


def test_text_summary_optional_preview_is_truncated():
    rendered = text_summary("abcdefghij", preview=4)
    assert "len=10" in rendered
    assert "abcd..." in rendered
    assert "abcdefghij" not in rendered


@pytest.mark.parametrize("scheme", ["http", "https", "ws", "wss"])
@pytest.mark.parametrize("authority", ["example.test:8443", "[2001:db8::1]:8443"])
@pytest.mark.parametrize("userinfo", ["PRIVATE_USER", "PRIVATE_USER:PRIVATE_PASSWORD", "PRIVATE_USER:p%40ss"])
def test_safe_json_strips_url_userinfo(scheme, authority, userinfo):
    url = f"{scheme}://{userinfo}@{authority}/file?token=PRIVATE_TOKEN#PRIVATE_FRAGMENT"
    rendered = json.loads(safe_json({"file": url}))
    assert rendered["file"] == f"{scheme}://{authority}/file?<redacted>"


def test_safe_json_preserves_url_without_credentials():
    url = "https://[2001:db8::1]:8443/path@name"
    assert json.loads(safe_json({"file": url}))["file"] == url

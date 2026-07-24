from __future__ import annotations

from onebot_adapter.logging_utils import safe_json


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

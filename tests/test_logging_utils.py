from __future__ import annotations

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

"""Outbound regex filter: config resolve, validation, and text extraction."""
from __future__ import annotations

from onebot_adapter.config import AdapterConfig, GroupConfig
from onebot_adapter.outbound_filter import (
    MAX_OUTBOUND_FILTER_PATTERN_LENGTH,
    MAX_OUTBOUND_FILTER_PATTERNS,
    extract_api_message_text,
    extract_send_frame_text,
    matching_pattern,
    validate_outbound_filter_patterns,
)


def test_filter_disabled_by_default():
    cfg = AdapterConfig()
    assert cfg.outbound_filter_enabled is False
    assert cfg.outbound_filter_patterns == []
    assert cfg.resolve_outbound_filter_enabled() is False
    assert cfg.resolve_outbound_filter_patterns() == []


def test_resolve_group_override_enabled_and_patterns():
    cfg = AdapterConfig(
        outbound_filter_enabled=True,
        outbound_filter_patterns=["global"],
        groups={
            "42": GroupConfig(
                group_id="42",
                outbound_filter_enabled=False,
                outbound_filter_patterns=["group-only"],
            ).to_dict(),
            "99": GroupConfig(
                group_id="99",
                outbound_filter_patterns=[],
            ).to_dict(),
        },
    )
    assert cfg.resolve_outbound_filter_enabled("42") is False
    assert cfg.resolve_outbound_filter_patterns("42") == ["group-only"]
    assert cfg.resolve_outbound_filter_enabled("99") is True
    assert cfg.resolve_outbound_filter_patterns("99") == []
    assert cfg.resolve_outbound_filter_enabled("7") is True
    assert cfg.resolve_outbound_filter_patterns("7") == ["global"]
    assert cfg.resolve_outbound_filter_enabled() is True


def test_matching_pattern_search_and_empty_text():
    assert matching_pattern("hello world", [r"world"]) == r"world"
    assert matching_pattern("hello", [r"^hello$"]) == r"^hello$"
    assert matching_pattern("HELLO", [r"hello"]) is None
    assert matching_pattern("HELLO", [r"(?i)hello"]) == r"(?i)hello"
    assert matching_pattern("", [r".*"]) is None
    assert matching_pattern("keep", [r"drop"]) is None
    assert matching_pattern("abc", ["a", "a.c"]) == "a"


def test_matching_pattern_skips_invalid():
    assert matching_pattern("ab", [r"(", r"ab"]) == r"ab"


def test_extract_send_frame_text():
    assert extract_send_frame_text("send_text", {"content": "hi"}) == "hi"
    assert extract_send_frame_text("send_image", {"caption": "cap", "image_url": "x"}) == "cap"
    assert extract_send_frame_text("send_voice", {}) == ""
    assert extract_send_frame_text("send_document", {"caption": "doc"}) == "doc"
    assert extract_send_frame_text("unknown", {"content": "x"}) == ""


def test_extract_api_message_text():
    assert extract_api_message_text({"message": "plain"}) == "plain"
    assert extract_api_message_text({
        "message": [
            {"type": "at", "data": {"qq": "1"}},
            {"type": "text", "data": {"text": "hello "}},
            {"type": "text", "data": {"text": "world"}},
        ],
    }) == "hello world"
    assert extract_api_message_text({"message": [{"type": "image", "data": {}}]}) == ""
    assert extract_api_message_text({}) == ""


def test_validate_rejects_invalid_regex_and_limits():
    errors: list[str] = []
    validate_outbound_filter_patterns("not-a-list", "outbound_filter_patterns", errors)
    assert any("must be a list" in e for e in errors)

    errors = []
    validate_outbound_filter_patterns(["("], "outbound_filter_patterns", errors)
    assert any("not a valid regex" in e for e in errors)

    errors = []
    validate_outbound_filter_patterns([""], "outbound_filter_patterns", errors)
    assert any("non-empty string" in e for e in errors)

    errors = []
    validate_outbound_filter_patterns(["x" * (MAX_OUTBOUND_FILTER_PATTERN_LENGTH + 1)], "p", errors)
    assert any("at most" in e for e in errors)

    errors = []
    validate_outbound_filter_patterns(["ok"] * (MAX_OUTBOUND_FILTER_PATTERNS + 1), "p", errors)
    assert any("at most" in e for e in errors)

    errors = []
    validate_outbound_filter_patterns([r"(?i)ok", r"\d+"], "p", errors)
    assert errors == []


def test_config_validate_outbound_filter():
    cfg = AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2", outbound_filter_patterns=["("])
    assert any("outbound_filter_patterns" in e for e in cfg.validate())

    ok = AdapterConfig(
        onebot_ws_token="t1", hermes_ws_token="t2",
        outbound_filter_enabled=True,
        outbound_filter_patterns=[r"(?i)secret"],
    )
    assert not any("outbound_filter" in e for e in ok.validate())

    bad_group = AdapterConfig(
        onebot_ws_token="t1", hermes_ws_token="t2",
        groups={"42": {"group_id": "42", "outbound_filter_patterns": ["("]}},
    )
    assert any("group 42 outbound_filter_patterns" in e for e in bad_group.validate())

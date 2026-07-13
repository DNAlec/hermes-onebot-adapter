"""Tests for OneBot 11 segment extraction helpers."""
from onebot_adapter.onebot import segments as seg


def test_extract_text():
    segs = [
        {"type": "text", "data": {"text": "hello "}},
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": " world"}},
    ]
    assert seg.extract_text(segs) == "hello @123 world"


def test_extract_text_with_file():
    segs = [{"type": "file", "data": {"file": "doc.pdf"}}]
    assert seg.extract_text(segs) == "[文件: doc.pdf]"


def test_extract_image_urls():
    segs = [
        {"type": "image", "data": {"url": "http://a/1.jpg"}},
        {"type": "image", "data": {"file": "http://a/2.png"}},
        {"type": "text", "data": {"text": "skip"}},
    ]
    assert seg.extract_image_urls(segs) == ["http://a/1.jpg", "http://a/2.png"]


def test_extract_record_url():
    segs = [{"type": "record", "data": {"url": "http://a/v.silk"}}]
    assert seg.extract_record_url(segs) == "http://a/v.silk"


def test_extract_video_urls():
    segs = [{"type": "video", "data": {"url": "http://a/v.mp4"}}]
    assert seg.extract_video_urls(segs) == ["http://a/v.mp4"]


def test_extract_files():
    segs = [{"type": "file", "data": {"file": "f.txt", "file_id": "fid1", "url": "http://a/f", "file_size": 100}}]
    files = seg.extract_files(segs)
    assert len(files) == 1
    assert files[0]["name"] == "f.txt"
    assert files[0]["url"] == "http://a/f"
    assert files[0]["file_id"] == "fid1"


def test_extract_reply_id():
    assert seg.extract_reply_id([{"type": "reply", "data": {"id": 42}}]) == 42
    assert seg.extract_reply_id([{"type": "text", "data": {"text": "x"}}]) is None


def test_extract_forward_id():
    assert seg.extract_forward_id([{"type": "forward", "data": {"id": "fwd123"}}]) == "fwd123"
    assert seg.extract_forward_id([]) is None


def test_extract_forward_content():
    content = [{"sender": {"user_id": 1}, "message": []}]
    segs = [{"type": "forward", "data": {"id": "fwd1", "content": content}}]
    assert seg.extract_forward_content(segs) is content
    # No content field → None
    assert seg.extract_forward_content([{"type": "forward", "data": {"id": "fwd1"}}]) is None
    # Empty content → None
    assert seg.extract_forward_content([{"type": "forward", "data": {"id": "fwd1", "content": []}}]) is None
    # No forward segment → None
    assert seg.extract_forward_content([{"type": "text", "data": {"text": "x"}}]) is None
    # Picks the first forward segment
    segs_two = [
        {"type": "forward", "data": {"id": "fwd_a", "content": [{"a": 1}]}},
        {"type": "forward", "data": {"id": "fwd_b", "content": [{"b": 2}]}},
    ]
    assert seg.extract_forward_content(segs_two) == [{"a": 1}]


def test_has_and_strip_bot_mention():
    segs = [
        {"type": "at", "data": {"qq": "111"}},
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    assert seg.has_bot_mention(segs, "999") is True
    assert seg.has_bot_mention(segs, "888") is False
    stripped = seg.strip_bot_mention(segs, "999")
    assert len(stripped) == 2
    assert all(s.get("data", {}).get("qq") != "999" for s in stripped)


def test_strip_first_bot_mention_leading():
    """Leading @bot is removed; non-leading @bot preserved."""
    # [at(bot), text] → [text]
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    out = seg.strip_first_bot_mention(segs, "999")
    assert [s.get("type") for s in out] == ["text"]


def test_strip_first_bot_mention_skips_reply():
    """Leading `reply` segments are skipped before checking @bot."""
    segs = [
        {"type": "reply", "data": {"id": "55"}},
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    out = seg.strip_first_bot_mention(segs, "999")
    assert [s.get("type") for s in out] == ["reply", "text"]


def test_strip_first_bot_mention_non_leading_preserved():
    """When @bot is not the first non-reply segment, nothing is stripped."""
    # @someone-else first → bot mention kept
    segs = [
        {"type": "at", "data": {"qq": "111"}},
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    out = seg.strip_first_bot_mention(segs, "999")
    assert out == segs  # unchanged

    # text first, @bot later → kept
    segs2 = [
        {"type": "text", "data": {"text": "hi "}},
        {"type": "at", "data": {"qq": "999"}},
    ]
    out2 = seg.strip_first_bot_mention(segs2, "999")
    assert out2 == segs2


def test_strip_first_bot_mention_only_first_bot_removed():
    """Two @bot mentions: only the leading one is removed."""
    segs = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "at", "data": {"qq": "999"}},
    ]
    out = seg.strip_first_bot_mention(segs, "999")
    assert [s.get("type") for s in out] == ["text", "at"]
    assert out[1].get("data", {}).get("qq") == "999"


def test_strip_first_bot_mention_no_bot():
    """No @bot at the leading position → list returned unchanged."""
    segs = [
        {"type": "text", "data": {"text": "hi"}},
        {"type": "at", "data": {"qq": "111"}},
    ]
    out = seg.strip_first_bot_mention(segs, "999")
    assert out == segs


def test_strip_first_bot_mention_empty():
    assert seg.strip_first_bot_mention([], "999") == []


def test_has_bot_mention_first():
    assert seg.has_bot_mention_first([], "999") is False
    # first segment is the @bot mention
    segs_first = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    assert seg.has_bot_mention_first(segs_first, "999") is True
    assert seg.has_bot_mention_first(segs_first, "888") is False
    # @bot present but not first → False
    segs_mid = [
        {"type": "text", "data": {"text": "hi "}},
        {"type": "at", "data": {"qq": "999"}},
    ]
    assert seg.has_bot_mention_first(segs_mid, "999") is False
    assert seg.has_bot_mention(segs_mid, "999") is True  # any-position still True
    # reply as first segment: skip it before checking @bot
    segs_reply = [
        {"type": "reply", "data": {"id": "55"}},
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    assert seg.has_bot_mention_first(segs_reply, "999") is True
    assert seg.has_bot_mention_first(segs_reply, "888") is False
    # reply but no @bot after → False
    segs_reply_no_at = [
        {"type": "reply", "data": {"id": "55"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    assert seg.has_bot_mention_first(segs_reply_no_at, "999") is False
    # reply + @someone else first, @bot later → False (bot mention not first)
    segs_reply_other_at = [
        {"type": "reply", "data": {"id": "55"}},
        {"type": "at", "data": {"qq": "111111"}},
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "hi"}},
    ]
    assert seg.has_bot_mention_first(segs_reply_other_at, "999") is False
    # forward is NOT skipped: [reply, forward] → first non-reply is forward, not at
    segs_reply_forward = [
        {"type": "reply", "data": {"id": "55"}},
        {"type": "forward", "data": {"id": "66"}},
    ]
    assert seg.has_bot_mention_first(segs_reply_forward, "999") is False
    # all reply segments → False
    assert seg.has_bot_mention_first(
        [{"type": "reply", "data": {"id": "1"}},
         {"type": "reply", "data": {"id": "2"}}], "999") is False


def test_sender_display():
    assert seg.sender_display({"card": "A", "nickname": "B"}) == "A"
    assert seg.sender_display({"nickname": "B"}) == "B"
    assert seg.sender_display({"user_id": 42}) == "42"


# ── extract_text_with_placeholders ────────────────────────────────────────


def test_extract_text_with_placeholders_text_only():
    segs = [{"type": "text", "data": {"text": "hello"}}]
    text, markers = seg.extract_text_with_placeholders(segs)
    assert text == "hello"
    assert markers == []


def test_extract_text_with_placeholders_image():
    segs = [
        {"type": "text", "data": {"text": "before "}},
        {"type": "image", "data": {"url": "http://a/1.jpg"}},
        {"type": "text", "data": {"text": " after"}},
    ]
    text, markers = seg.extract_text_with_placeholders(segs)
    assert text == "before {[IMG:0]} after"
    assert len(markers) == 1
    assert markers[0]["kind"] == "image"
    assert markers[0]["url"] == "http://a/1.jpg"
    assert markers[0]["index"] == 0
    assert markers[0]["marker"] == "{[IMG:0]}"


def test_extract_text_with_placeholders_multiple_types():
    segs = [
        {"type": "image", "data": {"url": "http://a/1.jpg"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "video", "data": {"url": "http://a/v.mp4"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "record", "data": {"url": "http://a/r.silk"}},
        {"type": "file", "data": {"file": "doc.pdf", "url": "http://a/f"}},
    ]
    text, markers = seg.extract_text_with_placeholders(segs)
    assert "{[IMG:0]}" in text
    assert "{[VIDEO:1]}" in text
    assert "{[RECORD:2]}" in text
    assert "{[FILE:3:doc.pdf]}" in text
    assert len(markers) == 4
    assert markers[0]["kind"] == "image"
    assert markers[1]["kind"] == "video"
    assert markers[2]["kind"] == "record"
    assert markers[3]["kind"] == "file"
    assert markers[3]["file_info"]["name"] == "doc.pdf"


def test_extract_text_with_placeholders_start_index():
    segs = [{"type": "image", "data": {"url": "http://a/1.jpg"}}]
    text, markers = seg.extract_text_with_placeholders(segs, start_index=5)
    assert text == "{[IMG:5]}"
    assert markers[0]["index"] == 5


def test_extract_text_with_placeholders_at():
    segs = [
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": " hi"}},
    ]
    text, markers = seg.extract_text_with_placeholders(segs)
    assert text == "@123 hi"
    assert markers == []

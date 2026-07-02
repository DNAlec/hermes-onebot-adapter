"""Tests for the markdown → QQ plain-text converter."""
from onebot_adapter.hermes_plugin.markdown import strip_markdown


def test_plain_text_unchanged():
    assert strip_markdown("hello world") == "hello world"


def test_bold_stripped():
    assert strip_markdown("**bold** text") == "bold text"


def test_italic_stripped():
    assert strip_markdown("*italic* text") == "italic text"


def test_inline_code_stripped():
    assert strip_markdown("use `code` here") == "use code here"


def test_heading_converted():
    assert strip_markdown("## Title") == "【Title】"
    assert strip_markdown("### Sub") == "▌ Sub"


def test_fenced_code_block():
    md = "```python\nprint('hi')\n```"
    result = strip_markdown(md)
    assert "│ print('hi')" in result
    assert "[python]" in result
    assert "┌" in result
    assert "└" in result


def test_unordered_list():
    assert strip_markdown("- item one\n- item two") == "• item one\n• item two"


def test_ordered_list():
    assert strip_markdown("1. first\n2. second") == "1. first\n2. second"


def test_link_converted():
    result = strip_markdown("[text](http://url)")
    assert "text" in result
    assert "http://url" in result


def test_strikethrough_stripped():
    assert strip_markdown("~~deleted~~") == "deleted"


def test_blockquote_converted():
    assert strip_markdown("> quoted text") == "「quoted text」"


def test_horizontal_rule():
    assert "────" in strip_markdown("---")


def test_table_row():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    result = strip_markdown(md)
    assert "A" in result and "B" in result
    assert "1" in result and "2" in result
    # separator row should be skipped
    assert "---" not in result.split("\n")[0]


def test_empty_string():
    assert strip_markdown("") == ""

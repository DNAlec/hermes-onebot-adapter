"""Markdown → QQ-friendly plain-text conversion.

Ported from the built-in napcat.py. QQ does not render Markdown; raw syntax
like **bold** or ## heading appears as literal characters.  These functions
convert the most common constructs to readable Unicode equivalents.
"""
from __future__ import annotations

import re


def strip_markdown(text: str) -> str:
    """Convert Markdown to clean QQ-friendly plain text."""
    lines = text.splitlines()
    out: list[str] = []
    in_code = False
    code_lang = ""
    code_lines: list[str] = []

    for line in lines:
        # ── fenced code blocks ────────────────────────────────────────────
        fence = re.match(r"^(`{3,}|~{3,})(.*)", line.strip())
        if fence:
            if not in_code:
                in_code = True
                code_lang = fence.group(2).strip()
                code_lines = []
            else:
                in_code = False
                label = f"[{code_lang}]" if code_lang else "[代码]"
                out.append(f"┌─{label}─")
                for cl in code_lines:
                    out.append("│ " + cl)
                out.append("└──────")
                code_lines = []
            continue
        if in_code:
            code_lines.append(line)
            continue

        # ── headings ──────────────────────────────────────────────────────
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            level, title = len(h.group(1)), h.group(2).strip()
            title = _inline(title)
            if level <= 2:
                out.append(f"【{title}】")
            else:
                out.append(f"▌ {title}")
            continue

        # ── horizontal rules ──────────────────────────────────────────────
        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            out.append("────────────────")
            continue

        # ── blockquotes ───────────────────────────────────────────────────
        bq = re.match(r"^>\s?(.*)", line)
        if bq:
            out.append("「" + _inline(bq.group(1)) + "」")
            continue

        # ── unordered lists ───────────────────────────────────────────────
        ul = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        if ul:
            indent = len(ul.group(1)) // 2
            out.append("  " * indent + "• " + _inline(ul.group(2)))
            continue

        # ── ordered lists ─────────────────────────────────────────────────
        ol = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        if ol:
            indent = len(ol.group(1)) // 2
            num = ol.group(2)
            out.append("  " * indent + num + ". " + _inline(ol.group(3)))
            continue

        # ── table rows ────────────────────────────────────────────────────
        if re.match(r"^\s*\|", line):
            if re.match(r"^\s*\|[\s\-:|]+\|\s*$", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("  ".join(_inline(c) for c in cells if c))
            continue

        # ── normal line ───────────────────────────────────────────────────
        out.append(_inline(line))

    # Flush an unclosed fenced code block (LLM output truncated mid-block).
    if in_code:
        label = f"[{code_lang}]" if code_lang else "[代码]"
        out.append(f"┌─{label}─")
        for cl in code_lines:
            out.append("│ " + cl)
        out.append("└──────")

    return "\n".join(out).strip()


def _inline(text: str) -> str:
    """Strip inline Markdown from a single line."""
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text)
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"[\1]", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    return text

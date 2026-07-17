"""OneBot 11 message segment extraction helpers.

These are pure functions over the ``message`` array of an OneBot 11 event.
No I/O, no downloads — just shape extraction so the parser stays readable.
"""
from __future__ import annotations


def extract_text(segments: list[dict]) -> str:
    """Concatenate text from text/at/file segments into a plain string."""
    parts: list[str] = []
    for s in segments:
        t = s.get("type")
        data = s.get("data", {}) or {}
        if t == "text":
            parts.append(data.get("text", ""))
        elif t == "at":
            parts.append(f"@{data.get('qq', '')}")
        elif t == "file":
            fname = data.get("file") or data.get("name", "文件")
            parts.append(f"[文件: {fname}]")
    return "".join(parts).strip()


def extract_text_with_placeholders(
    segments: list[dict], start_index: int = 0,
) -> tuple[str, list[dict]]:
    """Extract text and insert placeholder tokens at media-segment positions.

    Returns ``(text, media_markers)``:

    - ``text``: text with temporary tokens like ``{[IMG:1]}`` where media
      segments appeared. Tokens are later replaced by rendered placeholders
      (e.g. ``[图1]``) once download status is known.
    - ``media_markers``: list of dicts ``{marker, kind, url, file_info, index}``
      in the order their tokens appear in *text*.

    ``start_index`` is the 0-based global media index to begin numbering from.
    Displayed placeholder numbers are ``index + 1``.
    """
    parts: list[str] = []
    markers: list[dict] = []
    idx = start_index
    for s in segments:
        t = s.get("type")
        data = s.get("data", {}) or {}
        if t == "text":
            parts.append(data.get("text", ""))
        elif t == "at":
            parts.append(f"@{data.get('qq', '')}")
        elif t == "image":
            url = data.get("url") or data.get("file", "")
            marker = f"{{[IMG:{idx}]}}"
            parts.append(marker)
            markers.append({"marker": marker, "kind": "image", "url": url, "index": idx})
            idx += 1
        elif t == "record":
            url = data.get("url") or data.get("file", "")
            marker = f"{{[RECORD:{idx}]}}"
            parts.append(marker)
            markers.append({"marker": marker, "kind": "record", "url": url, "index": idx})
            idx += 1
        elif t == "video":
            url = data.get("url") or data.get("file", "")
            marker = f"{{[VIDEO:{idx}]}}"
            parts.append(marker)
            markers.append({"marker": marker, "kind": "video", "url": url, "index": idx})
            idx += 1
        elif t == "file":
            fname = data.get("file") or data.get("name", "文件")
            marker = f"{{[FILE:{idx}:{fname}]}}"
            parts.append(marker)
            markers.append({
                "marker": marker,
                "kind": "file",
                "file_info": {
                    "name": fname,
                    "file_id": data.get("file_id", ""),
                    "url": data.get("url", ""),
                    "size": data.get("file_size", 0),
                },
                "index": idx,
            })
            idx += 1
    return "".join(parts).strip(), markers


def extract_reply_id(segments: list[dict]) -> int | None:
    for s in segments:
        if s.get("type") == "reply":
            try:
                return int(s["data"]["id"])
            except (KeyError, ValueError, TypeError):
                return None
    return None


def extract_forward_id(segments: list[dict]) -> str | None:
    for s in segments:
        if s.get("type") == "forward":
            fid = s.get("data", {}).get("id")
            return str(fid) if fid else None
    return None


def extract_forward_content(segments: list[dict]) -> list[dict] | None:
    """Return the inline ``content`` array of the first ``forward`` segment,
    or ``None`` if absent/empty.

    NapCat populates ``data.content`` with a list of message objects (each
    ``{sender, message, message_id, ...}``) that is structurally identical
    to the top-level ``messages`` array returned by ``get_forward_msg``.
    This lets us expand nested forwards without a second API call (NapCat
    refuses per-id queries for inner forwards with retcode=1200).
    """
    for s in segments:
        if s.get("type") == "forward":
            content = s.get("data", {}).get("content")
            return content or None
    return None


def has_bot_mention(segments: list[dict], self_id: str) -> bool:
    return any(
        s.get("type") == "at" and str(s.get("data", {}).get("qq")) == self_id
        for s in segments
    )


def has_bot_mention_first(segments: list[dict], self_id: str) -> bool:
    """Return True only if the first segment is an @bot mention.

    Leading ``reply`` segments are skipped: OneBot quotes put the ``reply``
    segment first, so a quoted reply like ``[reply, at(bot), text]`` should
    still count as "the @bot mention is first". Other segment types
    (notably ``forward``) are NOT skipped, because they carry real content
    and cannot coexist with an @bot trigger in OneBot semantics.
    """
    i = 0
    n = len(segments)
    while i < n and segments[i].get("type") == "reply":
        i += 1
    if i >= n:
        return False
    s = segments[i]
    return s.get("type") == "at" and str(s.get("data", {}).get("qq")) == self_id


def strip_first_bot_mention(segments: list[dict], self_id: str) -> list[dict]:
    """Remove only a *leading* @bot mention, preserving non-leading ones.

    Leading ``reply`` segments are skipped first (OneBot puts the ``reply``
    segment ahead of the ``at`` segment in quoted messages, mirroring
    :func:`has_bot_mention_first`). After the skipped replies, if the next
    segment is an @bot mention (``type == "at"`` with ``data.qq == self_id``),
    it is dropped; otherwise the list is returned unchanged. Non-leading
    @bot mentions are always preserved to keep the message complete.
    """
    i = 0
    n = len(segments)
    while i < n and segments[i].get("type") == "reply":
        i += 1
    if i < n:
        s = segments[i]
        if s.get("type") == "at" and str(s.get("data", {}).get("qq")) == self_id:
            return segments[:i] + segments[i + 1:]
    return segments


def sender_display(sender: dict) -> str:
    """Best-effort display name for a OneBot sender object."""
    return sender.get("card") or sender.get("nickname") or str(sender.get("user_id", ""))

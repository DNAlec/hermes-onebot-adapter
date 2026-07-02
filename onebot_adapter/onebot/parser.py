"""OneBot 11 event parser.

Reduces raw OneBot 11 event dicts into :class:`NormalizedEvent` + a list of
:class:`MediaPayload`. Handles:

  * group @bot mention filtering
  * merged-forward (合并转发) recursive expansion via ``get_forward_msg``
    (top level) plus inline ``forward.data.content`` (NapCat nested forwards),
    with level-numbered begin/end tags
  * reply context via ``get_msg`` (text / image / voice / video / file / forward)
  * multi-media download (all images/videos/files in a message, not just one)
    with global count limit and per-file size limit
  * placeholder insertion at media positions in text, preserving layout
  * skipped-media placeholders with detailed reasons (size/count/failure)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot import segments as seg
from onebot_adapter.onebot.media import make_media_payload
from onebot_adapter.relay.protocol import FilteredEvent, MediaPayload, NormalizedEvent

if TYPE_CHECKING:
    from onebot_adapter.onebot.name_resolver import NameResolver

logger = logging.getLogger(__name__)

_MAX_FORWARD_DEPTH = 4

_MEDIA_CLASS_MAP = {"image": "image", "video": "video", "record": "audio", "file": "file"}

_AT_PATTERN = re.compile(r"@(\d{5,11})")


def _format_sender_prefix(
    name: str, qq_id: str, seq: str = "",
    admin_suffix: str = "",
) -> str:
    """Build a sender prefix like ``[昵称(QQ号)#序号]``.

    When *qq_id* is empty, only the name is used.  *seq* (when non-empty)
    is appended as ``#seq`` inside the brackets — for group messages this
    is the per-group sequence (``real_seq``), for DMs it's the global
    ``message_id``.  *admin_suffix* (e.g. ``(管理员)``) is appended after
    the QQ号, before ``#seq``.
    """
    base = f"[{name}({qq_id})" if qq_id else f"[{name}"
    if admin_suffix:
        base += admin_suffix
    if seq:
        base += f"#{seq}"
    base += "]"
    return base


@dataclass
class MediaDownloadContext:
    """Shared state across a single ``parse_event`` call.

    Tracks the global media index (so placeholder numbers align with
    ``media_ids`` ordering) and enforces the total download count limit
    across main message, forward expansion, and reply context.

    ``skipped`` collects per-marker skip records (count/size/download-failure)
    so the adapter service can send a single fused reject reply to the user;
    forward-level skips (depth/fetch-failure) do NOT populate this list.
    """
    counter: int = 0          # 0-based; displayed placeholder number = counter + 1
    downloaded: int = 0
    max_count: int = 10
    max_bytes: int = 5 * 1024 * 1024
    skipped: list[dict] = field(default_factory=list)


# ── Placeholder rendering ────────────────────────────────────────────────


def _render_placeholder(
    marker: dict, status: str, reason: str = "", detail: str = "",
) -> str:
    """Render a final placeholder string from a media marker and status.

    *status* is one of:
      ``"ok"``            — download succeeded
      ``"skipped"``      — download skipped (reason/detail explain why)
      ``"converted_raw"`` — voice conversion failed but raw bytes retained
    """
    kind = marker["kind"]
    idx = marker["index"] + 1  # 1-based display number
    if kind == "image":
        label = f"[图{idx}]"
    elif kind == "video":
        label = f"[视频{idx}]"
    elif kind == "record":
        label = f"[语音{idx}]"
    elif kind == "file":
        name = marker.get("file_info", {}).get("name", "")
        label = f"[文件{idx}:{name}]" if name else f"[文件{idx}]"
    else:
        label = f"[媒体{idx}]"

    if status == "skipped":
        suffix = f"(已跳过:{reason}"
        if detail:
            suffix += f":{detail}"
        suffix += ")"
        label += suffix
    elif status == "converted_raw":
        label += "(语音转换失败,保留原始格式)"
    return label


async def _download_marker(
    marker: dict,
    session: aiohttp.ClientSession | None,
    dl_ctx: MediaDownloadContext,
) -> tuple[str, str, str, MediaPayload | None]:
    """Download a single media marker.

    Returns ``(status, reason, detail, media_payload)`` where *status* is
    ``"ok"``, ``"skipped"``, or ``"converted_raw"``.
    """
    kind = marker["kind"]

    # Build a skip record (shared shape for all skip reasons). ``idx`` is the
    # 1-based display number so the reject reply matches the [图N] placeholder.
    def _skip_record(reason: str, detail: str = "") -> dict:
        return {
            "kind": kind,
            "idx": marker["index"] + 1,
            "name": marker.get("file_info", {}).get("name", "") if kind == "file" else "",
            "reason": reason,
            "detail": detail,
        }

    # Check count limit first
    if dl_ctx.downloaded >= dl_ctx.max_count:
        rec = _skip_record(
            "超出数量限制",
            f"已下载{dl_ctx.downloaded}个达到上限{dl_ctx.max_count}",
        )
        dl_ctx.skipped.append(rec)
        return ("skipped", rec["reason"], rec["detail"], None)

    if kind == "file":
        fi = marker["file_info"]
        url = fi.get("url", "")
        name = fi.get("name", "")
    else:
        url = marker.get("url", "")
        name = ""

    if not url:
        rec = _skip_record("下载失败", "无URL")
        dl_ctx.skipped.append(rec)
        return ("skipped", rec["reason"], rec["detail"], None)

    media_class = _MEDIA_CLASS_MAP.get(kind, "file")
    mp, reason = await make_media_payload(
        session, url,
        media_class=media_class, name=name, max_bytes=dl_ctx.max_bytes,
    )
    if mp is not None:
        dl_ctx.downloaded += 1
        if reason:
            # Conversion note (voice) — raw bytes retained
            return ("converted_raw", reason, "", mp)
        return ("ok", "", "", mp)

    # Download failed or size exceeded — reason from make_media_payload
    rec = _skip_record(reason or "下载失败")
    dl_ctx.skipped.append(rec)
    return ("skipped", rec["reason"], rec["detail"], None)


# ── @ mention name resolution ─────────────────────────────────────────────


async def _resolve_at_mentions(
    text: str, group_id: str, name_resolver: NameResolver | None,
) -> str:
    """Replace ``@QQ号`` with ``@QQ号(昵称)`` in *text*.

    Bot's own @ mentions are already stripped by ``strip_bot_mention()``,
    so remaining @ mentions are all other users (or bot itself when
    ``group_require_mention`` is False).
    """
    if not name_resolver:
        return text

    qq_numbers = set(_AT_PATTERN.findall(text))
    if not qq_numbers:
        return text

    # Resolve all unique QQ numbers

    tasks = {qq: name_resolver.resolve(qq, group_id) for qq in qq_numbers}
    names: dict[str, str] = {}
    for qq, coro in tasks.items():
        try:
            names[qq] = await coro
        except Exception:
            logger.debug("resolve qq %s failed", qq)
            names[qq] = ""

    def _replace(match: re.Match) -> str:
        qq = match.group(1)
        name = names.get(qq, "")
        if name:
            return f"@{qq}({name})"
        return f"@{qq}(未知用户)"

    return _AT_PATTERN.sub(_replace, text)


# ── /command extraction & filtering ───────────────────────────────────────


def _extract_command_name(segments: list[dict]) -> str | None:
    """Extract a slash-command name from *segments* after @bot stripping.

    The command is detected when the leading text (after lstrip) starts with
    ``/``.  The name is the first whitespace-delimited token after ``/``,
    lowercased, with any ``@botname`` suffix stripped (Telegram-style).  A
    bare ``/`` or a token containing ``/`` (file path) returns None.
    """
    text = seg.extract_text(segments).lstrip()
    if not text.startswith("/"):
        return None
    parts = text.split(maxsplit=1)
    raw = parts[0][1:].lower() if parts else None
    if not raw:
        return None
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    if "/" in raw:
        return None
    return raw


def _check_command_filter(
    event: dict[str, Any],
    segments: list[dict],
    config: AdapterConfig,
    is_group: bool,
    group_id: str,
    sender_id: str,
    sender_name: str,
    chat_id: str,
    is_known_command_fn: Any,
    canonical_command_name_fn: Any,
) -> FilteredEvent | None:
    """Check the /command filter against *segments*.

    Returns a :class:`FilteredEvent` if the message is a denied /command,
    otherwise ``None`` (not a command, or allowed).  Uses *config*'s
    ``check_command_permission`` and the relay-provided *is_known_command_fn*
    / *canonical_command_name_fn* to resolve the command's registration and
    permission level.
    """
    cmd = _extract_command_name(segments)
    if not cmd:
        return None  # not a /command

    gid = group_id if is_group else None
    is_known = False
    canonical = cmd
    if is_known_command_fn is not None:
        try:
            is_known = bool(is_known_command_fn(cmd))
        except Exception:
            logger.exception("is_known_command_fn failed for %r", cmd)
            is_known = False
    if canonical_command_name_fn is not None and is_known:
        try:
            canonical = str(canonical_command_name_fn(cmd)) or cmd
        except Exception:
            canonical = cmd

    allowed, reject_msg = config.check_command_permission(
        gid, sender_id, canonical, is_known,
    )
    if allowed:
        return None

    reply_to_id = seg.extract_reply_id(segments)
    return FilteredEvent(
        chat_id=chat_id,
        chat_type="group" if is_group else "dm",
        user_id=sender_id,
        user_name=sender_name,
        command_name=canonical,
        reject_message=reject_msg or "",
        message_id=str(event.get("message_id", "")),
        reply_to_message_id=str(reply_to_id) if reply_to_id else None,
        timestamp=float(event.get("time", 0) or 0),
    )


# ── Main entry point ─────────────────────────────────────────────────────


async def parse_event(
    event: dict[str, Any],
    *,
    self_id: str,
    group_require_mention: bool,
    media_max_bytes: int,
    api: Any = None,
    session: aiohttp.ClientSession | None = None,
    config: AdapterConfig | None = None,
    media_max_count: int = 10,
    name_resolver: NameResolver | None = None,
    mention_first_only: bool = False,
    trigger_keywords: list[str] | None = None,
    keyword_first_only: bool = False,
    keep_mention: bool = False,
    is_known_command_fn: Any = None,
    canonical_command_name_fn: Any = None,
) -> tuple[NormalizedEvent, list[MediaPayload]] | FilteredEvent | None:
    """Parse a OneBot 11 message event.

    Returns:
        * ``(NormalizedEvent, [MediaPayload, ...])`` for normal messages.
        * :class:`FilteredEvent` when the message is a /command that was
          denied by the command filter (the caller should send the reject
          message and skip forwarding to Hermes).
        * ``None`` for non-message events, filtered messages, or empty
          messages (no text and no media).

    When *config* is provided, it overrides *group_require_mention*,
    *media_max_bytes*, and *media_max_count* with per-group resolved values,
    and applies group allowlist/blocklist, session-mode chat_id, custom
    prompts, and admin computation.

    *is_known_command_fn* / *canonical_command_name_fn* are optional callables
    provided by the relay layer to check whether a command name is registered
    in Hermes.  When supplied and *config.command_filter_enabled* is True,
    /commands are checked against the permission policy before media download.

    Triggering: ``group_require_mention`` enables @-mention triggering
    (any-position unless ``mention_first_only`` is True, which requires the
    first segment to be the @bot mention). ``trigger_keywords`` enables
    keyword triggering (any-position unless ``keyword_first_only`` requires
    the keyword at the text start). A message triggers if it satisfies any
    enabled check (OR). If no check is enabled, all messages pass through.
    """
    if event.get("post_type") != "message":
        return None

    is_group = event.get("message_type") == "group"
    sender = event.get("sender", {}) or {}
    sender_id = str(event.get("user_id", ""))
    sender_name = seg.sender_display(sender)
    group_id = str(event.get("group_id", "")) if is_group else ""
    logger.debug(
        "parse_event: post_type=%s msg_type=%s user_id=%s card=%r nick=%r group=%s msg_id=%s real_seq=%s segs=%s",
        event.get("post_type"), event.get("message_type"),
        sender_id, sender.get("card"), sender.get("nickname"),
        group_id, event.get("message_id"), event.get("real_seq"),
        [s.get("type") for s in (event.get("message", []) or [])],
    )

    # ── Group filtering (config-driven) ──────────────────────────────
    channel_prompt: str | None = None
    is_admin = False
    if config and is_group:
        if not config.is_group_user_allowed(group_id, sender_id):
            return None
        gc = config.get_group_config(group_id)
        if not gc.enabled:
            return None
        group_require_mention = config.resolve_require_mention(group_id)
        mention_first_only = config.resolve_mention_first_only(group_id)
        trigger_keywords = config.resolve_trigger_keywords(group_id)
        keyword_first_only = config.resolve_keyword_first_only(group_id)
        keep_mention = config.resolve_keep_mention(group_id)
        media_max_bytes = config.resolve_media_max_bytes(group_id)
        media_max_count = config.resolve_media_max_count(group_id)
        channel_prompt = config.resolve_custom_prompt(group_id)
        is_admin = config.is_admin(sender_id, group_id)
    elif config and not is_group:
        if not config.is_dm_allowed(sender_id):
            return None
        media_max_bytes = config.resolve_media_max_bytes()
        media_max_count = config.resolve_media_max_count()
        is_admin = config.is_admin(sender_id)

    # ── Session mode → chat_id ────────────────────────────────────────
    if is_group:
        session_mode = config.resolve_session_mode(group_id) if config else "shared"
        if session_mode == "per_user":
            chat_id = f"group:{group_id}:user:{sender_id}"
        else:
            chat_id = f"group:{group_id}"
    else:
        chat_id = sender_id

    # ── chat_name (用于 Hermes Source 行) ─────────────────────────────
    # 群聊: "群号(群名)" 或 "群号";私聊: 发送者昵称
    # group_name 也用于下方 message_show_group_id 标识。
    chat_name = ""
    group_name = ""
    if is_group:
        if name_resolver is not None:
            group_name = await name_resolver.resolve_group_name(group_id)
        chat_name = f"{group_id}({group_name})" if group_name else str(group_id)
    else:
        chat_name = sender_name

    raw_segments: list[dict] = event.get("message", []) or []

    # Group: trigger gating (@-mention and/or keyword), then strip @bot mention
    if is_group:
        checks: list[bool] = []
        if group_require_mention and self_id:
            if mention_first_only:
                checks.append(seg.has_bot_mention_first(raw_segments, self_id))
            else:
                checks.append(seg.has_bot_mention(raw_segments, self_id))
        kws = trigger_keywords or []
        if kws:
            plain_text = seg.extract_text(raw_segments)
            if keyword_first_only:
                checks.append(any(plain_text.startswith(kw) for kw in kws))
            else:
                checks.append(any(kw in plain_text for kw in kws))
        if checks and not any(checks):
            return None  # some trigger required, none satisfied → drop
        if group_require_mention and self_id and not keep_mention:
            raw_segments = seg.strip_bot_mention(raw_segments, self_id)

    # ── /command filter ───────────────────────────────────────────────
    # After @bot stripping (group) or on raw segments (DM), check whether the
    # message is a /command and whether the sender has permission to use it.
    # This runs *before* media download to avoid wasting bandwidth on denied
    # commands.  Returns a FilteredEvent when denied; the caller sends the
    # reject message via the OneBot HTTP API and skips Hermes forwarding.
    if config and config.resolve_command_filter_enabled(group_id if is_group else None):
        filtered = _check_command_filter(
            event, raw_segments, config, is_group, group_id, sender_id, sender_name,
            chat_id, is_known_command_fn, canonical_command_name_fn,
        )
        if filtered is not None:
            return filtered

    dl_ctx = MediaDownloadContext(max_count=media_max_count, max_bytes=media_max_bytes)

    # Media ordering matches placeholder numbering:
    #   1. forward media   2. reply media   3. main message media
    all_media: list[MediaPayload] = []
    reply_to_text: str | None = None
    reply_to_id: int | None = None

    # ── Expand merged-forward (合并转发) ──────────────────────────────
    forward_id = seg.extract_forward_id(raw_segments)
    forward_text = ""
    if forward_id and api:
        logger.debug("parse_event: expanding forward msg_id=%s", forward_id)
        forward_text, fwd_media = await _expand_forward(
            api, forward_id, session, dl_ctx, depth=0,
            name_resolver=name_resolver, group_id=group_id,
        )
        # _expand_messages already wraps the result in
        # [合并转发开始:1]...[合并转发结束:1] — no extra wrapping here.
        all_media.extend(fwd_media)

    # ── Reply context (引用回复) ──────────────────────────────────────
    reply_to_id = seg.extract_reply_id(raw_segments)
    if reply_to_id and api:
        logger.debug("parse_event: fetching reply context msg_id=%s", reply_to_id)
        reply_to_text, reply_media = await _build_reply_context(
            api, reply_to_id, session, dl_ctx,
            name_resolver=name_resolver, group_id=group_id,
        )
        all_media.extend(reply_media)

    # ── Main message text + media ─────────────────────────────────────
    text, media_markers = seg.extract_text_with_placeholders(
        raw_segments, start_index=dl_ctx.counter,
    )
    if forward_text:
        text = forward_text + ("\n" + text if text else "")
    logger.debug(
        "parse_event: extracted text len=%d media_markers=%d forward=%s",
        len(text), len(media_markers), bool(forward_text),
    )

    main_media: list[MediaPayload] = []
    for i, marker in enumerate(media_markers):
        logger.debug(
            "parse_event: download media %d/%d type=%s url=%s",
            i + 1, len(media_markers), marker.get("type"), str(marker.get("url", ""))[:120],
        )
        status, reason, detail, mp = await _download_marker(marker, session, dl_ctx)
        logger.debug(
            "parse_event: media %d/%d status=%s reason=%s size=%d",
            i + 1, len(media_markers), status, reason, len(mp.data) if mp else 0,
        )
        if mp:
            main_media.append(mp)
        dl_ctx.counter += 1
        rendered = _render_placeholder(marker, status, reason, detail)
        text = text.replace(marker["marker"], rendered)

    all_media.extend(main_media)

    # ── Resolve @ mentions to @QQ号(昵称) ──────────────────────────────
    if name_resolver:
        text = await _resolve_at_mentions(text, group_id, name_resolver)

    # ── Infer message_type ────────────────────────────────────────────
    msg_type = _infer_message_type(all_media)

    # Group chat: prefix sender name + QQ号 (except slash commands)
    if is_group and text:
        if text.lstrip().startswith("/"):
            text = text.lstrip()
        else:
            admin_suffix = "(管理员)" if is_admin else ""
            # 群聊前缀展示 real_seq(群内递增序号),拿不到时回退 message_id
            group_seq = str(event.get("real_seq", "") or event.get("message_id", ""))
            prefix = _format_sender_prefix(
                sender_name, sender_id, group_seq,
                admin_suffix=admin_suffix,
            )
            text = f"{prefix}: {text}"

    # Optional group-id label at the head of the main message text.
    # Only injected for group chats when ``message_show_group_id`` is on,
    # and skipped for slash commands.  Format: ``[群:42(测试群)]`` or
    # ``[群:42]`` when group name is unavailable.
    if is_group and config and config.resolve_message_show_group_id(group_id) \
            and text and not text.lstrip().startswith("/"):
        gid_label = f"{group_id}({group_name})" if group_name else str(group_id)
        text = f"[群:{gid_label}]\n{text}"

    if not text and not all_media:
        return None

    norm = NormalizedEvent(
        message_id=str(event.get("message_id", "")),
        chat_id=chat_id,
        chat_type="group" if is_group else "dm",
        user_id=sender_id,
        user_name=sender_name,
        text=text,
        message_type=msg_type,  # type: ignore[arg-type]
        media_ids=[mp.id for mp in all_media],
        media_types=[mp.descriptor.mime for mp in all_media],
        reply_to_message_id=str(reply_to_id) if reply_to_id else None,
        reply_to_text=reply_to_text,
        timestamp=float(event.get("time", 0) or 0),
        channel_prompt=channel_prompt,
        is_admin=is_admin,
        chat_name=chat_name,
        real_seq=str(event.get("real_seq", "") or ""),
        raw=event,
        skipped_media=list(dl_ctx.skipped),
    )
    logger.debug(
        "parse_event: normalized chat_id=%s msg_type=%s text_preview=%r media=%d",
        norm.chat_id, norm.message_type, (norm.text or "")[:120], len(all_media),
    )
    return norm, all_media


def _infer_message_type(media: list[MediaPayload]) -> str:
    """Determine message_type from the set of downloaded media."""
    if not media:
        return "text"
    kinds: set[str] = set()
    for mp in media:
        mime = mp.descriptor.mime
        if mime.startswith("image"):
            kinds.add("photo")
        elif mime.startswith("audio"):
            kinds.add("voice")
        elif mime.startswith("video"):
            kinds.add("video")
        else:
            kinds.add("document")
    if len(kinds) == 1:
        return kinds.pop()
    return "mixed"


# ── Reply context ────────────────────────────────────────────────────────


async def _build_reply_context(
    api: Any,
    reply_id: int,
    session: aiohttp.ClientSession | None,
    dl_ctx: MediaDownloadContext,
    name_resolver: NameResolver | None = None,
    group_id: str = "",
) -> tuple[str | None, list[MediaPayload]]:
    """Fetch the quoted message and build reply text + any quoted media."""
    try:
        quoted = await api.get_msg(reply_id)
    except Exception as exc:
        logger.warning("get_msg failed for reply_id=%s: %s", reply_id, exc)
        return None, []
    if not quoted:
        return None, []

    q_sender = quoted.get("sender", {}) or {}
    q_name = seg.sender_display(q_sender)
    q_id = str(q_sender.get("user_id", ""))
    q_msg_id = str(quoted.get("message_id", ""))
    q_real_seq = str(quoted.get("real_seq", "") or "")
    q_seq = q_real_seq or q_msg_id  # 群聊优先 real_seq,拿不到回退 message_id
    q_segments: list[dict] = quoted.get("message", []) or []
    logger.debug(
        "get_msg reply context: reply_id=%s user_id=%s card=%r nick=%r real_seq=%s msg_id=%s group_id=%s segs=%s",
        reply_id, q_sender.get("user_id"), q_sender.get("card"), q_sender.get("nickname"),
        quoted.get("real_seq"), quoted.get("message_id"), quoted.get("group_id"),
        [s.get("type") for s in q_segments],
    )
    media: list[MediaPayload] = []

    # Quoted message may itself be a forward
    q_forward_id = seg.extract_forward_id(q_segments)
    if q_forward_id:
        fwd_text, fwd_media = await _expand_forward(
            api, q_forward_id, session, dl_ctx, depth=0,
            name_resolver=name_resolver, group_id=group_id,
        )
        media.extend(fwd_media)
        label = fwd_text or "（无内容）"
        q_prefix = _format_sender_prefix(q_name, q_id, q_seq)
        reply_text = f"{q_prefix}: {label}"
        return reply_text, media

    # Regular quoted message: extract text with placeholders
    q_text, markers = seg.extract_text_with_placeholders(
        q_segments, start_index=dl_ctx.counter,
    )

    for marker in markers:
        status, reason, detail, mp = await _download_marker(marker, session, dl_ctx)
        if mp:
            media.append(mp)
        dl_ctx.counter += 1
        rendered = _render_placeholder(marker, status, reason, detail)
        q_text = q_text.replace(marker["marker"], rendered)

    # Resolve @ mentions before adding sender prefix (avoids matching
    # numbers in the prefix as QQ IDs)
    if q_text and name_resolver:
        q_text = await _resolve_at_mentions(q_text, group_id, name_resolver)

    q_prefix = _format_sender_prefix(q_name, q_id, q_seq)
    reply_text = f"{q_prefix}: {q_text}" if q_text else None

    return reply_text, media


# ── Forward expansion (recursive) ────────────────────────────────────────


async def _expand_forward(
    api: Any,
    forward_id: str,
    session: aiohttp.ClientSession | None,
    dl_ctx: MediaDownloadContext,
    depth: int = 0,
    name_resolver: NameResolver | None = None,
    group_id: str = "",
) -> tuple[str, list[MediaPayload]]:
    """Fetch a merged-forward by id and expand it.

    This is the API-fetching entry point: it calls ``get_forward_msg`` once
    to obtain the top-level ``messages`` array, then delegates to
    :func:`_expand_messages` for traversal (which reads nested forwards
    from their inline ``data.content`` — NapCat populates this and refuses
    per-id queries for inner forwards with retcode=1200). Depth is guarded
    here too to cap malicious/huge forwards.
    """
    if depth > _MAX_FORWARD_DEPTH:
        return "[合并转发(已跳过:超过最大深度)]", []
    try:
        fwd_data = await api.get_forward_msg(forward_id)
    except Exception as exc:
        logger.warning("get_forward_msg failed id=%s: %s", forward_id, exc)
        return "[合并转发(已跳过:读取失败)]", []
    messages: list[dict] = (fwd_data or {}).get("messages", []) or []
    logger.debug(
        "get_forward_msg response: forward_id=%s messages=%d depth=%d",
        forward_id, len(messages), depth,
    )
    for i, msg in enumerate(messages):
        s = msg.get("sender", {}) or {}
        seg_types = [s.get("type") for s in (msg.get("message", []) or [])]
        logger.debug(
            "  forward[%d]: user_id=%s card=%r nickname=%r real_seq=%s message_id=%s group_id=%s segments=%s",
            i, s.get("user_id"), s.get("card"), s.get("nickname"),
            msg.get("real_seq"), msg.get("message_id"), msg.get("group_id"), seg_types,
        )
    return await _expand_messages(
        messages, session, dl_ctx, depth,
        name_resolver=name_resolver, group_id=group_id,
    )


async def _expand_messages(
    messages: list[dict],
    session: aiohttp.ClientSession | None,
    dl_ctx: MediaDownloadContext,
    depth: int,
    name_resolver: NameResolver | None = None,
    group_id: str = "",
) -> tuple[str, list[MediaPayload]]:
    """Expand a list of message objects (each ``{sender, message, ...}``).

    Shared between the top-level ``get_forward_msg`` response and the inline
    ``forward.data.content`` array. Pure traversal: no API calls — nested
    forwards are read from their inline ``content`` field. Begin/end tags
    carry a level number to help the LLM understand nesting structure.
    """
    if depth > _MAX_FORWARD_DEPTH:
        return "[合并转发(已跳过:超过最大深度)]", []
    parts: list[str] = []
    media: list[MediaPayload] = []
    level = depth + 1
    logger.debug("_expand_messages: depth=%d messages=%d", depth, len(messages))

    for msg in messages:
        sender = msg.get("sender", {}) or {}
        name = seg.sender_display(sender)
        fwd_prefix = _format_sender_prefix(name, "")
        msg_segments: list[dict] = msg.get("message", []) or []

        # Nested forward: expand from inline content (NapCat) — no API call.
        nested_content = seg.extract_forward_content(msg_segments)
        if nested_content:
            nested_text, nested_media = await _expand_messages(
                nested_content, session, dl_ctx, depth + 1,
                name_resolver=name_resolver, group_id=group_id,
            )
            if nested_text:
                parts.append(f"{fwd_prefix}: {nested_text}")
            media.extend(nested_media)
            continue

        # Extract text with placeholders and download all media
        msg_text, markers = seg.extract_text_with_placeholders(
            msg_segments, start_index=dl_ctx.counter,
        )

        for marker in markers:
            status, reason, detail, mp = await _download_marker(marker, session, dl_ctx)
            if mp:
                media.append(mp)
            dl_ctx.counter += 1
            rendered = _render_placeholder(marker, status, reason, detail)
            msg_text = msg_text.replace(marker["marker"], rendered)

        # Resolve @ mentions in sub-message text
        if name_resolver:
            msg_text = await _resolve_at_mentions(msg_text, group_id, name_resolver)

        if msg_text:
            parts.append(f"{fwd_prefix}: {msg_text}")

    forward_text = "\n".join(parts)
    if forward_text:
        forward_text = f"[合并转发开始:{level}]\n{forward_text}\n[合并转发结束:{level}]"
    return forward_text, media

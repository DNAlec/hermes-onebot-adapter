"""OneBot 11 event parser.

Reduces raw OneBot 11 event dicts into a :class:`NormalizedEvent`. Handles:

  * group @bot mention filtering
  * merged-forward (合并转发) recursive expansion via ``get_forward_msg``
    (top level) plus inline ``forward.data.content`` (NapCat nested forwards),
    with level-numbered begin/end tags
  * reply context via ``get_msg`` (text / image / voice / video / file / forward)
  * media delivery — controlled by ``media_delivery_mode``:
    - ``cache`` (default): ``media_items`` carries one entry per media segment
      so the plugin can download them via ``cache_image_from_url`` etc.; text
      placeholders are rendered without URLs (``[图1]``) so the LLM still sees
      media positions. No media is downloaded by the adapter; no binary WS
      frames are produced.
    - ``passthrough``: media URLs are rendered inline in ``text``
      as placeholders like ``[图1](https://...)`` so the LLM can fetch them
      on demand via code execution or vision tools. ``media_items`` is empty.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from onebot_adapter.config import MEDIA_DELIVERY_CACHE, AdapterConfig
from onebot_adapter.logging_utils import text_summary
from onebot_adapter.onebot import segments as seg
from onebot_adapter.relay.protocol import DroppedEvent, FilteredEvent, MediaItem, NormalizedEvent

if TYPE_CHECKING:
    from onebot_adapter.onebot.name_resolver import NameResolver

logger = logging.getLogger(__name__)

_MAX_FORWARD_DEPTH = 4

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
class _MediaCounter:
    """Tracks the global media index across a single ``parse_event`` call.

    The counter ensures placeholder numbers (``[图1]``, ``[视频2]``…) align
    across forward expansion, reply context, and the main message, even
    though no media is downloaded — the numbers are purely for the LLM's
    reference so it can correlate a placeholder with its URL.
    """

    counter: int = 0  # 0-based; displayed placeholder number = counter + 1


@dataclass
class _ParseOptions:
    self_id: str
    group_require_mention: bool
    api: Any
    config: AdapterConfig | None
    name_resolver: NameResolver | None
    mention_first_only: bool
    trigger_keywords: list[str] | None
    keyword_first_only: bool
    strip_first_mention: bool
    is_known_command_fn: Callable[[str], bool] | None
    canonical_command_name_fn: Callable[[str], str] | None
    media_delivery_mode: str
    bot_blacklist_match_fn: Callable[[str, str | None], Any] | None


@dataclass
class _MessageState:
    is_group: bool
    sender_id: str
    sender_name: str
    group_id: str
    is_admin: bool
    is_global_admin: bool
    include_url: bool
    chat_id: str
    chat_name: str
    group_name: str
    raw_segments: list[dict]


# ── Placeholder rendering ────────────────────────────────────────────────


def _render_url_placeholder(marker: dict, *, include_url: bool = True) -> str:
    """Render a media marker as a placeholder string.

    Passthrough mode (``include_url=True``): ``[图1](https://...)`` /
    ``[视频2](https://...)`` / ``[语音3](https://...)`` /
    ``[文件4:name.ext](https://...)``. When no URL is available, the
    parenthesised part is ``无URL``.

    Cache mode (``include_url=False``): ``[图1]`` / ``[视频2]`` / ``[语音3]`` /
    ``[文件4:name.ext]`` — the URL is omitted because it is carried in
    ``media_items`` for the plugin to download; the LLM only needs to see the
    media's position in the message.
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

    if not include_url:
        return label

    if kind == "file":
        url = marker.get("file_info", {}).get("url", "")
    else:
        url = marker.get("url", "")
    return f"{label}({url or '无URL'})"


def _marker_to_media_item(marker: dict) -> MediaItem:
    """Convert a parser-internal media marker dict to a ``MediaItem``.

    The plugin side receives ``MediaItem`` via the ``media_items`` field of
    ``NormalizedEvent`` and uses it to pick the right ``cache_*_from_url`` /
    ``cache_*_from_bytes`` helper when ``media_delivery_mode == "cache"``.
    """
    kind = marker.get("kind", "")
    idx = marker.get("index", 0)
    if kind == "file":
        info = marker.get("file_info", {}) or {}
        return MediaItem(
            kind="file",
            url=info.get("url", ""),
            name=info.get("name", ""),
            file_id=info.get("file_id", ""),
            index=idx,
        )
    return MediaItem(
        kind=kind,
        url=marker.get("url", ""),
        index=idx,
    )


def _marker_has_url(marker: dict) -> bool:
    """Return True if the marker carries a usable download URL.

    File segments without a URL (only ``file_id``) are skipped in cache mode —
    the LLM can fetch them via the ``onebot_get_file`` tool using ``file_id``.
    """
    kind = marker.get("kind", "")
    if kind == "file":
        return bool(marker.get("file_info", {}).get("url", ""))
    return bool(marker.get("url", ""))


# ── @ mention name resolution ─────────────────────────────────────────────


async def _resolve_at_mentions(
    text: str, group_id: str, name_resolver: NameResolver | None,
) -> str:
    """Replace ``@QQ号`` with ``@QQ号(昵称)`` in *text*.

    Bot's own leading @ mention is already stripped by
    ``strip_first_bot_mention()``, so remaining @ mentions are all other
    users, or a non-leading @bot mention (kept for message completeness),
    or the bot itself when ``group_require_mention`` is False and the
    @bot mention is not in the leading position.
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
    text = seg.extract_text(segments)
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
    is_known_command_fn: Callable[[str], bool] | None,
    canonical_command_name_fn: Callable[[str], str] | None,
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


def _render_bot_blacklist_reject(config: AdapterConfig, entry: Any, user_id: str) -> str:
    """Render the shared dynamic-blacklist rejection template."""
    now = time.time()
    values = {
        "user_id": user_id,
        "scope": entry.scope,
        "remaining": entry.to_dict(now)["remaining"],
        "expires_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.expires_at)),
        "reason": entry.reason,
    }
    message = config.bot_blacklist_reject_message
    for key, value in values.items():
        message = message.replace("{" + key + "}", str(value))
    return message


# ── Main entry point ─────────────────────────────────────────────────────


async def parse_event(
    event: dict[str, Any],
    *,
    self_id: str,
    group_require_mention: bool,
    api: Any = None,
    config: AdapterConfig | None = None,
    name_resolver: NameResolver | None = None,
    mention_first_only: bool = False,
    trigger_keywords: list[str] | None = None,
    keyword_first_only: bool = False,
    strip_first_mention: bool = True,
    is_known_command_fn: Callable[[str], bool] | None = None,
    canonical_command_name_fn: Callable[[str], str] | None = None,
    media_delivery_mode: str = "passthrough",
    bot_blacklist_match_fn: Callable[[str, str | None], Any] | None = None,
) -> NormalizedEvent | FilteredEvent | DroppedEvent | None:
    """Parse a OneBot 11 message event.

    Returns:
        * :class:`NormalizedEvent` for normal messages.
        * :class:`FilteredEvent` when the message is a /command or blacklist
          hit that should get a reject reply (not forwarded to Hermes).
        * :class:`DroppedEvent` when a candidate message is silently dropped
          (``user_filter`` / ``mention`` / ``empty``) — logged at INFO without
          the body, not forwarded to Hermes.
        * ``None`` for heartbeats, unhandled/disabled notices, and other
          non-candidate events.

    Media delivery is controlled by *media_delivery_mode* (defaults to
    ``"cache"``, overridable via *config.media_delivery_mode* when
    *config* is supplied):

      * ``cache``: ``media_items`` carries one entry per media segment so
        the plugin can download them; ``text`` placeholders are rendered
        without URLs (``[图1]``). No media is downloaded by the adapter and
        no binary WS frames are produced in either mode.
      * ``passthrough``: media URLs are rendered inline in ``text`` as
        placeholders like ``[图1](https://...)`` so the LLM can fetch them on
        demand. ``media_items`` is empty.

    When *config* is provided, the per-group trigger settings
    (*group_require_mention*, *mention_first_only*, *trigger_keywords*,
    *keyword_first_only*, *strip_first_mention*) and *media_delivery_mode*
    are **overridden** by the config's resolved values — the standalone
    keyword arguments are only used when *config* is ``None`` (e.g. in tests).
    The config also applies group allowlist/blocklist, session-mode chat_id,
    custom prompts, and admin computation.

    *is_known_command_fn* / *canonical_command_name_fn* are optional callables
    provided by the relay layer to check whether a command name is registered
    in Hermes.  When supplied and *config.command_filter_enabled* is True,
    /commands are checked against the permission policy before media
    placeholders are rendered.

    Triggering: ``group_require_mention`` enables @-mention triggering
    (any-position unless ``mention_first_only`` is True, which requires the
    first segment to be the @bot mention). ``trigger_keywords`` enables
    keyword triggering (any-position unless ``keyword_first_only`` requires
    the keyword at the text start). A message triggers if it satisfies any
    enabled check (OR). If no check is enabled, all messages pass through.
    """
    post_type = event.get("post_type")
    if post_type == "notice":
        return await _parse_notice_event(
            event,
            self_id=self_id,
            config=config,
            name_resolver=name_resolver,
            bot_blacklist_match_fn=bot_blacklist_match_fn,
        )
    if post_type != "message":
        return None
    options = _ParseOptions(
        self_id=self_id,
        group_require_mention=group_require_mention,
        api=api,
        config=config,
        name_resolver=name_resolver,
        mention_first_only=mention_first_only,
        trigger_keywords=trigger_keywords,
        keyword_first_only=keyword_first_only,
        strip_first_mention=strip_first_mention,
        is_known_command_fn=is_known_command_fn,
        canonical_command_name_fn=canonical_command_name_fn,
        media_delivery_mode=media_delivery_mode,
        bot_blacklist_match_fn=bot_blacklist_match_fn,
    )
    state = await _prepare_message_state(event, options)
    if isinstance(state, DroppedEvent):
        return state
    if state is None:
        return None
    filtered = _filter_message(event, state, options)
    if filtered is not None:
        return filtered
    text, reply_to_id, reply_to_text, media_items = await _render_message(event, state, options)
    if not text:
        return _drop_event(
            "empty", event, is_group=state.is_group, sender_id=state.sender_id,
            sender_name=state.sender_name, group_id=state.group_id,
        )

    norm = NormalizedEvent(
        message_id=str(event.get("message_id", "")),
        chat_id=state.chat_id,
        chat_type="group" if state.is_group else "dm",
        user_id=state.sender_id,
        user_name=state.sender_name,
        text=text,
        reply_to_message_id=str(reply_to_id) if reply_to_id else None,
        reply_to_text=reply_to_text,
        timestamp=float(event.get("time", 0) or 0),
        is_admin=state.is_admin,
        is_global_admin=state.is_global_admin,
        chat_name=state.chat_name,
        real_seq=str(event.get("real_seq", "") or ""),
        media_items=media_items,
    )
    logger.debug(
        "parse_event: normalized chat_id=%s text=%s",
        norm.chat_id, text_summary(norm.text),
    )
    return norm


def _drop_event(
    reason: str,
    event: dict[str, Any],
    *,
    is_group: bool,
    sender_id: str,
    sender_name: str,
    group_id: str,
    command_name: str = "",
) -> DroppedEvent:
    return DroppedEvent(
        reason=reason,
        chat_id=f"group:{group_id}" if is_group else sender_id,
        chat_type="group" if is_group else "dm",
        user_id=sender_id,
        user_name=sender_name,
        message_id=str(event.get("message_id", "")),
        chat_name=group_id if is_group else sender_name,
        command_name=command_name,
    )


async def _prepare_message_state(
    event: dict[str, Any], options: _ParseOptions,
) -> _MessageState | DroppedEvent | None:
    is_group = event.get("message_type") == "group"
    sender = event.get("sender", {}) or {}
    sender_id = str(event.get("user_id", ""))
    sender_name = seg.sender_display(sender)
    group_id = str(event.get("group_id", "")) if is_group else ""
    logger.debug(
        "parse_event: post_type=%s msg_type=%s user_id=%s card=%r nick=%r group=%s msg_id=%s real_seq=%s segs=%s",
        event.get("post_type"), event.get("message_type"), sender_id,
        sender.get("card"), sender.get("nickname"), group_id,
        event.get("message_id"), event.get("real_seq"),
        [item.get("type") for item in (event.get("message", []) or [])],
    )
    settings = _resolved_message_settings(options, is_group, group_id, sender_id)
    if settings is None:
        return _drop_event(
            "user_filter", event, is_group=is_group, sender_id=sender_id,
            sender_name=sender_name, group_id=group_id,
        )
    require_mention, mention_first, keywords, keyword_first, strip_mention, is_admin, is_global_admin, mode = settings
    raw_segments: list[dict] = event.get("message", []) or []
    if is_group and not _passes_group_trigger(
        raw_segments,
        options.self_id,
        require_mention,
        mention_first,
        keywords,
        keyword_first,
    ):
        return _drop_event(
            "mention", event, is_group=is_group, sender_id=sender_id,
            sender_name=sender_name, group_id=group_id,
        )
    if is_group and options.self_id and strip_mention:
        raw_segments = seg.strip_first_bot_mention(raw_segments, options.self_id)

    group_name = ""
    if is_group and options.name_resolver is not None:
        group_name = await options.name_resolver.resolve_group_name(group_id)
    chat_id = f"group:{group_id}" if is_group else sender_id
    chat_name = f"{group_id}({group_name})" if group_name else (group_id if is_group else sender_name)
    return _MessageState(
        is_group=is_group,
        sender_id=sender_id,
        sender_name=sender_name,
        group_id=group_id,
        is_admin=is_admin,
        is_global_admin=is_global_admin,
        include_url=mode != MEDIA_DELIVERY_CACHE,
        chat_id=chat_id,
        chat_name=chat_name,
        group_name=group_name,
        raw_segments=raw_segments,
    )


def _resolved_message_settings(
    options: _ParseOptions,
    is_group: bool,
    group_id: str,
    sender_id: str,
) -> tuple[bool, bool, list[str] | None, bool, bool, bool, bool, str] | None:
    config = options.config
    if config is None:
        return (
            options.group_require_mention,
            options.mention_first_only,
            options.trigger_keywords,
            options.keyword_first_only,
            options.strip_first_mention,
            False,
            False,
            options.media_delivery_mode,
        )
    if is_group:
        if not config.is_group_user_allowed(group_id, sender_id) or not config.get_group_config(group_id).enabled:
            return None
        return (
            config.resolve_require_mention(group_id),
            config.resolve_mention_first_only(group_id),
            config.resolve_trigger_keywords(group_id),
            config.resolve_keyword_first_only(group_id),
            config.resolve_strip_first_mention(group_id),
            config.is_admin(sender_id, group_id),
            sender_id in config.global_admins,
            config.media_delivery_mode,
        )
    if not config.is_dm_allowed(sender_id):
        return None
    return (
        options.group_require_mention,
        options.mention_first_only,
        options.trigger_keywords,
        options.keyword_first_only,
        options.strip_first_mention,
        config.is_admin(sender_id),
        sender_id in config.global_admins,
        config.media_delivery_mode,
    )


def _passes_group_trigger(
    raw_segments: list[dict],
    self_id: str,
    require_mention: bool,
    mention_first: bool,
    keywords: list[str] | None,
    keyword_first: bool,
) -> bool:
    checks: list[bool] = []
    if require_mention and self_id:
        mention_matches = seg.has_bot_mention_first(raw_segments, self_id) if mention_first \
            else seg.has_bot_mention(raw_segments, self_id)
        checks.append(mention_matches)
    if keywords:
        plain_text = seg.extract_text(raw_segments)
        keyword_matches = any(plain_text.startswith(keyword) for keyword in keywords) if keyword_first \
            else any(keyword in plain_text for keyword in keywords)
        checks.append(keyword_matches)
    return not checks or any(checks)


def _filter_message(
    event: dict[str, Any], state: _MessageState, options: _ParseOptions,
) -> FilteredEvent | None:
    config = options.config
    if config and config.bot_blacklist_enabled and not state.is_admin \
            and options.bot_blacklist_match_fn is not None:
        entry = options.bot_blacklist_match_fn(
            state.sender_id, state.group_id if state.is_group else None
        )
        if entry is not None:
            return FilteredEvent(
                chat_id=state.chat_id,
                chat_type="group" if state.is_group else "dm",
                user_id=state.sender_id,
                user_name=state.sender_name,
                command_name="",
                reject_message=_render_bot_blacklist_reject(config, entry, state.sender_id),
                message_id=str(event.get("message_id", "")),
                reply_to_message_id=str(event.get("message_id", "")) or None,
                timestamp=float(event.get("time", 0) or 0),
                filter_type="bot_blacklist",
            )
    if config and config.resolve_command_filter_enabled(state.group_id if state.is_group else None):
        return _check_command_filter(
            event,
            state.raw_segments,
            config,
            state.is_group,
            state.group_id,
            state.sender_id,
            state.sender_name,
            state.chat_id,
            options.is_known_command_fn,
            options.canonical_command_name_fn,
        )
    return None


async def _render_message(
    event: dict[str, Any], state: _MessageState, options: _ParseOptions,
) -> tuple[str, int | None, str | None, list[MediaItem]]:
    counter = _MediaCounter()
    media_items: list[MediaItem] = []
    forward_text = await _render_forward_context(state, options, counter, media_items)
    reply_to_id = seg.extract_reply_id(state.raw_segments)
    reply_to_text = None
    if reply_to_id and options.api:
        logger.debug("parse_event: fetching reply context msg_id=%s", reply_to_id)
        reply_to_text = await _build_reply_context(
            options.api,
            reply_to_id,
            counter,
            name_resolver=options.name_resolver,
            group_id=state.group_id,
            include_url=state.include_url,
            media_items=media_items,
        )
    text = _render_main_segments(state.raw_segments, counter, media_items, state.include_url, forward_text)
    if options.name_resolver:
        text = await _resolve_at_mentions(text, state.group_id, options.name_resolver)
    text = _decorate_group_text(text, event, state, options.config)
    return text, reply_to_id, reply_to_text, media_items


async def _render_forward_context(
    state: _MessageState,
    options: _ParseOptions,
    counter: _MediaCounter,
    media_items: list[MediaItem],
) -> str:
    forward_id = seg.extract_forward_id(state.raw_segments)
    if not forward_id or not options.api:
        return ""
    logger.debug("parse_event: expanding forward msg_id=%s", forward_id)
    return await _expand_forward(
        options.api,
        forward_id,
        counter,
        depth=0,
        name_resolver=options.name_resolver,
        group_id=state.group_id,
        include_url=state.include_url,
        media_items=media_items,
    )


def _render_main_segments(
    raw_segments: list[dict],
    counter: _MediaCounter,
    media_items: list[MediaItem],
    include_url: bool,
    forward_text: str,
) -> str:
    text, media_markers = seg.extract_text_with_placeholders(raw_segments, start_index=counter.counter)
    if forward_text:
        text = forward_text + ("\n" + text if text else "")
    logger.debug(
        "parse_event: extracted text len=%d media_markers=%d forward=%s",
        len(text), len(media_markers), bool(forward_text),
    )
    for marker in media_markers:
        counter.counter += 1
        text = text.replace(marker["marker"], _render_url_placeholder(marker, include_url=include_url), 1)
        if not include_url and _marker_has_url(marker):
            media_items.append(_marker_to_media_item(marker))
    return text


def _decorate_group_text(
    text: str, event: dict[str, Any], state: _MessageState, config: AdapterConfig | None,
) -> str:
    if not state.is_group or not text or text.startswith("/"):
        return text
    admin_suffix = "(管理员)" if state.is_admin else ""
    group_seq = str(event.get("real_seq", "") or event.get("message_id", ""))
    prefix = _format_sender_prefix(
        state.sender_name, state.sender_id, group_seq, admin_suffix=admin_suffix,
    )
    text = f"{prefix}: {text}"
    if config and config.resolve_message_show_group_id(state.group_id):
        gid_label = f"{state.group_id}({state.group_name})" if state.group_name else state.group_id
        text = f"[群:{gid_label}]\n{text}"
    return text


# ── Reply context ────────────────────────────────────────────────────────


async def _build_reply_context(
    api: Any,
    reply_id: int,
    counter: _MediaCounter,
    name_resolver: NameResolver | None = None,
    group_id: str = "",
    include_url: bool = True,
    media_items: list[MediaItem] | None = None,
) -> str | None:
    """Fetch the quoted message and build reply text.

    Media markers are rendered as placeholders; in cache mode (``include_url=
    False``) the URLs are omitted from the placeholders and the corresponding
    :class:`MediaItem` entries are appended to *media_items* for the plugin
    to download.
    """
    try:
        quoted = await api.get_msg(reply_id)
    except Exception as exc:
        logger.warning("get_msg failed for reply_id=%s: %s", reply_id, exc)
        return None
    if not quoted:
        return None

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

    # Quoted message may itself be a forward
    q_forward_id = seg.extract_forward_id(q_segments)
    if q_forward_id:
        fwd_text = await _expand_forward(
            api, q_forward_id, counter, depth=0,
            name_resolver=name_resolver, group_id=group_id,
            include_url=include_url, media_items=media_items,
        )
        label = fwd_text or "（无内容）"
        q_prefix = _format_sender_prefix(q_name, q_id, q_seq)
        return f"{q_prefix}: {label}"

    # Regular quoted message: extract text with placeholders
    q_text, markers = seg.extract_text_with_placeholders(
        q_segments, start_index=counter.counter,
    )

    for marker in markers:
        counter.counter += 1
        rendered = _render_url_placeholder(marker, include_url=include_url)
        q_text = q_text.replace(marker["marker"], rendered, 1)
        if not include_url and media_items is not None and _marker_has_url(marker):
            media_items.append(_marker_to_media_item(marker))

    # Resolve @ mentions before adding sender prefix (avoids matching
    # numbers in the prefix as QQ IDs)
    if q_text and name_resolver:
        q_text = await _resolve_at_mentions(q_text, group_id, name_resolver)

    q_prefix = _format_sender_prefix(q_name, q_id, q_seq)
    return f"{q_prefix}: {q_text}" if q_text else None


# ── Forward expansion (recursive) ────────────────────────────────────────


async def _expand_forward(
    api: Any,
    forward_id: str,
    counter: _MediaCounter,
    depth: int = 0,
    name_resolver: NameResolver | None = None,
    group_id: str = "",
    include_url: bool = True,
    media_items: list[MediaItem] | None = None,
) -> str:
    """Fetch a merged-forward by id and expand it into text.

    This is the API-fetching entry point: it calls ``get_forward_msg`` once
    to obtain the top-level ``messages`` array, then delegates to
    :func:`_expand_messages` for traversal (which reads nested forwards
    from their inline ``data.content`` — NapCat populates this and refuses
    per-id queries for inner forwards with retcode=1200). Depth is guarded
    by ``_expand_messages``; we skip the API call when already over the limit.
    """
    if depth > _MAX_FORWARD_DEPTH:
        return "[合并转发(已跳过:超过最大深度)]"
    try:
        fwd_data = await api.get_forward_msg(forward_id)
    except Exception as exc:
        logger.warning("get_forward_msg failed id=%s: %s", forward_id, exc)
        return "[合并转发(已跳过:读取失败)]"
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
        messages, counter, depth,
        name_resolver=name_resolver, group_id=group_id,
        include_url=include_url, media_items=media_items,
    )


async def _expand_messages(
    messages: list[dict],
    counter: _MediaCounter,
    depth: int,
    name_resolver: NameResolver | None = None,
    group_id: str = "",
    include_url: bool = True,
    media_items: list[MediaItem] | None = None,
) -> str:
    """Expand a list of message objects (each ``{sender, message, ...}``).

    Shared between the top-level ``get_forward_msg`` response and the inline
    ``forward.data.content`` array. Pure traversal: no API calls — nested
    forwards are read from their inline ``content`` field. Begin/end tags
    carry a level number to help the LLM understand nesting structure.
    Media markers are rendered as placeholders; in cache mode
    (``include_url=False``) URLs are omitted from placeholders and the
    corresponding :class:`MediaItem` entries are appended to *media_items*
    for the plugin to download.
    """
    if depth > _MAX_FORWARD_DEPTH:
        return "[合并转发(已跳过:超过最大深度)]"
    parts: list[str] = []
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
            nested_text = await _expand_messages(
                nested_content, counter, depth + 1,
                name_resolver=name_resolver, group_id=group_id,
                include_url=include_url, media_items=media_items,
            )
            if nested_text:
                parts.append(f"{fwd_prefix}: {nested_text}")
            continue

        # Extract text with placeholders and render media URLs
        msg_text, markers = seg.extract_text_with_placeholders(
            msg_segments, start_index=counter.counter,
        )

        for marker in markers:
            counter.counter += 1
            rendered = _render_url_placeholder(marker, include_url=include_url)
            msg_text = msg_text.replace(marker["marker"], rendered, 1)
            if not include_url and media_items is not None and _marker_has_url(marker):
                media_items.append(_marker_to_media_item(marker))

        # Resolve @ mentions in sub-message text
        if name_resolver:
            msg_text = await _resolve_at_mentions(msg_text, group_id, name_resolver)

        if msg_text:
            parts.append(f"{fwd_prefix}: {msg_text}")

    forward_text = "\n".join(parts)
    if forward_text:
        forward_text = f"[合并转发开始:{level}]\n{forward_text}\n[合并转发结束:{level}]"
    return forward_text


# ── Notice event parser ────────────────────────────────────────────────────


async def _parse_notice_event(
    event: dict[str, Any],
    *,
    self_id: str,
    config: AdapterConfig | None = None,
    name_resolver: NameResolver | None = None,
    bot_blacklist_match_fn: Callable[[str, str | None], Any] | None = None,
) -> NormalizedEvent | FilteredEvent | DroppedEvent | None:
    """Parse a OneBot 11 notice event into a synthetic NormalizedEvent.

    Handles three notice types (when enabled via config):
      * ``notify/poke`` — bot 被戳 (target_id == self_id);群聊和私聊都处理。
      * ``group_increase`` — 其他成员进群 (user_id != self_id)。
      * ``group_decrease`` — 其他成员退群 (user_id != self_id, leave/kick 区分措辞)。

    All other notice types are ignored (return None).

    戳一戳走群/DM 用户过滤(黑名单/白名单);成员变动不走用户过滤。
    所有合成事件设置 ``is_system_notice=True``,插件侧据此设 ``internal=True``。

    Returns:
        * :class:`NormalizedEvent` for enabled notice types.
        * ``None`` for disabled/unhandled notice types, or when filtered out.
    """
    notice_type = event.get("notice_type")
    sub_type = event.get("sub_type", "")
    user_id = str(event.get("user_id", ""))
    group_id_raw = event.get("group_id")
    group_id = str(group_id_raw) if group_id_raw is not None else ""
    is_group = bool(group_id)
    timestamp = float(event.get("time", 0) or 0)

    logger.debug(
        "parse_notice: notice_type=%s sub_type=%s user_id=%s group_id=%s target_id=%s",
        notice_type, sub_type, user_id, group_id, event.get("target_id"),
    )

    if config is None:
        return None

    kind = _notice_kind(event, self_id, config, group_id, is_group, user_id)
    if kind is None:
        return None

    if kind == "poke":
        blocked = _filter_poke_notice(
            event, config, group_id, is_group, user_id, timestamp, bot_blacklist_match_fn,
        )
        if blocked is not None:
            return blocked

    # ── Resolve user name ──
    user_name = ""
    if name_resolver is not None and user_id:
        user_name = await name_resolver.resolve(user_id, group_id if is_group else "")

    chat_id, chat_type, chat_name = await _resolve_notice_chat(
        group_id, is_group, user_id, user_name, name_resolver,
    )
    display = user_name or user_id
    text = _notice_text(kind, sub_type, display, user_id)

    # ── Admin check (group only) ──
    is_admin = config.is_admin(user_id, group_id if is_group else None)
    is_global_admin = user_id in config.global_admins

    logger.debug(
        "parse_notice: synthesized kind=%s chat_id=%s user_id=%s text=%r",
        kind, chat_id, user_id, text,
    )

    return NormalizedEvent(
        message_id="",  # notice 事件没有 message_id
        chat_id=chat_id,
        chat_type=chat_type,  # type: ignore[arg-type]
        user_id=user_id,
        user_name=user_name or display,
        text=text,
        timestamp=timestamp,
        is_admin=is_admin,
        is_global_admin=is_global_admin,
        chat_name=chat_name,
        is_system_notice=True,
        rate_limit_eligible=kind == "poke",
    )


def _notice_kind(
    event: dict[str, Any],
    self_id: str,
    config: AdapterConfig,
    group_id: str,
    is_group: bool,
    user_id: str,
) -> str | None:
    notice_type = event.get("notice_type")
    sub_type = event.get("sub_type", "")
    if notice_type == "notify" and sub_type == "poke":
        target_id = str(event.get("target_id", ""))
        if not self_id or target_id != self_id:
            return None
        return "poke" if config.resolve_notify_poke_enabled(group_id if is_group else None) else None
    if notice_type == "group_increase" and is_group:
        if not self_id or user_id == self_id:
            return None
        return "member_join" if config.resolve_notify_member_change_enabled(group_id) else None
    if notice_type == "group_decrease" and is_group:
        if not self_id or user_id == self_id or sub_type not in ("leave", "kick"):
            return None
        return "member_leave" if config.resolve_notify_member_change_enabled(group_id) else None
    return None


def _filter_poke_notice(
    event: dict[str, Any],
    config: AdapterConfig,
    group_id: str,
    is_group: bool,
    user_id: str,
    timestamp: float,
    bot_blacklist_match_fn: Callable[[str, str | None], Any] | None,
) -> DroppedEvent | FilteredEvent | None:
    admitted = config.is_group_user_allowed(group_id, user_id) if is_group else config.is_dm_allowed(user_id)
    if not admitted:
        return _drop_event(
            "user_filter", event, is_group=is_group, sender_id=user_id,
            sender_name="", group_id=group_id,
        )
    is_admin = config.is_admin(user_id, group_id if is_group else None)
    if not config.bot_blacklist_enabled or is_admin or bot_blacklist_match_fn is None:
        return None
    entry = bot_blacklist_match_fn(user_id, group_id if is_group else None)
    if entry is None:
        return None
    return FilteredEvent(
        chat_id=f"group:{group_id}" if is_group else user_id,
        chat_type="group" if is_group else "dm",
        user_id=user_id,
        user_name="",
        command_name="",
        reject_message=_render_bot_blacklist_reject(config, entry, user_id),
        timestamp=timestamp,
        filter_type="bot_blacklist",
    )


async def _resolve_notice_chat(
    group_id: str,
    is_group: bool,
    user_id: str,
    user_name: str,
    name_resolver: NameResolver | None,
) -> tuple[str, str, str]:
    if not is_group:
        return user_id, "dm", user_name or user_id
    group_name = await name_resolver.resolve_group_name(group_id) if name_resolver is not None else ""
    chat_name = f"{group_id}({group_name})" if group_name else group_id
    return f"group:{group_id}", "group", chat_name


def _notice_text(kind: str, sub_type: str, display: str, user_id: str) -> str:
    if kind == "poke":
        action = "戳了戳你"
    elif kind == "member_join":
        action = "加入了群聊"
    elif sub_type == "kick":
        action = "被管理员移出了群聊"
    else:
        action = "退出了群聊"
    return f"[系统] 用户 {display}({user_id}) {action}"

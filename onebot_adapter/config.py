"""Adapter service configuration model and JSON persistence."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from onebot_adapter._async_utils import log_task_exception

logger = logging.getLogger(__name__)


class ConfigLoadError(RuntimeError):
    """Raised when an existing configuration cannot be read safely."""


CONFIG_ENV = "ONEBOT_ADAPTER_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".onebot_adapter" / "config.json"

NAPCAT_MODE_REVERSE = "reverse"
NAPCAT_MODE_FORWARD = "forward"
_VALID_MODES = {NAPCAT_MODE_REVERSE, NAPCAT_MODE_FORWARD}

USER_FILTER_WHITELIST = "whitelist"
USER_FILTER_BLACKLIST = "blacklist"
_VALID_USER_FILTER_MODES = {USER_FILTER_WHITELIST, USER_FILTER_BLACKLIST}

MEDIA_DELIVERY_PASSTHROUGH = "passthrough"
MEDIA_DELIVERY_CACHE = "cache"
_VALID_MEDIA_DELIVERY_MODES = {MEDIA_DELIVERY_PASSTHROUGH, MEDIA_DELIVERY_CACHE}

COMMAND_PERM_EVERYONE = "everyone"
COMMAND_PERM_ADMIN = "admin"
COMMAND_PERM_DISABLED = "disabled"
_VALID_COMMAND_PERM_LEVELS = {COMMAND_PERM_EVERYONE, COMMAND_PERM_ADMIN, COMMAND_PERM_DISABLED}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

RATE_LIMIT_SLIDING_WINDOW = "sliding_window"
RATE_LIMIT_TOKEN_BUCKET = "token_bucket"
_VALID_RATE_LIMIT_ALGORITHMS = {RATE_LIMIT_SLIDING_WINDOW, RATE_LIMIT_TOKEN_BUCKET}

DEFAULT_CHANNEL_PROMPT = (
    "# 平台特性\n"
    "你正通过 OneBot(QQ) 对话。QQ 不渲染 Markdown,仅纯文本(系统会自动剥离 Markdown 语法,但请尽量直接输出纯文本)。\n"
    "回复当前对话通常直接输出文本即可(系统会自动送达);"
    "当你需要主动发送消息(分多条发、推送其他会话、跨会话通知等)时,使用 onebot_send_message 工具。\n"
    "群聊需 @bot 触发。消息上限约 4500 字符,超长会自动分段。\n\n"
    "# chat_id 格式\n"
    "- 私聊: <QQ号>(如 100)\n"
    "- 群聊: group:<群号>(如 group:42)\n\n"
    "# 入站消息格式(你看到的样子)\n"
    "- 群聊消息前缀: [昵称(QQ号)#群内序号]: 内容;管理员标识为 [昵称(QQ号)(管理员)#群内序号]: 内容\n"
    "  #后数字是群内递增序号(real_seq),连续可读,用于发现消息断层;调用 onebot 工具时传此数字\n"
    "  私聊前缀无 # 序号;拿不到 real_seq 时回退显示全局消息 ID(message_id)\n"
    "- @ 段显示为 @QQ号(昵称);未知用户为 @QQ号(未知用户)\n"
    "- 媒体占位符: [图1] [视频1] [语音1] [文件1:report.pdf],编号全局连续\n"
    "- 媒体缓存失败时仍保留对应占位符；文件没有 URL 时可使用 onebot_get_file 工具获取\n"
    "- 引用回复:被引用消息在 reply_to_text 字段(独立于主 text),格式 [昵称(QQ号)#群内序号]: 文本\n"
    "- 合并转发:\n"
    "  [合并转发开始:1]\n"
    "  [Alice]: msg one\n"
    "  [Bob]: msg two\n"
    "  [合并转发结束:1]\n"
    "  嵌套时层级号递增;超过 4 层显示 [合并转发(已跳过:超过最大深度)]\n"
    "  合并转发中仅含昵称,无 QQ 号和群内序号,请勿尝试获取转发中发言者的详细信息\n"
    "- 斜杠命令(/reset 等)不加发送者前缀,原样传递\n"
    "- 启用群号标识时,消息头部会有 [群:42(测试群)] 行(仅主消息,斜杠命令不加)\n\n"
    "# 消息序号与工具调用\n"
    "- 群聊前缀 # 后的数字是群内序号(real_seq),不是全局消息 ID(message_id)\n"
    "- onebot_get_msg / onebot_recall_message / onebot_set_msg_emoji_like 等工具的 real_seq 参数填此群内序号\n"
    "- onebot_get_group_msg_history 的 message_seq 参数例外:填消息 ID(message_id),不是群内序号\n"
    "- 适配器内部维护 real_seq→message_id 映射,自动转换;映射过期时工具返回错误,"
    "需用 onebot_get_group_msg_history 重新获取\n\n"
    "# 出站消息格式(你输出时)\n"
    "- 直接输出文本只能发纯文本,**无法 @ 人**;要 @ 某人必须用 onebot_send_message 工具,"
    "message 参数传 OneBot 11 消息段数组,如 "
    '[{"type":"at","data":{"qq":"123456"}},{"type":"text","data":{"text":" 你好"}}]\n'
    "- 不要用 Markdown 语法(**粗体**、## 标题、- 列表 等),会被自动剥离;"
    "如需结构化展示可用纯文本约定(• 列表、【标题】、「引用」、───── 分隔线)\n"
    "- 回复时无需重复发送者前缀,直接输出正文\n\n"
    "# 不支持的元素\n"
    "- 表情(face/emoji/bface/mface)段在入站时会被丢弃,不要期望看到 QQ 原生表情\n"
    "- 不支持打字状态提示(send_typing 为 no-op)\n\n"
    "# 交互式提问\n"
    "- 不要使用 clarify 工具提问:OneBot 平台无按钮 UI,且群聊共享会话下用户回复会被排队拦截,"
    "导致 agent 卡死直到超时\n"
    "- 需要向用户提问时直接输出纯文本问题即可,用户回复后会在下一轮被正常处理"
)


@dataclass
class GroupConfig:
    """Per-group configuration. Stored in AdapterConfig.groups[group_id]."""
    group_id: str
    name: str = ""  # UI 展示缓存;运行时群名走 name_resolver
    enabled: bool = True
    require_mention: bool | None = None       # None=跟随全局
    mention_first_only: bool | None = None    # None=跟随全局，True=仅首@段触发
    trigger_keywords: list[str] | None = None  # None=跟随全局，[] = 强制禁用关键词
    keyword_first_only: bool | None = None   # None=跟随全局，True=关键词须在开头
    strip_first_mention: bool | None = None  # None=跟随全局，True=移除首@bot段
    custom_prompt: str = ""                   # 空=用全局 global_channel_prompt(WebUI 保存时物化写入 Hermes config.yaml)
    admins: list[str] = field(default_factory=list)
    # ── 群成员准入（黑名单/白名单）──
    group_user_filter_mode: str = USER_FILTER_BLACKLIST  # 默认黑名单
    group_user_list: list[str] = field(default_factory=list)  # 默认空：黑名单空=允许所有人
    message_show_group_id: bool | None = None
    reaction_emoji_enabled: bool | None = None  # None=跟随全局,True=在送达的消息上贴表情回应
    # ── /指令过滤（None=跟随全局）──
    command_filter_enabled: bool | None = None
    command_filter_unknown: bool | None = None
    command_permissions: dict[str, str] | None = None  # None=跟随全局，{} = 强制清空，非空=覆盖
    # ── notice 事件推送（None=跟随全局）──
    notify_poke_enabled: bool | None = None            # 戳一戳(bot 被戳)推送,None=跟随全局
    notify_member_change_enabled: bool | None = None   # 群成员进退群推送,None=跟随全局
    # ── 群聊消息限流（None=跟随全局群聊限流配置）──
    group_rate_limit_algorithm: str | None = None
    group_rate_limit_messages: int | None = None
    group_rate_limit_window_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GroupConfig:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        # group_id is a required field — default to "" if absent in the dict
        # so empty group config dicts ({}) don't raise TypeError.
        filtered.setdefault("group_id", "")
        return cls(**filtered)

    def is_user_allowed(self, user_id: str) -> bool:
        """Whether *user_id* may interact with the bot in this group.

        - ``blacklist`` mode: reject users in the list; empty list = allow all.
        - ``whitelist`` mode: allow only users in the list; empty list = reject all.
        """
        uid = str(user_id)
        if self.group_user_filter_mode == USER_FILTER_WHITELIST:
            return uid in self.group_user_list
        # blacklist (default)
        return uid not in self.group_user_list


@dataclass
class AdapterConfig:
    # ── OneBot 连接 ──
    onebot_mode: str = NAPCAT_MODE_REVERSE
    onebot_reverse_ws_port: int = 18800
    onebot_reverse_ws_path: str = "/onebot"
    onebot_forward_ws_url: str = "ws://127.0.0.1:3001"
    onebot_ws_token: str = ""
    self_id: str = ""

    # ── 全局群聊设置 ──
    group_require_mention: bool = True
    group_mention_first_only: bool = False       # True=仅首@段触发，False=任意位置@
    group_trigger_keywords: list[str] = field(default_factory=list)  # 关键词触发，空=不启用
    group_keyword_first_only: bool = False       # True=关键词须出现在文本开头
    group_strip_first_mention: bool = True       # True=消息以@bot开头时移除该段(非首@bot保留)
    global_admins: list[str] = field(default_factory=list)

    # ── 私聊设置 ──
    dm_user_filter_mode: str = USER_FILTER_WHITELIST  # 默认白名单
    dm_user_list: list[str] = field(default_factory=list)  # 默认空：白名单空=拒绝所有人

    # ── 每群覆盖 ──
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ── 其他 ──
    global_channel_prompt: str = DEFAULT_CHANNEL_PROMPT
    hermes_ws_port: int = 18810
    hermes_ws_path: str = "/hermes"
    hermes_ws_token: str = ""
    hermes_install_dir: str = ""
    webui_port: int = 18820
    webui_token: str = ""  # WebUI 登录鉴权 token,自动生成,请勿清空
    webui_token_lifetime_hours: int = 168  # 登录有效期(小时),最小 1;默认 168(7 天)
    webui_token_epoch: int = 0  # token 纪元,改 lifetime 时 bump 使所有旧 session token 立即失效
    webui_trust_proxy_headers: bool = False  # 信任 X-Forwarded-For(仅反向代理时开启)
    log_level: str = "INFO"
    log_message_preview: int = 100
    log_file_enabled: bool = True
    log_file_dir: str = ""
    log_retention_days: int = 3
    usage_stats_enabled: bool = True
    usage_stats_retention_days: int = 365
    message_show_group_id: bool = True
    seq_map_size: int = 4500
    reaction_emoji_enabled: bool = True
    reaction_emoji_id: str = "124"
    reaction_emoji_id_queued: str = "123"      # 消息排队时贴的表情ID,空=不贴表情
    # ── 发送去重(Gateway send_text 超时重试导致重复发送的兜底)──
    send_dedup_enabled: bool = True
    send_dedup_ttl_seconds: float = 10.0

    # ── 群聊消息排队(仅在 Hermes group_sessions_per_user=false 全群共享 session 时生效)──
    event_queue_enabled: bool = True            # 总开关:Hermes 不隔离群成员时是否排队
    event_queue_max_per_chat: int = 50          # 单群排队上限,超限拒绝入队
    event_queue_idle_timeout: float = 300.0     # 秒,plugin 崩溃/idle 帧丢失时强制清空 busy

    # ── 入站消息限流（限额 0=禁用该维度）──
    rate_limit_enabled: bool = False
    global_rate_limit_algorithm: str = RATE_LIMIT_SLIDING_WINDOW
    global_rate_limit_messages: int = 0
    global_rate_limit_window_seconds: float = 0.0
    group_rate_limit_algorithm: str = RATE_LIMIT_SLIDING_WINDOW
    group_rate_limit_messages: int = 0
    group_rate_limit_window_seconds: float = 0.0
    user_rate_limit_algorithm: str = RATE_LIMIT_SLIDING_WINDOW
    user_rate_limit_messages: int = 0
    user_rate_limit_window_seconds: float = 0.0
    rate_limit_reject_message: str = "⛔ 消息发送过于频繁，请在 {retry_after} 秒后重试"

    # ── 媒体投递 ──
    media_delivery_mode: str = MEDIA_DELIVERY_CACHE  # "passthrough"(URL 占位符) | "cache"(默认,插件侧下载落盘)

    # ── /指令过滤 ──
    command_filter_enabled: bool = False                # 总开关：是否对 /指令 做权限过滤
    command_filter_unknown: bool = False                # 未知指令(不在 hermes 列表)是否过滤，默认放行
    command_permissions: dict[str, str] = field(default_factory=dict)  # {指令名: everyone|admin|disabled}
    command_reject_message: str = "⛔ 你没有权限使用此指令 /{cmd}"

    # ── Bot 动态用户黑名单（独立于准入黑白名单）──
    bot_blacklist_enabled: bool = True
    bot_blacklist_max_duration_seconds: int = 86400
    bot_blacklist_reject_message: str = (
        "⛔ 你已被 bot 暂时拉黑，剩余时间：{remaining}。原因：{reason}"
    )

    # ── notice 事件推送 ──
    notify_poke_enabled: bool = False               # 戳一戳(bot 被戳)推送开关
    notify_member_change_enabled: bool = False      # 群成员进退群推送开关

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.onebot_mode not in _VALID_MODES:
            errors.append(f"onebot_mode must be one of {sorted(_VALID_MODES)}")
        if self.onebot_mode == NAPCAT_MODE_FORWARD and not self.onebot_forward_ws_url:
            errors.append("onebot_forward_ws_url required when onebot_mode=forward")
        if self.log_message_preview < 0:
            errors.append("log_message_preview must be non-negative")
        if self.log_retention_days < 1:
            errors.append("log_retention_days must be at least 1")
        if self.usage_stats_retention_days < 1:
            errors.append("usage_stats_retention_days must be at least 1")
        for port_field in ("onebot_reverse_ws_port", "hermes_ws_port", "webui_port"):
            port_val = getattr(self, port_field)
            if not isinstance(port_val, int) or port_val < 1 or port_val > 65535:
                errors.append(f"{port_field} must be an integer in [1, 65535]")
        if not self.onebot_ws_token:
            errors.append("onebot_ws_token must not be empty")
        if not self.hermes_ws_token:
            errors.append("hermes_ws_token must not be empty")
        if self.seq_map_size <= 0:
            errors.append("seq_map_size must be positive")
        if self.send_dedup_ttl_seconds <= 0:
            errors.append("send_dedup_ttl_seconds must be positive")
        if self.event_queue_max_per_chat < 1:
            errors.append("event_queue_max_per_chat must be at least 1")
        if self.event_queue_idle_timeout <= 0:
            errors.append("event_queue_idle_timeout must be positive")
        if not isinstance(self.rate_limit_enabled, bool):
            errors.append("rate_limit_enabled must be bool")
        for scope in ("global", "group", "user"):
            algorithm = getattr(self, f"{scope}_rate_limit_algorithm")
            messages = getattr(self, f"{scope}_rate_limit_messages")
            window = getattr(self, f"{scope}_rate_limit_window_seconds")
            if algorithm not in _VALID_RATE_LIMIT_ALGORITHMS:
                errors.append(
                    f"{scope}_rate_limit_algorithm must be one of {sorted(_VALID_RATE_LIMIT_ALGORITHMS)}"
                )
            if not isinstance(messages, int) or isinstance(messages, bool) or messages < 0:
                errors.append(f"{scope}_rate_limit_messages must be a non-negative integer")
            if not isinstance(window, (int, float)) or isinstance(window, bool) or window < 0:
                errors.append(f"{scope}_rate_limit_window_seconds must be non-negative")
            elif isinstance(messages, int) and not isinstance(messages, bool) and messages > 0 and window <= 0:
                errors.append(f"{scope}_rate_limit_window_seconds must be positive when the limit is enabled")
        if not isinstance(self.rate_limit_reject_message, str) or not self.rate_limit_reject_message:
            errors.append("rate_limit_reject_message must not be empty")
        if not isinstance(self.bot_blacklist_enabled, bool):
            errors.append("bot_blacklist_enabled must be bool")
        if not isinstance(self.bot_blacklist_max_duration_seconds, int) \
                or isinstance(self.bot_blacklist_max_duration_seconds, bool) \
                or self.bot_blacklist_max_duration_seconds <= 0:
            errors.append("bot_blacklist_max_duration_seconds must be a positive integer")
        if not isinstance(self.bot_blacklist_reject_message, str) or not self.bot_blacklist_reject_message:
            errors.append("bot_blacklist_reject_message must not be empty")
        if self.media_delivery_mode not in _VALID_MEDIA_DELIVERY_MODES:
            errors.append(f"media_delivery_mode must be one of {sorted(_VALID_MEDIA_DELIVERY_MODES)}")
        if self.webui_token_lifetime_hours < 1:
            errors.append("webui_token_lifetime_hours must be at least 1")
        if not self.reaction_emoji_id:
            errors.append("reaction_emoji_id must not be empty")
        if self.dm_user_filter_mode not in _VALID_USER_FILTER_MODES:
            errors.append(f"dm_user_filter_mode must be one of {sorted(_VALID_USER_FILTER_MODES)}")
        if self.log_level.upper() not in _VALID_LOG_LEVELS:
            errors.append(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        for cmd, perm in self.command_permissions.items():
            if perm not in _VALID_COMMAND_PERM_LEVELS:
                errors.append(f"command_permissions[{cmd!r}] must be one of {sorted(_VALID_COMMAND_PERM_LEVELS)}")
        for gid, raw in self.groups.items():
            gc = GroupConfig.from_dict(raw)
            if gc.group_user_filter_mode not in _VALID_USER_FILTER_MODES:
                errors.append(f"group {gid} group_user_filter_mode must be one of {sorted(_VALID_USER_FILTER_MODES)}")
            # Type-check bool/int fields to catch WebUI sending strings
            if not isinstance(gc.enabled, bool):
                errors.append(f"group {gid} enabled must be bool")
            if gc.require_mention is not None and not isinstance(gc.require_mention, bool):
                errors.append(f"group {gid} require_mention must be bool or null")
            if gc.mention_first_only is not None and not isinstance(gc.mention_first_only, bool):
                errors.append(f"group {gid} mention_first_only must be bool or null")
            if gc.keyword_first_only is not None and not isinstance(gc.keyword_first_only, bool):
                errors.append(f"group {gid} keyword_first_only must be bool or null")
            if gc.strip_first_mention is not None and not isinstance(gc.strip_first_mention, bool):
                errors.append(f"group {gid} strip_first_mention must be bool or null")
            if gc.reaction_emoji_enabled is not None and not isinstance(gc.reaction_emoji_enabled, bool):
                errors.append(f"group {gid} reaction_emoji_enabled must be bool or null")
            if gc.command_filter_enabled is not None and not isinstance(gc.command_filter_enabled, bool):
                errors.append(f"group {gid} command_filter_enabled must be bool or null")
            if gc.command_filter_unknown is not None and not isinstance(gc.command_filter_unknown, bool):
                errors.append(f"group {gid} command_filter_unknown must be bool or null")
            if gc.message_show_group_id is not None and not isinstance(gc.message_show_group_id, bool):
                errors.append(f"group {gid} message_show_group_id must be bool or null")
            if gc.notify_poke_enabled is not None and not isinstance(gc.notify_poke_enabled, bool):
                errors.append(f"group {gid} notify_poke_enabled must be bool or null")
            if gc.notify_member_change_enabled is not None and not isinstance(gc.notify_member_change_enabled, bool):
                errors.append(f"group {gid} notify_member_change_enabled must be bool or null")
            if (
                gc.group_rate_limit_algorithm is not None
                and gc.group_rate_limit_algorithm not in _VALID_RATE_LIMIT_ALGORITHMS
            ):
                errors.append(
                    f"group {gid} group_rate_limit_algorithm must be one of "
                    f"{sorted(_VALID_RATE_LIMIT_ALGORITHMS)} or null"
                )
            if (
                gc.group_rate_limit_messages is not None
                and (
                    not isinstance(gc.group_rate_limit_messages, int)
                    or isinstance(gc.group_rate_limit_messages, bool)
                    or gc.group_rate_limit_messages < 0
                )
            ):
                errors.append(f"group {gid} group_rate_limit_messages must be a non-negative integer or null")
            if (
                gc.group_rate_limit_window_seconds is not None
                and (
                    not isinstance(gc.group_rate_limit_window_seconds, (int, float))
                    or isinstance(gc.group_rate_limit_window_seconds, bool)
                    or gc.group_rate_limit_window_seconds < 0
                )
            ):
                errors.append(f"group {gid} group_rate_limit_window_seconds must be non-negative or null")
            resolved_messages = self.resolve_group_rate_limit_messages(gid)
            resolved_window = self.resolve_group_rate_limit_window_seconds(gid)
            if (
                isinstance(resolved_messages, int)
                and not isinstance(resolved_messages, bool)
                and isinstance(resolved_window, (int, float))
                and not isinstance(resolved_window, bool)
                and resolved_messages > 0
                and resolved_window <= 0
            ):
                errors.append(
                    f"group {gid} group_rate_limit_window_seconds must be positive when the limit is enabled"
                )
            if gc.command_permissions is not None:
                for cmd, perm in gc.command_permissions.items():
                    if perm not in _VALID_COMMAND_PERM_LEVELS:
                        errors.append(
                            f"group {gid} command_permissions[{cmd!r}] must be one of "
                            f"{sorted(_VALID_COMMAND_PERM_LEVELS)}"
                        )
        return errors

    def get_group_config(self, group_id: str) -> GroupConfig:
        """Return GroupConfig for a group, or a default if not configured."""
        raw = self.groups.get(str(group_id))
        if raw is not None:
            gc = GroupConfig.from_dict(raw)
            # Ensure group_id is set even if the stored dict omitted it
            if not gc.group_id:
                gc.group_id = str(group_id)
            return gc
        return GroupConfig(group_id=str(group_id))

    def is_group_user_allowed(self, group_id: str, user_id: str) -> bool:
        """Whether *user_id* may interact in *group_id* (per-group filter)."""
        gc = self.get_group_config(group_id)
        return gc.is_user_allowed(user_id)

    def is_dm_allowed(self, user_id: str) -> bool:
        """Whether *user_id* may DM the bot.

        - ``whitelist`` mode (default): allow only users in the list; empty list = reject all.
        - ``blacklist`` mode: reject users in the list; empty list = allow all.
        """
        uid = str(user_id)
        if self.dm_user_filter_mode == USER_FILTER_WHITELIST:
            return uid in self.dm_user_list
        # blacklist
        return uid not in self.dm_user_list

    def is_admin(self, user_id: str, group_id: str | None = None) -> bool:
        uid = str(user_id)
        if uid in self.global_admins:
            return True
        if group_id is not None:
            gc = self.get_group_config(group_id)
            if uid in gc.admins:
                return True
        return False

    def resolve_require_mention(self, group_id: str) -> bool:
        gc = self.get_group_config(group_id)
        if gc.require_mention is not None:
            return gc.require_mention
        return self.group_require_mention

    def resolve_mention_first_only(self, group_id: str) -> bool:
        gc = self.get_group_config(group_id)
        if gc.mention_first_only is not None:
            return gc.mention_first_only
        return self.group_mention_first_only

    def resolve_trigger_keywords(self, group_id: str) -> list[str]:
        gc = self.get_group_config(group_id)
        if gc.trigger_keywords is not None:
            return list(gc.trigger_keywords)
        return list(self.group_trigger_keywords)

    def resolve_keyword_first_only(self, group_id: str) -> bool:
        gc = self.get_group_config(group_id)
        if gc.keyword_first_only is not None:
            return gc.keyword_first_only
        return self.group_keyword_first_only

    def resolve_strip_first_mention(self, group_id: str) -> bool:
        gc = self.get_group_config(group_id)
        if gc.strip_first_mention is not None:
            return gc.strip_first_mention
        return self.group_strip_first_mention

    def resolve_message_show_group_id(self, group_id: str) -> bool:
        gc = self.get_group_config(group_id)
        if gc.message_show_group_id is not None:
            return gc.message_show_group_id
        return self.message_show_group_id

    def resolve_reaction_emoji_enabled(self, group_id: str | None = None) -> bool:
        """消息送达贴表情开关。群配置非 None 时覆盖全局。私聊 (group_id=None) 用全局。"""
        if group_id is not None:
            gc = self.get_group_config(group_id)
            if gc.reaction_emoji_enabled is not None:
                return gc.reaction_emoji_enabled
        return self.reaction_emoji_enabled

    def resolve_group_rate_limit_algorithm(self, group_id: str) -> str:
        gc = self.get_group_config(group_id)
        return gc.group_rate_limit_algorithm or self.group_rate_limit_algorithm

    def resolve_group_rate_limit_messages(self, group_id: str) -> int:
        gc = self.get_group_config(group_id)
        if gc.group_rate_limit_messages is not None:
            return gc.group_rate_limit_messages
        return self.group_rate_limit_messages

    def resolve_group_rate_limit_window_seconds(self, group_id: str) -> float:
        gc = self.get_group_config(group_id)
        if gc.group_rate_limit_window_seconds is not None:
            return gc.group_rate_limit_window_seconds
        return self.group_rate_limit_window_seconds

    # ── notice 事件推送解析 ──

    def resolve_notify_poke_enabled(self, group_id: str | None = None) -> bool:
        """戳一戳(bot 被戳)推送开关。群配置非 None 时覆盖全局。私聊 (group_id=None) 用全局。"""
        if group_id is not None:
            gc = self.get_group_config(group_id)
            if gc.notify_poke_enabled is not None:
                return gc.notify_poke_enabled
        return self.notify_poke_enabled

    def resolve_notify_member_change_enabled(self, group_id: str | None = None) -> bool:
        """群成员进退群推送开关。群配置非 None 时覆盖全局。"""
        if group_id is not None:
            gc = self.get_group_config(group_id)
            if gc.notify_member_change_enabled is not None:
                return gc.notify_member_change_enabled
        return self.notify_member_change_enabled

    # ── /指令过滤解析 ──

    def resolve_command_filter_enabled(self, group_id: str | None = None) -> bool:
        """指令过滤总开关。群配置非 None 时覆盖全局。私聊 (group_id=None) 用全局。"""
        if group_id is not None:
            gc = self.get_group_config(group_id)
            if gc.command_filter_enabled is not None:
                return gc.command_filter_enabled
        return self.command_filter_enabled

    def resolve_command_filter_unknown(self, group_id: str | None = None) -> bool:
        """未知指令处理：True=过滤，False=放行(默认)。群配置非 None 时覆盖全局。"""
        if group_id is not None:
            gc = self.get_group_config(group_id)
            if gc.command_filter_unknown is not None:
                return gc.command_filter_unknown
        return self.command_filter_unknown

    def resolve_command_permission(
        self, group_id: str | None, command_name: str,
    ) -> str | None:
        """解析单个指令的权限级别。

        返回 ``everyone`` / ``admin`` / ``disabled`` 或 ``None``(未配置)。
        群级 ``command_permissions`` 优先；群配置为 ``{}``(空 dict)表示强制
        清空所有指令配置(均视为未配置)；群配置非空 dict 时按 key 覆盖，其余
        指令回落到全局 ``command_permissions``。
        """
        if group_id is not None:
            gc = self.get_group_config(group_id)
            if gc.command_permissions is not None:
                # 群级显式配置：直接查；未在群配置中的指令视为 None(不回落全局)
                return gc.command_permissions.get(command_name)
        return self.command_permissions.get(command_name)

    def check_command_permission(
        self,
        group_id: str | None,
        user_id: str,
        command_name: str,
        is_known: bool,
    ) -> tuple[bool, str | None]:
        """检查用户是否有权限执行某指令。

        返回 ``(allowed, reject_message)``。``allowed=True`` 表示放行；
        ``allowed=False`` 时 ``reject_message`` 为拒绝原因(可用于回复用户)。

        参数:
            group_id: 群号；私聊传 None
            user_id: 发送者 QQ 号
            command_name: 规范化后的指令名(小写、不含 "/")
            is_known: 该指令是否在 hermes 已注册指令列表中
        """
        if not self.resolve_command_filter_enabled(group_id):
            return True, None  # 总开关关闭，不过滤

        # 未知指令处理
        if not is_known:
            if self.resolve_command_filter_unknown(group_id):
                msg = self.command_reject_message.replace("{cmd}", command_name)
                return False, msg
            return True, None  # 未知指令默认放行

        perm = self.resolve_command_permission(group_id, command_name)
        if perm == COMMAND_PERM_DISABLED:
            msg = self.command_reject_message.replace("{cmd}", command_name)
            return False, msg
        if perm == COMMAND_PERM_ADMIN:
            if not self.is_admin(user_id, group_id or None):
                msg = self.command_reject_message.replace("{cmd}", command_name)
                return False, msg
        # everyone 或 None(未配置) → 放行
        return True, None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdapterConfig:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def with_overrides(self, **changes: Any) -> AdapterConfig:
        return replace(self, **changes)


def config_path() -> Path:
    explicit = os.getenv(CONFIG_ENV)
    if explicit:
        return Path(explicit)
    return DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> AdapterConfig:
    target = path or config_path()
    if not target.exists():
        logger.warning("config file not found at %s, using defaults", target)
        return AdapterConfig()
    stat = None
    try:
        stat = target.stat()
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ConfigLoadError("configuration root must be a JSON object")
        cfg = AdapterConfig.from_dict(data)
        logger.info("config loaded from %s (mtime=%s, size=%d)", target, stat.st_mtime, stat.st_size)
        return cfg
    except ConfigLoadError:
        raise
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        message = (
            f"config load failed at {target} "
            f"(mtime={getattr(stat, 'st_mtime', '?')}, size={getattr(stat, 'st_size', '?')}): {exc}"
        )
        logger.error(message)
        raise ConfigLoadError(message) from exc


def _inject_comments(d: dict[str, Any]) -> dict[str, Any]:
    """Insert ``_comment_*`` annotation fields before selected keys.

    ``from_dict`` ignores unknown keys, so these comments round-trip safely
    through save/load without polluting the dataclass.
    """
    # Map field name -> comment text for annotated fields.
    comments: dict[str, str] = {
        "onebot_mode": "可选值: reverse(被动,默认) | forward(主动连接NapCat)",
        "onebot_ws_token": "OneBot↔适配器 WS 鉴权 token,自动生成,请勿清空",
        "hermes_ws_token": "适配器↔Hermes插件 WS 鉴权 token,自动生成,请勿清空",
        "webui_token": "WebUI 登录鉴权 token,自动生成,请勿清空",
        "webui_token_lifetime_hours": "WebUI 登录有效期(小时),最小 1,默认 168(7天);改后已登录会话立即失效",
        "webui_token_epoch": "token 纪元(内部状态,勿手动修改);改 lifetime 时自动递增使旧 session token 失效",
        "webui_trust_proxy_headers": "信任 X-Forwarded-For 获取客户端 IP(仅反向代理时开启;"
                                     "直连开启会被伪造 IP 绕过登录限流)",
        "dm_user_filter_mode": "可选值: whitelist(白名单,默认) | blacklist(黑名单)",
        "log_level": "可选值: DEBUG | INFO(默认) | WARNING | ERROR",
        "usage_stats_enabled": "用量统计开关;关闭后停止新增记录,已有历史仍可查询",
        "usage_stats_retention_days": "用量统计保留天数(默认365),过期记录自动清理",
        "groups": "群组配置,key为群号字符串,value为群配置对象;子字段require_mention等为null时跟随全局",
        "reaction_emoji_enabled": "消息送达 Hermes 后在原消息贴表情回应;群配置可单独覆盖",
        "reaction_emoji_id": "贴表情回应使用的表情ID(默认 124),QQ 表情编号",
        "event_queue_enabled": "群聊排队总开关:Hermes 不隔离群成员(group_sessions_per_user=false)时,"
                              "是否对群消息排队串行处理",
        "event_queue_max_per_chat": "群聊排队:单群排队消息上限(默认50),超限拒绝入队",
        "event_queue_idle_timeout": "群聊排队:plugin 无 idle 信号超时(秒,默认300),超时强制清空 busy 状态",
        "rate_limit_enabled": "入站消息限流总开关;全局/群聊/个人三个维度同时检查,管理员豁免",
        "global_rate_limit_algorithm": "全局限流算法:sliding_window(滑动窗口)|token_bucket(令牌桶)",
        "global_rate_limit_messages": "全局限流消息数;0=禁用该维度",
        "global_rate_limit_window_seconds": "全局限流窗口秒数",
        "group_rate_limit_algorithm": "群聊限流算法;群配置可单独覆盖",
        "group_rate_limit_messages": "每个群聊的限流消息数;0=禁用该维度;群配置可覆盖",
        "group_rate_limit_window_seconds": "群聊限流窗口秒数;群配置可覆盖",
        "user_rate_limit_algorithm": "个人限流算法;同一QQ在私聊和所有群共享计数",
        "user_rate_limit_messages": "每个QQ的限流消息数;0=禁用该维度",
        "user_rate_limit_window_seconds": "个人限流窗口秒数",
        "rate_limit_reject_message": "限流提示模板;支持 {scope}/{retry_after}/{user_id}",
        "reaction_emoji_id_queued": "消息排队时贴表情回应使用的表情ID(默认 123),空=不贴表情",
        "media_delivery_mode": "可选值: passthrough(URL 占位符直传) | cache(插件侧下载落盘到 ~/.hermes/cache/,默认)",
        "global_channel_prompt": "全局提示词;保存时物化写入 Hermes config.yaml 的"
                                 " platforms.onebot.channel_prompts,需重启 Hermes 网关生效",
        "notify_poke_enabled": "戳一戳(bot 被戳)推送开关;开启后 bot 被戳会合成系统事件转发给 agent",
        "notify_member_change_enabled": "群成员进退群推送开关;开启后其他成员进群/退群会合成系统事件转发给 agent",
        "bot_blacklist_enabled": "允许 bot 使用独立动态黑名单工具并在消息触发时拦截命中用户",
        "bot_blacklist_max_duration_seconds": "bot 单次动态拉黑允许的最大秒数,默认86400(24小时)",
        "bot_blacklist_reject_message": "动态拉黑提示模板;支持 {user_id}/{scope}/{remaining}/{expires_at}/{reason}",
    }
    result: dict[str, Any] = {}
    for key, value in d.items():
        cmt = comments.get(key)
        if cmt:
            result[f"_comment_{key}"] = cmt
        result[key] = value
    return result


_MAX_BACKUPS = 5
_AUDIT_RETENTION_DAYS = 365
_AUDIT_LOCK = threading.Lock()


def _config_fingerprint(data: dict[str, Any] | None) -> str | None:
    if data is None:
        return None
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_existing_config_data(target: Path) -> dict[str, Any] | None:
    """Read the pre-save JSON for audit comparison without applying defaults."""
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    return {k: v for k, v in raw.items() if not k.startswith("_comment_")}


def _audit_diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    old_data = old or {}
    changed_fields = sorted(k for k in old_data.keys() | new.keys() if old_data.get(k) != new.get(k))
    defaults = AdapterConfig().to_dict()
    reverted_to_default = sorted(
        k for k in changed_fields
        if k in defaults and old is not None and old_data.get(k) != defaults[k] and new.get(k) == defaults[k]
    )
    old_groups = old_data.get("groups")
    new_groups = new.get("groups")
    old_group_count = len(old_groups) if isinstance(old_groups, dict) else 0
    new_group_count = len(new_groups) if isinstance(new_groups, dict) else 0
    suspicious_reset = (
        len(reverted_to_default) >= 5
        or (old_group_count > 0 and new_group_count == 0)
    )
    return {
        "changed_fields": changed_fields,
        "reverted_to_default_fields": reverted_to_default,
        "old_group_count": old_group_count,
        "new_group_count": new_group_count,
        "suspicious_reset": suspicious_reset,
    }


def _safe_audit_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    """Allow only bounded request metadata; configuration values never enter here."""
    if not metadata:
        return {}
    allowed = {"client_ip", "http_method", "http_path", "group_id"}
    result: dict[str, str] = {}
    for key in allowed:
        if key in metadata:
            result[key] = str(metadata[key]).replace("\r", " ").replace("\n", " ")[:300]
    return result


def _rotate_config_audit(audit_path: Path, now: float) -> None:
    if audit_path.exists():
        modified = datetime.fromtimestamp(audit_path.stat().st_mtime)
        current = datetime.fromtimestamp(now)
        if modified.date() != current.date():
            rotated = audit_path.with_name(f"{audit_path.name}.{modified:%Y-%m-%d}")
            if rotated.exists():
                rotated = audit_path.with_name(f"{audit_path.name}.{modified:%Y-%m-%d}.{int(now)}")
            os.replace(audit_path, rotated)
    cutoff = now - (_AUDIT_RETENTION_DAYS * 86400)
    for old in audit_path.parent.glob(f"{audit_path.name}.*"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError as exc:
            logger.warning("could not remove old config audit log %s: %s", old, exc)


def _write_config_audit(target: Path, event: dict[str, Any]) -> None:
    """Append one JSON audit record independently of normal logging settings."""
    audit_dir = target.parent / "logs"
    audit_path = audit_dir / "config-audit.log"
    with _AUDIT_LOCK:
        audit_dir.mkdir(parents=True, exist_ok=True)
        _rotate_config_audit(audit_path, time.time())
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            handle.flush()


def _try_write_config_audit(target: Path, event: dict[str, Any]) -> None:
    try:
        _write_config_audit(target, event)
    except Exception:
        # Audit failure must never turn a successful atomic config write into a
        # failed API operation, but it must remain visible in ordinary logs.
        logger.exception("config audit write failed for %s", target)


def _rotate_backups(target: Path, max_backups: int = _MAX_BACKUPS) -> None:
    """轮转 ``{target.name}.bak.{timestamp}`` 备份,保留最近 *max_backups* 个。

    备份按文件名中的时间戳排序(单调递增),超出上限的最旧备份被删除。
    """
    parent = target.parent
    pattern = f"{target.name}.bak.*"
    backups = sorted(parent.glob(pattern))
    for old in backups[:-max_backups] if max_backups > 0 else backups:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("could not remove old backup %s: %s", old, exc)


def save_config(
    cfg: AdapterConfig,
    path: Path | None = None,
    *,
    source: str = "internal",
    reason: str = "unspecified",
    actor: str = "system",
    metadata: dict[str, Any] | None = None,
    submitted_fields: list[str] | None = None,
) -> None:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    old_data = _read_existing_config_data(target)
    new_data = cfg.to_dict()
    diff = _audit_diff(old_data, new_data)
    event: dict[str, Any] = {
        "audit_id": uuid.uuid4().hex,
        "timestamp": datetime.now().astimezone().isoformat(),
        "pid": os.getpid(),
        "source": source,
        "reason": reason,
        "actor": actor,
        "config_path": str(target),
        "outcome": "attempt",
        "old_fingerprint": _config_fingerprint(old_data),
        "new_fingerprint": _config_fingerprint(new_data),
        "submitted_fields": sorted(set(submitted_fields or [])),
        **diff,
        **_safe_audit_metadata(metadata),
    }
    backup_path: Path | None = None
    # 备份现有文件(若存在),避免静默覆盖丢失用户配置
    if target.exists():
        candidate_backup = target.with_name(f"{target.name}.bak.{int(time.time())}")
        try:
            shutil.copy2(target, candidate_backup)
            backup_path = candidate_backup
            logger.info("config backed up to %s", candidate_backup)
            _rotate_backups(target)
        except OSError as exc:
            logger.warning("config backup failed (will still save): %s", exc)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        data = _inject_comments(new_data)
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except Exception as exc:
        event["outcome"] = "failure"
        event["error_type"] = type(exc).__name__
        if backup_path is not None:
            event["backup_file"] = backup_path.name
        _try_write_config_audit(target, event)
        logger.error(
            "config save failed: path=%s reason=%s source=%s changed=%s error_type=%s audit_id=%s",
            target,
            reason,
            source,
            diff["changed_fields"],
            type(exc).__name__,
            event["audit_id"],
        )
        raise
    event["outcome"] = "success"
    if backup_path is not None:
        event["backup_file"] = backup_path.name
    _try_write_config_audit(target, event)
    if diff["suspicious_reset"]:
        logger.warning(
            "suspicious config reset detected: reason=%s source=%s changed=%s reverted_to_default=%s "
            "groups=%d->%d audit_id=%s",
            reason,
            source,
            diff["changed_fields"],
            diff["reverted_to_default_fields"],
            diff["old_group_count"],
            diff["new_group_count"],
            event["audit_id"],
        )
    logger.info(
        "config saved to %s (size=%d, reason=%s, source=%s, changed=%s, audit_id=%s)",
        target,
        target.stat().st_size,
        reason,
        source,
        diff["changed_fields"],
        event["audit_id"],
    )


def ensure_tokens(
    cfg: AdapterConfig,
    path: Path | None = None,
    *,
    source: str = "startup",
    reason: str = "startup.token_generation",
    actor: str = "system",
) -> AdapterConfig:
    """Generate and persist tokens for any that are empty.

    Returns *cfg* unchanged when both tokens are already set.  When one or both
    are empty, new random tokens are generated, saved to *path* (default config
    location), and the updated config is returned.
    """
    changes: dict[str, Any] = {}
    if not cfg.onebot_ws_token:
        changes["onebot_ws_token"] = secrets.token_urlsafe(24)
    if not cfg.hermes_ws_token:
        changes["hermes_ws_token"] = secrets.token_urlsafe(24)
    if not cfg.webui_token:
        changes["webui_token"] = secrets.token_urlsafe(24)
    if changes:
        logger.warning(
            "tokens empty (%s), generating new ones; this overwrites %s",
            list(changes.keys()),
            path or config_path(),
        )
        cfg = cfg.with_overrides(**changes)
        save_config(
            cfg,
            path,
            source=source,
            reason=reason,
            actor=actor,
            submitted_fields=sorted(changes),
        )
    return cfg


class ConfigStore:
    """Thread-safe config holder with change notification for hot-reload."""

    def __init__(self, cfg: AdapterConfig | None = None) -> None:
        self._cfg = cfg or AdapterConfig()
        self._lock = threading.Lock()
        self._listeners: list = []
        self._listener_tasks: set[asyncio.Task] = set()

    @property
    def config(self) -> AdapterConfig:
        with self._lock:
            return self._cfg

    def update(self, new_cfg: AdapterConfig) -> None:
        with self._lock:
            old = self._cfg
            self._cfg = new_cfg
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                result = cb(old, new_cfg)
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_running_loop()
                        task = loop.create_task(result)
                        self._listener_tasks.add(task)
                        task.add_done_callback(self._listener_tasks.discard)
                        task.add_done_callback(log_task_exception)
                    except RuntimeError:
                        result.close()
            except Exception:
                logger.exception("config change listener failed")

    def patch(self, **changes: Any) -> AdapterConfig:
        new_cfg = self.config.with_overrides(**changes)
        errors = new_cfg.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self.update(new_cfg)
        return new_cfg

    def on_change(self, cb) -> None:
        with self._lock:
            self._listeners.append(cb)

    async def close_notifications(self) -> None:
        """Cancel and await outstanding asynchronous change listeners."""
        for task in list(self._listener_tasks):
            task.cancel()
        if self._listener_tasks:
            await asyncio.gather(*self._listener_tasks, return_exceptions=True)
        self._listener_tasks.clear()

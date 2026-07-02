"""Adapter service configuration model and JSON persistence."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_ENV = "ONEBOT_ADAPTER_CONFIG"
DEFAULT_CONFIG_DIR = Path.home() / ".onebot_adapter"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"

NAPCAT_MODE_REVERSE = "reverse"
NAPCAT_MODE_FORWARD = "forward"
_VALID_MODES = {NAPCAT_MODE_REVERSE, NAPCAT_MODE_FORWARD}

SESSION_SHARED = "shared"
SESSION_PER_USER = "per_user"
_VALID_SESSION_MODES = {SESSION_SHARED, SESSION_PER_USER}

USER_FILTER_WHITELIST = "whitelist"
USER_FILTER_BLACKLIST = "blacklist"
_VALID_USER_FILTER_MODES = {USER_FILTER_WHITELIST, USER_FILTER_BLACKLIST}

COMMAND_PERM_EVERYONE = "everyone"
COMMAND_PERM_ADMIN = "admin"
COMMAND_PERM_DISABLED = "disabled"
_VALID_COMMAND_PERM_LEVELS = {COMMAND_PERM_EVERYONE, COMMAND_PERM_ADMIN, COMMAND_PERM_DISABLED}

DEFAULT_PLATFORM_HINT = (
    "# 平台特性\n"
    "你正通过 OneBot(QQ) 对话。QQ 不渲染 Markdown,仅纯文本(系统会自动剥离 Markdown 语法,但请尽量直接输出纯文本)。\n"
    "回复当前对话通常直接输出文本即可(系统会自动送达);"
    "当你需要主动发送消息(分多条发、推送其他会话、跨会话通知等)时,使用 onebot_send_message 工具。\n"
    "群聊需 @bot 触发。消息上限约 4500 字符,超长会自动分段。\n\n"
    "# chat_id 格式\n"
    "- 私聊: <QQ号>(如 100)\n"
    "- 群聊(默认 shared 模式): group:<群号>(如 group:42)\n"
    "- 群聊 per_user 会话模式: group:<群号>:user:<QQ号>(如 group:42:user:100)\n\n"
    "# 入站消息格式(你看到的样子)\n"
    "- 群聊消息前缀: [昵称(QQ号)#群内序号]: 内容;管理员标识为 [昵称(QQ号)(管理员)#群内序号]: 内容\n"
    "  #后数字是群内递增序号(real_seq),连续可读,用于发现消息断层;调用 onebot 工具时传此数字\n"
    "  私聊前缀无 # 序号;拿不到 real_seq 时回退显示全局消息 ID(message_id)\n"
    "- @ 段显示为 @QQ号(昵称);未知用户为 @QQ号(未知用户)\n"
    "- 媒体占位符: [图1] [视频1] [语音1] [文件1:report.pdf],编号全局连续\n"
    "- 媒体跳过/失败: [图1](已跳过:超出数量限制:已下载10个达到上限10) 或 [语音1](语音转换失败,保留原始格式)\n"
    "- 引用回复:被引用消息在 reply_to_text 字段(独立于主 text),格式 [昵称(QQ号)#群内序号]: 文本\n"
    "- 合并转发:\n"
    "  [合并转发开始:1]\n"
    "  [Alice(1)#123]: msg one\n"
    "  [Bob(2)#124]: msg two\n"
    "  [合并转发结束:1]\n"
    "  嵌套时层级号递增;超过 4 层显示 [合并转发(已跳过:超过最大深度)]\n"
    "- 斜杠命令(/reset 等)不加发送者前缀,原样传递\n"
    "- 启用群号标识时,消息头部会有 [群:42(测试群)] 行(仅主消息,斜杠命令不加)\n\n"
    "# 消息序号与工具调用\n"
    "- 群聊前缀 # 后的数字是群内序号(real_seq),不是全局消息 ID(message_id)\n"
    "- onebot_get_msg / onebot_recall_message / onebot_set_msg_emoji_like 等工具的 real_seq 参数填此群内序号\n"
    "- onebot_get_group_msg_history 的 message_seq 参数例外:填消息 ID(message_id),不是群内序号\n"
    "- 适配器内部维护 real_seq→message_id 映射,自动转换;映射过期时工具返回错误,"
    "需用 onebot_get_group_msg_history 重新获取\n\n"
    "# 出站消息格式(你输出时)\n"
    "- 要 @ 某人,使用 {@QQ号} 格式,如 {@123456} 你好(QQ 号 5-11 位数字,大括号包裹)\n"
    "- 不要用 Markdown 语法(**粗体**、## 标题、- 列表 等),会被自动剥离;"
    "如需结构化展示可用纯文本约定(• 列表、【标题】、「引用」、───── 分隔线)\n"
    "- 回复时无需重复发送者前缀,直接输出正文\n\n"
    "# 不支持的元素\n"
    "- 表情(face/emoji/bface/mface)段在入站时会被丢弃,不要期望看到 QQ 原生表情\n"
    "- 不支持打字状态提示(send_typing 为 no-op)"
)


@dataclass
class GroupConfig:
    """Per-group configuration. Stored in AdapterConfig.groups[group_id]."""
    group_id: str
    name: str = ""
    enabled: bool = True
    require_mention: bool | None = None       # None=跟随全局
    mention_first_only: bool | None = None    # None=跟随全局，True=仅首@段触发
    trigger_keywords: list[str] | None = None  # None=跟随全局，[] = 强制禁用关键词
    keyword_first_only: bool | None = None   # None=跟随全局，True=关键词须在开头
    keep_mention: bool | None = None         # None=跟随全局，True=保留@bot段
    session_mode: str = "default"             # "default" | "shared" | "per_user"
    custom_prompt: str = ""                   # 空=用全局 platform_hint
    admins: list[str] = field(default_factory=list)
    # ── 群成员准入（黑名单/白名单）──
    group_user_filter_mode: str = USER_FILTER_BLACKLIST  # 默认黑名单
    group_user_list: list[str] = field(default_factory=list)  # 默认空：黑名单空=允许所有人
    welcome_enabled: bool = False
    welcome_message: str = ""
    media_max_mb: int | None = None           # None=跟随全局
    media_max_count: int | None = None       # None=跟随全局
    media_limit_reject_enabled: bool | None = None  # None=跟随全局,True=超出限制时回发提示
    auto_join: bool = False
    message_show_group_id: bool | None = None
    reaction_emoji_enabled: bool | None = None  # None=跟随全局,True=在送达的消息上贴表情回应
    # ── /指令过滤（None=跟随全局）──
    command_filter_enabled: bool | None = None
    command_filter_unknown: bool | None = None
    command_permissions: dict[str, str] | None = None  # None=跟随全局，{} = 强制清空，非空=覆盖

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GroupConfig:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
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
    group_keep_mention: bool = False              # True=触发后保留@bot段（不 strip）
    group_session_mode: str = SESSION_SHARED
    global_admins: list[str] = field(default_factory=list)
    group_auto_join: bool = False

    # ── 私聊设置 ──
    dm_user_filter_mode: str = USER_FILTER_WHITELIST  # 默认白名单
    dm_user_list: list[str] = field(default_factory=list)  # 默认空：白名单空=拒绝所有人

    # ── 每群覆盖 ──
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ── 其他 ──
    media_max_mb: int = 5
    media_max_count: int = 10
    media_limit_reject_enabled: bool = True  # 媒体超出数量/大小限制或下载失败时,回发一条融合提示
    media_limit_reject_message: str = (
        "⚠️ 本条消息有 {skipped_count} 个媒体未处理:\n{details}"
    )
    platform_hint: str = DEFAULT_PLATFORM_HINT
    hermes_ws_port: int = 18810
    hermes_ws_path: str = "/hermes"
    hermes_ws_token: str = ""
    hermes_install_dir: str = ""
    webui_port: int = 18820
    webui_token: str = ""  # WebUI 登录鉴权 token,自动生成,请勿清空
    webui_token_lifetime_hours: int = 168  # 登录有效期(小时),最小 1;默认 168(7 天)
    webui_token_epoch: int = 0  # token 纪元,改 lifetime 时 bump 使所有旧 session token 立即失效
    log_level: str = "INFO"
    log_message_preview: int = 100
    log_file_enabled: bool = True
    log_file_dir: str = ""
    log_retention_days: int = 3
    message_show_group_id: bool = False
    seq_map_size: int = 4500
    reaction_emoji_enabled: bool = False
    reaction_emoji_id: str = "76"
    # ── 发送去重(Gateway send_text 超时重试导致重复发送的兜底)──
    send_dedup_enabled: bool = True
    send_dedup_ttl_seconds: float = 10.0

    # ── /指令过滤 ──
    command_filter_enabled: bool = False                # 总开关：是否对 /指令 做权限过滤
    command_filter_unknown: bool = False                # 未知指令(不在 hermes 列表)是否过滤，默认放行
    command_permissions: dict[str, str] = field(default_factory=dict)  # {指令名: everyone|admin|disabled}
    command_reject_message: str = "⛔ 你没有权限使用此指令 /{cmd}"

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.onebot_mode not in _VALID_MODES:
            errors.append(f"onebot_mode must be one of {sorted(_VALID_MODES)}")
        if self.onebot_mode == NAPCAT_MODE_FORWARD and not self.onebot_forward_ws_url:
            errors.append("onebot_forward_ws_url required when onebot_mode=forward")
        if self.media_max_mb <= 0:
            errors.append("media_max_mb must be positive")
        if self.media_max_count <= 0:
            errors.append("media_max_count must be positive")
        if not isinstance(self.media_limit_reject_enabled, bool):
            errors.append("media_limit_reject_enabled must be bool")
        if not isinstance(self.media_limit_reject_message, str) or not self.media_limit_reject_message.strip():
            errors.append("media_limit_reject_message must be a non-empty string")
        if self.group_session_mode not in _VALID_SESSION_MODES:
            errors.append(f"group_session_mode must be one of {sorted(_VALID_SESSION_MODES)}")
        if self.log_message_preview < 0:
            errors.append("log_message_preview must be non-negative")
        if self.log_retention_days < 1:
            errors.append("log_retention_days must be at least 1")
        if not self.onebot_ws_token:
            errors.append("onebot_ws_token must not be empty")
        if not self.hermes_ws_token:
            errors.append("hermes_ws_token must not be empty")
        if self.seq_map_size <= 0:
            errors.append("seq_map_size must be positive")
        if self.send_dedup_ttl_seconds <= 0:
            errors.append("send_dedup_ttl_seconds must be positive")
        if self.webui_token_lifetime_hours < 1:
            errors.append("webui_token_lifetime_hours must be at least 1")
        if not self.reaction_emoji_id:
            errors.append("reaction_emoji_id must not be empty")
        if self.dm_user_filter_mode not in _VALID_USER_FILTER_MODES:
            errors.append(f"dm_user_filter_mode must be one of {sorted(_VALID_USER_FILTER_MODES)}")
        _VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
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
            if gc.keep_mention is not None and not isinstance(gc.keep_mention, bool):
                errors.append(f"group {gid} keep_mention must be bool or null")
            if gc.welcome_enabled is not None and not isinstance(gc.welcome_enabled, bool):
                errors.append(f"group {gid} welcome_enabled must be bool")
            if gc.auto_join is not None and not isinstance(gc.auto_join, bool):
                errors.append(f"group {gid} auto_join must be bool")
            if gc.reaction_emoji_enabled is not None and not isinstance(gc.reaction_emoji_enabled, bool):
                errors.append(f"group {gid} reaction_emoji_enabled must be bool or null")
            if gc.media_max_mb is not None and (not isinstance(gc.media_max_mb, int) or gc.media_max_mb <= 0):
                errors.append(f"group {gid} media_max_mb must be a positive int or null")
            if gc.media_max_count is not None and (not isinstance(gc.media_max_count, int) or gc.media_max_count <= 0):
                errors.append(f"group {gid} media_max_count must be a positive int or null")
            if gc.media_limit_reject_enabled is not None and not isinstance(gc.media_limit_reject_enabled, bool):
                errors.append(f"group {gid} media_limit_reject_enabled must be bool or null")
            if gc.session_mode not in _VALID_SESSION_MODES and gc.session_mode != "default":
                errors.append(f"group {gid} session_mode must be one of {sorted(_VALID_SESSION_MODES | {'default'})}")
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
        if raw:
            return GroupConfig.from_dict(raw)
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
        if group_id:
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

    def resolve_keep_mention(self, group_id: str) -> bool:
        gc = self.get_group_config(group_id)
        if gc.keep_mention is not None:
            return gc.keep_mention
        return self.group_keep_mention

    def resolve_session_mode(self, group_id: str) -> str:
        gc = self.get_group_config(group_id)
        if gc.session_mode == "default":
            return self.group_session_mode
        return gc.session_mode

    def resolve_custom_prompt(self, group_id: str) -> str | None:
        gc = self.get_group_config(group_id)
        return gc.custom_prompt if gc.custom_prompt else None

    def resolve_media_max_bytes(self, group_id: str | None = None) -> int:
        if group_id:
            gc = self.get_group_config(group_id)
            if gc.media_max_mb is not None:
                return gc.media_max_mb * 1024 * 1024
        return self.media_max_mb * 1024 * 1024

    def resolve_media_max_count(self, group_id: str | None = None) -> int:
        if group_id:
            gc = self.get_group_config(group_id)
            if gc.media_max_count is not None:
                return gc.media_max_count
        return self.media_max_count

    def resolve_media_limit_reject_enabled(self, group_id: str | None = None) -> bool:
        """媒体超出限制回发提示开关。群配置非 None 时覆盖全局。私聊 (group_id=None) 用全局。"""
        if group_id:
            gc = self.get_group_config(group_id)
            if gc.media_limit_reject_enabled is not None:
                return gc.media_limit_reject_enabled
        return self.media_limit_reject_enabled

    def resolve_media_limit_reject_message(self, group_id: str | None = None) -> str:
        """媒体超出限制回发提示文案模板。无 per-group 覆盖字段(文案仅全局可配)。"""
        return self.media_limit_reject_message

    def resolve_message_show_group_id(self, group_id: str) -> bool:
        gc = self.get_group_config(group_id)
        if gc.message_show_group_id is not None:
            return gc.message_show_group_id
        return self.message_show_group_id

    def resolve_reaction_emoji_enabled(self, group_id: str | None = None) -> bool:
        """消息送达贴表情开关。群配置非 None 时覆盖全局。私聊 (group_id=None) 用全局。"""
        if group_id:
            gc = self.get_group_config(group_id)
            if gc.reaction_emoji_enabled is not None:
                return gc.reaction_emoji_enabled
        return self.reaction_emoji_enabled

    # ── /指令过滤解析 ──

    def resolve_command_filter_enabled(self, group_id: str | None = None) -> bool:
        """指令过滤总开关。群配置非 None 时覆盖全局。私聊 (group_id=None) 用全局。"""
        if group_id:
            gc = self.get_group_config(group_id)
            if gc.command_filter_enabled is not None:
                return gc.command_filter_enabled
        return self.command_filter_enabled

    def resolve_command_filter_unknown(self, group_id: str | None = None) -> bool:
        """未知指令处理：True=过滤，False=放行(默认)。群配置非 None 时覆盖全局。"""
        if group_id:
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
        if group_id:
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
        return AdapterConfig()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("config load failed (%s), using defaults: %s", target, exc)
        return AdapterConfig()
    return AdapterConfig.from_dict(data)


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
        "group_session_mode": "可选值: shared(共享会话,默认) | per_user(独立会话)",
        "dm_user_filter_mode": "可选值: whitelist(白名单,默认) | blacklist(黑名单)",
        "log_level": "可选值: DEBUG | INFO(默认) | WARNING | ERROR",
        "groups": "群组配置,key为群号字符串,value为群配置对象;子字段require_mention等为null时跟随全局",
        "reaction_emoji_enabled": "消息送达 Hermes 后在原消息贴表情回应;群配置可单独覆盖",
        "reaction_emoji_id": "贴表情回应使用的表情ID(默认 76=👍),QQ 表情编号",
    }
    result: dict[str, Any] = {}
    for key, value in d.items():
        cmt = comments.get(key)
        if cmt:
            result[f"_comment_{key}"] = cmt
        result[key] = value
    return result


def save_config(cfg: AdapterConfig, path: Path | None = None) -> None:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    data = _inject_comments(cfg.to_dict())
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def ensure_tokens(cfg: AdapterConfig, path: Path | None = None) -> AdapterConfig:
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
        cfg = cfg.with_overrides(**changes)
        save_config(cfg, path)
    return cfg


class ConfigStore:
    """Thread-safe config holder with change notification for hot-reload."""

    def __init__(self, cfg: AdapterConfig | None = None) -> None:
        self._cfg = cfg or AdapterConfig()
        self._lock = threading.Lock()
        self._listeners: list = []

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
                        loop.create_task(result)
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

# 聊天与过滤

安装与连接见根目录 [README.md](../README.md)。页面入口见 [WebUI 与工具](webui.md)。字段定义见 [api.md](api.md)。

- [准入控制](#准入控制)
- [/指令过滤](#指令过滤)
- [出站消息过滤](#出站消息过滤)
- [未匹配转发（Cascade WS）](#未匹配转发cascade-ws)
- [媒体投递](#媒体投递)
- [notice 事件](#notice-事件)
- [群聊消息排队](#群聊消息排队)

## 准入控制

适配器服务对群与私聊分别提供准入控制，均在 WebUI 配置。适配器在消息转发到 Hermes 之前完成所有过滤/鉴权，Hermes 网关侧无需重复配置准入名单。

**群聊**：每群通过「启用 Bot」开关控制是否处理该群；群内成员通过群配置的「群成员过滤模式 + 名单」控制（默认黑名单空 = 允许所有群成员）。不再使用全局群白/黑名单。

**私聊**：通过全局「私聊模式 + 常驻黑白名单」控制，WebUI「聊天配置 → 私聊设置」。判定顺序：bot 动态黑名单 → 私聊黑名单 → 私聊白名单 → 模式。默认禁止私聊（与旧「白名单空」一致）。

| 作用域 | 配置 | 默认 | 语义 |
|--------|------|------|------|
| 群成员 | 群配置 → 群成员过滤模式/名单 | 黑名单空 | 名单内禁用，空名单=允许所有人 |
| 群成员 | 群配置 → 群成员过滤模式/名单 | 白名单非空 | 仅名单内可用，空名单=拒绝所有人 |
| 私聊 | 聊天配置 → 私聊模式 | 禁止私聊 | `allow` / `deny` / `friends`（仅限好友走 `get_friend_list`） |
| 私聊 | 聊天配置 → 私聊白名单 | 空 | 始终允许私聊（不能覆盖 bot 动态黑名单） |
| 私聊 | 聊天配置 → 私聊黑名单 | 空 | 始终禁止私聊；与白名单冲突时黑名单优先 |
| 私聊 | 聊天配置 → 私聊被拒时回复 | 关闭 | 开启后回复 `dm_reject_message`（默认 `⛔ 当前私聊策略为：{reason}`）；关闭则静默丢弃。`{reason}`：禁止私聊/黑名单 →「禁止私聊」，仅限好友 →「仅限好友」 |

管理工具（踢人/禁言等）的鉴权由适配器的 `global_admins` / 群配置 `admins` 决定，非管理员调用时适配器直接拒绝。

Bot 动态黑名单独立保存到 `~/.onebot_adapter/bot_blacklist.sqlite3`，不与上述准入名单混用。Bot 通过 `onebot_get_bot_blacklist` / `onebot_edit_bot_blacklist` 工具临时拉黑用户（全局管理员和对应群管理员始终豁免），WebUI「聊天配置」页可设置 `bot_blacklist_enabled`/`bot_blacklist_max_duration_seconds`/`bot_blacklist_reject_message`，也可查看及人工解除有效记录。群聊仅在消息本应触发 bot 时检查并回复提示；私聊记录、群记录和全局记录按各自作用域生效。

## 未匹配转发（Cascade WS）

开启 `cascade_ws_enabled` 后，适配器额外监听一条 OneBot 11 反向 WS（默认 `18830` `/onebot`）。群聊入站顺序是 **群是否启用 → 匹配（@bot / 关键词）→ 过滤**（用户名单、黑名单、指令权限、限流等）：

- **群已关闭**：静默丢弃，不转发。
- **匹配失败**：原始 OneBot JSON 原样发给已连接的下游客户端。没有下游时仍静默丢弃。
- **匹配成功**：无论之后是否真的进 Hermes，都不转发。拒绝提示仍按现逻辑发送。
- **私聊**：视为发给本 bot，不转发。
- 群未要求 @、也没有关键词时，全部群消息视为匹配成功，不会 cascade。

注意：[入站限流](ops.md#入站消息限流)只在消息匹配成功、即将投递 Hermes 时才检查；匹配失败的 cascade 流量不经过限流器，过载时仅受出站队列上限约束（满则丢弃并记 warning）。

下游发到该端口的 text 帧原样写到当前 OneBot 连接；带 `echo` 的 API 响应回到当前这一个下游客户端（新连接会替换旧连接）。API 等待上限与 `file_upload_timeout` 相同。心跳 / lifecycle（含下游连上时补发的 `lifecycle/connect`）由 `cascade_forward_meta` 控制（默认转发）。开启、改端口或路径后需重启适配器。WebUI「连接管理」页可配置。

## /指令过滤

适配器启动后，Hermes 插件会将 Hermes 已注册的所有 `/` 指令（内置 + 插件注册）推送给适配器服务。适配器在消息进入 Hermes 之前，根据指令权限配置进行过滤。

**匹配方式**：去除消息中所有 @bot 段后，从开头匹配 `/xxx`（小写化、支持别名解析、兼容 Telegram 风格 `/cmd@BotName`）。

**权限级别**（每指令可配置）：

| 级别 | 说明 |
|------|------|
| 所有人 (everyone) | 任何用户均可使用 |
| 仅管理员 (admin) | 仅全局/群管理员可用 |
| 禁用 (disabled) | 完全禁用此指令 |
| 未配置 | 默认放行 |

**配置项**（WebUI「指令过滤」页面）：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `command_filter_enabled` | `false` | 指令过滤总开关 |
| `command_filter_unknown` | `false` | 未知指令（不在 Hermes 列表）处理：`true`=过滤，`false`=放行 |
| `command_permissions` | `{}` | 指令权限映射 `{指令名: everyone\|admin\|disabled}` |
| `command_reject_message` | `⛔ 你没有权限使用此指令 /{cmd}` | 拒绝消息模板（`{cmd}` 替换为指令名） |

**每群覆盖**：群配置中可覆盖 `command_filter_enabled`、`command_filter_unknown`、`command_permissions`，与现有群配置模式一致（`None`=跟随全局）。

被过滤的指令会通过当前 OneBot WebSocket 上的 API 调用向原聊天发送拒绝消息，不会送入 Hermes 处理。指令过滤在媒体下载之前执行，避免浪费带宽。

## 出站消息过滤

拦截 **Hermes 发往 QQ** 的文本。在 WebUI「聊天配置」启用后，按配置的 Python 正则（`re.search`）匹配发送正文：命中任意一条则不调用 OneBot 发送接口，并向插件返回成功（避免 Gateway 超时重试把同一条内容再发一遍）。

匹配范围：

- `send` 帧：`send_text` 的 `content`，以及 `send_image` / `send_voice` / `send_video` / `send_document` 的 `caption`
- 插件 `api_call`：`send_msg` / `send_group_msg` / `send_private_msg` 中的纯文本段（字符串 `message` 或 text 消息段拼接）

不匹配：无说明的纯媒体/文件、适配器本地发出的拒绝提示（`send_direct_message`）、HTTP 自动化 API。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `outbound_filter_enabled` | `false` | 出站过滤总开关 |
| `outbound_filter_patterns` | `[]` | 正则列表；空=不过滤。支持内联标志如 `(?i)`、`(?s)` |

**每群覆盖**：`outbound_filter_enabled` / `outbound_filter_patterns` 为 `None` 时跟随全局；群级 `patterns` 整表覆盖（不与全局合并），`[]` 表示此群无规则。

## 媒体投递

入站图片/语音/视频/文件由 `media_delivery_mode` 控制（WebUI「聊天配置」，热更新后向已连接插件广播新的 `ready` 帧）：

| 模式 | 行为 |
|------|------|
| `cache`（默认） | 文本里放空占位符（`[图1]`），插件把媒体下载到 `~/.hermes/cache/`（校验重定向与解析后的 IP，允许回环/局域网），填入 `MessageEvent.media_urls`；缓存失败则跳过该媒体、保留占位符 |
| `passthrough` | 文本里内联 URL 占位符（`[图1](https://...)`），`media_items` 为空，由 LLM 按需拉取 |

没有 URL 的 file 段一律跳过，LLM 用 `onebot_get_file` 按 `file_id` 获取。适配器本身不再下载或转码语音。

## notice 事件

默认关闭。开启后将 OneBot notice 合成为以 `[系统]` 开头的文本，走与普通消息相同的排队和投递路径（插件设 `MessageEvent.internal=True`，绕过 Hermes 文本去抖）。群配置可单独覆盖。

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `notify_poke_enabled` | `false` | 仅 bot 被戳时推送（含私聊），走群/DM 用户过滤 |
| `notify_member_change_enabled` | `false` | 其他成员进群/退群；退群区分主动离开 `leave` 与被踢 `kick` |

## 群聊消息排队

适配器内置 shared 群聊消息排队机制，防止群聊中多个群成员的消息互相打断 agent 当前任务。**只在 Hermes 配置 `group_sessions_per_user: false`（全群共享 session）且适配器 `event_queue_enabled: true` 时生效**；per_user 模式每人独立 session，无需排队。

若群聊消息一直排队、`/stop` 提示没有活跃任务，发 `/clean` 可清空队列并释放 busy，不必重启适配器。

### 排队规则

| 场景 | 行为 |
|------|------|
| 私聊 | 直接转发，不排队 |
| Hermes 隔离群成员（per_user=True） | 直接转发，不排队 |
| 适配器排队总开关关闭 | 直接转发，不排队 |
| 群未 busy | 标记 busy，转发 |
| 群 busy，发送者相同且队列为空 | 直接转发（刷新 busy 时间戳，不入队；仍算同一 Hermes session） |
| 群 busy（其他情况） | 入队等待（含 busy 用户自身；队列非空时 busy 用户也不能插队）；出队时连续同用户消息合并为一条 |
| `/new`、`/reset` | 绕过排队发给 Hermes；默认同时清空当前群待处理队列（不释放 busy） |
| `/clean` | 默认由适配器本地清空当前群待处理队列**并释放 busy**，不发送给 Hermes |
| 其他 `/` 开头的消息 | **始终直接转发**（绕过排队） |

所有 `/` 指令都不占用适配器的 busy 槽，也不会注册用于释放 busy 槽的 idle 回调；它们不会扰动正在处理的普通群消息及其排队顺序。

### Hermes 会话隔离配置

`group_sessions_per_user` 是 Hermes 顶层的唯一真相源。适配器 WebUI（连接管理页）可直接修改 Hermes `config.yaml` 的此字段，修改后需重启 Hermes 网关生效。插件连接后会上报当前值给适配器，适配器据此决定是否排队。

### idle 信号

处理完成的 idle 信号由 Hermes 插件在 `on_processing_complete` 中发送：仅当 shared 群聊 session 没有 pending/debounce 后续时发 `idle` 帧；适配器把它当作 **session 空闲**并出队下一条。同一发送者在 busy 且队列为空时仍直推（喂给当前 Hermes session 的 redirect/steer/pending），但不再按直推次数等待多帧 idle。若插件崩溃或 idle 帧丢失，看门狗会在超时后强制清空 busy。

`/stop`、`/new`、`/reset` 命令会导致 Hermes 中断当前 turn 但**不触发 idle 帧**，适配器会在 broadcast 这些命令 3 秒后主动清空 busy 槽防止队列卡死（识别 busy 代数，不受 bot 发送刷新时间戳影响）。`/stop` 只中断 Hermes 任务：若网关已经空闲（回复「没有可停止的活跃任务」），适配器 busy 不会因此解开。

默认情况下，`/new` 和 `/reset` 还会立即丢弃当前群尚未处理的排队消息，但**不**释放 busy；`/clean` 清空队列并释放 busy，可在排队卡死时手动解堵，且不发送给 Hermes。`/new`/`/reset` 的队列清理不会中断当前正在执行的 turn。

### 配置项（WebUI「聊天配置」页面）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `event_queue_enabled` | `true` | 排队总开关：Hermes 不隔离群成员时是否排队 |
| `event_queue_max_per_chat` | `50` | 单群队列上限，超限拒绝入队 |
| `event_queue_idle_timeout` | `300.0` | plugin 无 idle 信号的超时阈值（秒），超时强制清空 busy |
| `event_queue_clear_on_session_reset` | `true` | 使用 `/new`、`/reset` 时清空当前群待处理队列 |
| `event_queue_clean_command_enabled` | `true` | 启用适配器本地 `/clean` 清队列并释放 busy |

![:name](https://count.getloli.com/@hermes-onebot-adapter?name=hermes-onebot-adapter&theme=original-new&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)
# Hermes OneBot Adapter

OneBot 11 适配器服务 + Hermes 插件，经独立服务对接 NapCat / go-cqhttp 等 OneBot 11 实现（目前仅在 NapCat 下测试过）。

## 架构

```text
NapCat ──双向 OneBot 11 WS（事件 + API）── 适配器服务 ──WS── Hermes 插件 ── Hermes Agent
```

适配器服务承担全部 OneBot 交互；事件接收和 API 调用共用同一条 OneBot WebSocket，不需要独立的 OneBot HTTP API 端口。插件只与适配器服务通信，不直接接触 OneBot，也不修改 Hermes 本身的代码。

更细的 REST API 字段、专项诊断和维护者架构说明见 [文档索引](docs/README.md)。

- [快速开始](#快速开始)
- [配置流程](#配置流程)
- [CLI 用法](#cli-用法)
- [自动化工具 API](#自动化工具-api)
- [OneBot 连接模式](#onebot-连接模式)
- [WebUI 功能](#webui-功能)
- [准入控制](#准入控制)
- [出站消息过滤](#出站消息过滤)
- [媒体投递](#媒体投递)
- [notice 事件](#notice-事件)
- [日志](#日志)
- [群聊消息排队](#群聊消息排队)

## 环境要求

- Python >= 3.11
- [pipx](https://pipx.pypa.io/)（推荐）或 pip
- Node.js ^20.19.0 或 >= 22.12.0（仅源码安装或开发时构建前端需要；PyPI 安装不需要）

## 快速开始

```bash
pipx install hermes-onebot-adapter    # 从 PyPI 安装
hermes-onebot-adapter                 # 启动服务，WebUI 默认 http://localhost:18820
```

从源码安装需先编译前端：

```bash
./scripts/build_frontend.sh           # 编译前端 (需要 Node.js)
pipx install .                        # 安装
hermes-onebot-adapter                 # 启动服务
```
首次启动会自动生成 `~/.onebot_adapter/config.json`（含随机 token）。也可先手动生成：

```bash
hermes-onebot-adapter --init-config   # 生成默认配置后退出
```

已有安装升级时，适配器和复制到 Hermes 目录中的插件需要一起更新：

```bash
pipx upgrade hermes-onebot-adapter
hermes-onebot-adapter install --hermes-dir ~/.hermes
hermes gateway restart
```

升级不会覆盖现有适配器配置。安装器会更新 `<hermes>/plugins/onebot/` 中的插件文件；重启后可在 WebUI 仪表盘确认适配器与插件版本一致。

## 配置流程

1. **启动适配器服务** — `hermes-onebot-adapter`
2. **打开 WebUI** — 浏览器访问 `http://localhost:18820`，登录后进入管理界面
3. **配置 OneBot 连接** — 在 WebUI 的"连接管理"页选择连接模式（反向 WS / 正向 WS），填写 WS 地址和 token
4. **安装 Hermes 插件** — 在 WebUI 的"连接管理"页填写 Hermes 安装目录（默认 `~/.hermes`），点击"安装插件到 Hermes"
5. **启用插件** — `hermes plugins enable onebot-platform`
6. **重启 Hermes 网关** — `hermes gateway restart`

安装插件时，Installer 自动完成三件事：

| 操作 | 说明 |
|------|------|
| 复制插件文件 | 5 个文件 → `<hermes>/plugins/onebot/` |
| 写入环境变量 | `ONEBOT_ADAPTER_URL` + `ONEBOT_ADAPTER_TOKEN` → `<hermes>/.env` |
| 初始化工具集 | 写入 `platform_toolsets.onebot` → `<hermes>/config.yaml` |

以上均需**启用插件并重启 Hermes 网关**后生效。

也可通过 CLI 安装：

```bash
hermes-onebot-adapter install --hermes-dir ~/.hermes
```

## CLI 用法

```bash
# 启动服务
hermes-onebot-adapter                         # 默认 127.0.0.1
hermes-onebot-adapter --host 0.0.0.0          # 监听所有网络接口
hermes-onebot-adapter --port 18820            # 指定 WebUI 端口
hermes-onebot-adapter --no-webui              # 不启动 WebUI (仅 WS 服务)

# 配置管理
hermes-onebot-adapter --init-config           # 生成默认配置文件后退出
hermes-onebot-adapter --init-config --force   # 覆盖已有配置 (保留 token，其余重置为默认)
hermes-onebot-adapter --generate-api-key --enable-api  # 生成自动化 API key 并启用
hermes-onebot-adapter --rotate-api-key         # 轮换 key（仅显示一次）
hermes-onebot-adapter --revoke-api-key         # 撤销 key 并关闭自动化 API
hermes-onebot-adapter --enable-api             # 启用已有 key
hermes-onebot-adapter --disable-api            # 关闭自动化 API

# 插件安装 (默认从 config.json 读取 URL 和 token)
hermes-onebot-adapter install                          # 安装到 ~/.hermes
hermes-onebot-adapter install --hermes-dir /opt/hermes # 指定安装目录
hermes-onebot-adapter install --adapter-url ws://host:18810/hermes --adapter-token xxx  # 手动指定连接参数
hermes-onebot-adapter uninstall                        # 卸载
hermes-onebot-adapter uninstall --hermes-dir /opt/hermes
```

## 自动化工具 API

自动化 API 默认关闭。它使用独立于 WebUI 登录的全权限 key，可调用全部 100 个 OneBot 工具，包括文件上传：

```bash
# key 只显示一次；请保存到调用脚本的安全环境变量中
hermes-onebot-adapter --generate-api-key --enable-api

curl -H "Authorization: Bearer hoa_xxx" \
  http://127.0.0.1:18820/api/v1/tools
```

每个工具使用独立的 RPC 路径 `POST /api/v1/tools/<tool_name>`。完整参数可从 `GET /api/v1/tools` 或 `/api/v1/openapi.json` 获取。HTTP 调用不会继承 Hermes 的当前聊天上下文，发送消息或上传文件时必须显式提供与 `message_type` 匹配的 `group_id` 或 `user_id`。Key 可执行踢人、禁言、退群、删好友等高风险操作，不要放入 URL、日志或前端代码。

本地文件默认只允许来自 `/tmp/hermes-onebot-adapter-uploads`；可在 WebUI 高级设置中调整 `automation_upload_allowed_roots`。闪传与文件集工具仅 Windows 版客户端可用，在其他平台调用会失败。qBittorrent hook 需要在其进程环境中设置：

```bash
ONEBOT_AUTOMATION_API_KEY=hoa_xxx
```

## 三端口

| 端口  | 用途 |
|------|------|
| 18800 | OneBot WS 服务端 `/onebot`（反向 WS 模式，OneBot 连接此端口；正向 WS 模式不使用） |
| 18810 | Hermes 插件 WS 服务端 `/hermes?token=`（插件连接适配器的端口） |
| 18820 | WebUI + REST API + 健康检查 (`/api/v1/health`)（详见 [API 文档](docs/api.md)） |

## 环境变量

### 适配器服务

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ONEBOT_ADAPTER_CONFIG` | `~/.onebot_adapter/config.json` | 配置文件路径 |

### 自动化客户端

| 变量 | 必填 | 说明 |
|------|------|------|
| `ONEBOT_AUTOMATION_API_KEY` | 是 | qBittorrent hook 使用的自动化 key；这是客户端约定，适配器服务本身不从该变量加载 key |

### Hermes 插件

| 变量 | 必填 | 说明 |
|------|------|------|
| `ONEBOT_ADAPTER_URL` | 是 | 适配器服务 WS 地址 (`ws://host:18810/hermes`) |
| `ONEBOT_ADAPTER_TOKEN` | 是 | 适配器服务鉴权 token |
| `ONEBOT_HOME_CHANNEL` | 否 | cron 投递目标 chat_id |

## OneBot 连接模式

### 反向 WS（推荐）

OneBot 主动连接适配器服务。在 OneBot WebUI 中配置反向 WS 地址：
```
ws://127.0.0.1:18800/onebot
```

### 正向 WS

适配器主动连接 OneBot。在适配器 WebUI 中配置 OneBot 的正向 WS 地址：
```
ws://127.0.0.1:3001
```

模式切换可在 WebUI 中热重载，无需重启服务。

## WebUI 功能

| 页面 | 功能 |
|------|------|
| 仪表盘 | 服务/插件状态、版本检查、使用统计图表（趋势/活跃群/活跃用户） |
| 连接管理 | 配置 OneBot 连接模式和 WS 地址；安装/卸载 Hermes 插件 |
| 聊天配置 | 全局群聊触发、出站正则过滤、入站限流、Bot 动态黑名单、会话隔离与群聊排队、媒体投递、notice 事件、贴表情回应；同页管理群组（每群启用/成员过滤及覆盖项） |
| 指令过滤 | 管理 `/` 指令的权限（所有人 / 管理员 / 禁用） |
| 工具管理 | 启停 OneBot 平台的 Hermes 工具集 |
| OneBot 工具 | 逐项控制 OneBot 工具是否注册给 Hermes，以及所有人/管理员调用权限 |
| 高级设置 | WebUI Token、登录有效期、自动化 API、文件上传超时、使用统计、发送去重、序列号映射、文件日志策略 |
| 日志 | 内存环形缓冲（最近 500 条）或 `adapter.log` 尾部；可下载当前日志文件 |

## 工具集管理

安装插件时会自动写入默认工具集配置到 `<hermes>/config.yaml`。之后可通过 WebUI 的"工具管理"页面自主启停工具集。

工具集修改后写入 Hermes 的 `config.yaml`，**需重启 Hermes 网关生效**。适配器只负责写配置文件，不触发热重载。

## OneBot API 工具

插件提供 100 个 OneBot 工具（toolset: `onebot`），其中 88 个默认注册给 LLM，其余 12 个默认隐藏，可在 WebUI「OneBot 工具」页启用：

- **只读**：获取群列表/成员/扩展信息、好友、消息历史、精华、公告、禁言列表、签到、相册与收藏
- **消息**：发送消息、撤回、合并转发、戳一戳、表情回应
- **文件**：获取和上传群/私聊文件，查询群文件目录、容量与下载地址，管理群文件和文件夹
- **管理**：踢人、禁言、设置群资料、精华、公告、待办、相册、好友资料和自定义表情
- **动态黑名单**：查看或临时拉黑群聊、私聊或全部会话中的用户；管理员始终豁免

**闪传与文件集（仅 Windows）**：8 个闪传工具（创建/发送闪传任务、分享链接、文件集查询与下载等）默认对 Hermes 隐藏。闪传是 PC 版 QQ 端的能力，**只有 Windows 版客户端可用**，且在 Linux 等平台运行 NapCat 时不可用；同时它们会读取本机文件或写入下载目录。需要时请在 WebUI「OneBot 工具」页显式启用（修改后需重启 Hermes 生效）。HTTP 自动化 API 始终包含这些工具，但调用前请确认 NapCat 运行在 Windows 客户端上。

WebUI「OneBot 工具」页可以为每个工具设置是否注册给 Hermes，以及 `everyone` / `admin` 权限。策略只影响 Hermes，使用 automation key 的 HTTP API 始终保留完整工具目录和全权限。注册状态在 Hermes 启动时读取，修改后需要重启 Hermes；群管理员只能对当前群调用群级管理员工具，跨群和账号级管理员工具仅允许全局管理员调用。维护者可以显式把默认管理员工具降级为所有人。

群聊、私聊和闪传文件上传等待 NapCat 响应的时间统一由 `file_upload_timeout` 控制，默认 600 秒，可在 WebUI 高级设置中调整为 30–600 秒。群上传超时后适配器仍会短轮询群历史进行保守确认；闪传上传超时无法确认结果。两种情况都会返回不可自动重试的“结果未知”，避免重复上传。

## 使用统计

适配器记录通过准入与指令过滤的消息元数据（QQ 号、群号、时间、是否系统事件，**不含消息正文或媒体地址**）到 `~/.onebot_adapter/usage_stats.sqlite3`，默认保留 365 天。WebUI 仪表盘展示趋势、活跃群、活跃用户图表，可按时间、范围（全部/私聊/群聊）、群、用户过滤。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `usage_stats_enabled` | `true` | 统计开关；关闭后停止新增，已有历史仍可查询 |
| `usage_stats_retention_days` | `365` | 保留天数；缩短后保存配置会立即清理过期数据 |

数据也可通过 REST API 查询/清空（见 [API 文档](docs/api.md)）。

## 入站消息限流

适配器在准入控制和指令过滤通过后、转发 Hermes 之前对消息限流。全局、群聊、个人三个维度同时检查，命中任一维度即回复原消息并拦截。全局管理员和对应群管理员不受限流；个人计数在私聊和所有群之间共享。限额 0 表示禁用该维度。

| 维度 | 配置 | 说明 |
|------|------|------|
| 全局总量 | `global_rate_limit_(algorithm\|messages\|window_seconds)` | 全局消息总量 |
| 群聊 | `group_rate_limit_(algorithm\|messages\|window_seconds)` | 每个群独立计数；GroupConfig 可 per-group 覆盖 |
| 个人 | `user_rate_limit_(algorithm\|messages\|window_seconds)` | 每个 QQ 独立计数，跨私聊和所有群 |

算法支持滑动窗口（`sliding_window`，窗口内消息数上限）和令牌桶（`token_bucket`，按速率补充令牌）。额度持久化到 `~/.onebot_adapter/rate_limit.sqlite3`，进程重启不会重置；关闭总开关时保留状态并按经过的真实时间自然恢复。`rate_limit_storage_failure_mode` 可选数据库故障时退回内存限流（默认）或拒绝受限流消息。

拦截提示模板 `rate_limit_reject_message` 支持 `{scope}`/`{retry_after}`/`{user_id}` 占位符。WebUI「聊天配置」页可查询和定向重置全局、群聊或用户额度，并查看持久化健康状态。

## 配置备份与审计

每次保存配置时自动备份现有文件为 `config.json.bak.<时间戳>`，保留最近 5 个。同时追加一条 JSON 审计记录到 `~/.onebot_adapter/logs/config-audit.log`（按日轮转，保留 365 天），记录改动字段、来源、操作者、客户端 IP 及新旧配置指纹。检测到疑似重置（≥5 字段回退默认值或群配置清空）时额外输出告警日志。

配置文件损坏或字段非法时，适配器启动 fail-fast（`ConfigLoadError`），不再回退默认配置并覆盖原文件——需修正配置或从 `.bak` 备份恢复后才能启动。

## 准入控制

适配器服务对群与私聊分别提供准入控制，均在 WebUI 配置。适配器在消息转发到 Hermes 之前完成所有过滤/鉴权，Hermes 网关侧无需重复配置准入名单。

**群聊**：每群通过「启用 Bot」开关控制是否处理该群；群内成员通过群配置的「群成员过滤模式 + 名单」控制（默认黑名单空 = 允许所有群成员）。不再使用全局群白/黑名单。

**私聊**：通过全局「私聊过滤模式 + 名单」控制（默认白名单空 = 拒绝所有私聊，需显式加入白名单才放行）。

| 作用域 | 配置 | 默认 | 语义 |
|--------|------|------|------|
| 群成员 | 群配置 → 群成员过滤模式/名单 | 黑名单空 | 名单内禁用，空名单=允许所有人 |
| 群成员 | 群配置 → 群成员过滤模式/名单 | 白名单非空 | 仅名单内可用，空名单=拒绝所有人 |
| 私聊 | 高级设置 → 私聊过滤模式/名单 | 白名单空 | 仅名单内可私聊，空名单=拒绝所有人；黑名单则相反 |

管理工具（踢人/禁言等）的鉴权由适配器的 `global_admins` / 群配置 `admins` 决定，非管理员调用时适配器直接拒绝。

Bot 动态黑名单独立保存到 `~/.onebot_adapter/bot_blacklist.sqlite3`，不与上述准入名单混用。Bot 通过 `onebot_get_bot_blacklist` / `onebot_edit_bot_blacklist` 工具临时拉黑用户（全局管理员和对应群管理员始终豁免），WebUI「聊天配置」页可设置 `bot_blacklist_enabled`/`bot_blacklist_max_duration_seconds`/`bot_blacklist_reject_message`，也可查看及人工解除有效记录。群聊仅在消息本应触发 bot 时检查并回复提示；私聊记录、群记录和全局记录按各自作用域生效。

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
| `cache`（默认） | 文本里放空占位符（`[图1]`），插件把媒体下载到 `~/.hermes/cache/`，填入 `MessageEvent.media_urls`；缓存失败则跳过该媒体、保留占位符 |
| `passthrough` | 文本里内联 URL 占位符（`[图1](https://...)`），`media_items` 为空，由 LLM 按需拉取 |

没有 URL 的 file 段一律跳过，LLM 用 `onebot_get_file` 按 `file_id` 获取。适配器本身不再下载或转码语音。

## notice 事件

默认关闭。开启后将 OneBot notice 合成为以 `[系统]` 开头的文本，走与普通消息相同的排队和投递路径（插件设 `MessageEvent.internal=True`，绕过 Hermes 文本去抖）。群配置可单独覆盖。

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `notify_poke_enabled` | `false` | 仅 bot 被戳时推送（含私聊），走群/DM 用户过滤 |
| `notify_member_change_enabled` | `false` | 其他成员进群/退群；退群区分主动离开 `leave` 与被踢 `kick` |

## 日志

控制台和 WebUI 内存视图走独立的 `onebot_adapter.onebot.message_preview` logger（不写入文件）。文件日志由 `log_file_message_mode` 控制（`none` / `preview` / `full`，默认 `preview`），达到 `log_file_max_bytes`（默认 10 MiB）后轮转。未进入 Hermes 的候选消息只在 DEBUG 记 `丢弃 -- reason=`，不含正文。WebUI「日志」页可切换内存缓冲与 `adapter.log` 尾部，并下载当前文件。

## 群聊消息排队

适配器内置 shared 群聊消息排队机制，防止群聊中多个群成员的消息互相打断 agent 当前任务。**只在 Hermes 配置 `group_sessions_per_user: false`（全群共享 session）且适配器 `event_queue_enabled: true` 时生效**；per_user 模式每人独立 session，无需排队。

### 排队规则

| 场景 | 行为 |
|------|------|
| 私聊 | 直接转发，不排队 |
| Hermes 隔离群成员（per_user=True） | 直接转发，不排队 |
| 适配器排队总开关关闭 | 直接转发，不排队 |
| 群未 busy | 标记 busy，转发 |
| 群 busy，发送者相同且队列为空 | 直接转发（刷新 busy 时间戳，不入队） |
| 群 busy（其他情况） | 入队等待（含 busy 用户自身；队列非空时 busy 用户也不能插队）；出队时连续同用户消息合并为一条 |
| `/new`、`/reset` | 绕过排队发给 Hermes；默认同时清空当前群待处理队列 |
| `/clean` | 默认由适配器本地清空当前群待处理队列，不发送给 Hermes |
| 其他 `/` 开头的消息 | **始终直接转发**（绕过排队） |

所有 `/` 指令都不占用适配器的 busy 槽，也不会注册用于释放 busy 槽的 idle 回调；它们不会扰动正在处理的普通群消息及其排队顺序。

### Hermes 会话隔离配置

`group_sessions_per_user` 是 Hermes 顶层的唯一真相源。适配器 WebUI（连接管理页）可直接修改 Hermes `config.yaml` 的此字段，修改后需重启 Hermes 网关生效。插件连接后会上报当前值给适配器，适配器据此决定是否排队。

### idle 信号

处理完成的 idle 信号由 Hermes 插件在 `on_processing_complete` 中发送：每轮 agent 处理结束后插件向适配器发 `idle` 帧；适配器在该群 inflight 归零后清空 busy 并从队列取下一条。同一发送者在 busy 期间直推会增加 inflight，避免第一条 idle 提前放出下一个人。若插件崩溃或 idle 帧丢失，看门狗会在超时后强制清空 busy。

`/stop`、`/new`、`/reset` 命令会导致 Hermes 中断当前 turn 但**不触发 idle 帧**，适配器会在 broadcast 这些命令 3 秒后主动清空 busy 槽防止队列卡死。默认情况下，`/new` 和 `/reset` 还会立即丢弃当前群尚未处理的排队消息；`/clean` 可手动执行同样的队列清理，但不会发送给 Hermes。两种清理都不会中断当前正在执行的 turn。

### 配置项（WebUI「聊天配置」页面）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `event_queue_enabled` | `true` | 排队总开关：Hermes 不隔离群成员时是否排队 |
| `event_queue_max_per_chat` | `50` | 单群队列上限，超限拒绝入队 |
| `event_queue_idle_timeout` | `300.0` | plugin 无 idle 信号的超时阈值（秒），超时强制清空 busy |
| `event_queue_clear_on_session_reset` | `true` | 使用 `/new`、`/reset` 时清空当前群待处理队列 |
| `event_queue_clean_command_enabled` | `true` | 启用适配器本地 `/clean` 清队列命令 |

## 开发

```bash
pip install -e ".[dev]"          # 开发安装（可编辑模式 + dev 依赖）
pytest -q                        # 运行测试
ruff check .                     # 代码检查
cd frontend && npm install && npm run dev   # 前端开发 (Vite 代理到 :18820)
./scripts/build_frontend.sh      # 构建前端到 webui/static/
```

维护者架构与模块约定见 [AGENTS.md](AGENTS.md)；文档目录见 [docs/README.md](docs/README.md)。发布前更新 [CHANGELOG.md](CHANGELOG.md)，再打 `vX.Y.Z` 标签。

## 配置文件

适配器服务配置持久化于 `~/.onebot_adapter/config.json`（或 `ONEBOT_ADAPTER_CONFIG` 指定路径），WebUI 修改即保存。完整字段见 [REST API 文档](docs/api.md#config-字段)。损坏或非法的配置文件会使服务 fail-fast，不会覆盖原文件。

## 技术栈

- **后端**：aiohttp（WS 服务端/客户端、WebUI REST API、静态托管）
- **前端**：Vue 3 + Vite + TypeScript + Vue Router
- **打包**：pyproject.toml + setuptools，`hermes-onebot-adapter` CLI entry point

## License

MIT

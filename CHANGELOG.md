# 更新日志

## [Unreleased]

### 文档
- 新增 Windows / WSL2 部署教程（`docs/wsl.md`）：适配器与 Hermes 装在 WSL，NapCat 留在 Windows 走反向 WS
- README 改为安装/连接入口；WebUI、聊天过滤、运维说明拆到 `docs/webui.md` / `docs/chat.md` / `docs/ops.md`

## [1.8.0] - 2026-09-02

### 新增
- `hermes_install_allowed_roots`（默认 `[]`）：给 `/opt/hermes` 等非常规安装追加允许根；不可为 `/`、盘符根、`/etc`、`/proc`、`/sys`
- CLI `--webui-host` / `--onebot-host` / `--hermes-host`：三个端口可分别绑定；未指定时回落到 `--host`（默认仍 `127.0.0.1`）
- OneBot 反向 WS 接受 OneBot 11 标准 query `access_token`（仍接受 `token`；`Authorization: Bearer` 优先）

### 变更
- Hermes 安装目录默认只允许当前用户 `$HOME`（含 `~/.hermes`、仍落在 `$HOME` 下的 `$HERMES_HOME`），不再放行整个 `/home` 或 `/tmp`
- `automation_upload_allowed_roots` 必须是绝对路径；拒绝 `/`、盘符根、`/etc`、`/proc`、`/sys` 和整个 `/tmp`。默认专用目录 `/tmp/hermes-onebot-adapter-uploads` 仍可用
- 远程 NapCat 推荐 `hermes-onebot-adapter --onebot-host 0.0.0.0`。`--host 0.0.0.0` 仍会暴露三个口（无 TLS），不推荐；WebUI / Hermes 绑到非回环时打 WARNING
- 重装插件不再覆盖 WebUI「工具管理」已保存的 `platform_toolsets.onebot`（含空列表）；仅在该键不存在时写入默认工具集
- 启动日志每个端口只打一次 listening；WebUI ready 与非回环 WARNING 仍保留

### 修复
- `PATCH /api/v1/config`、工具集读写和 `venv/bin/python` 子进程都校验 Hermes 安装白名单；越界返回 `hermes_install_dir is outside the allowed Hermes install roots`
- 安装/卸载用 `O_NOFOLLOW` 写文件；卸载前 `lstat` 拒绝 symlink；`__pycache__` 若属 root 删不掉时不阻断安装/卸载
- WebSocket token 恒定时间比较；错的 query 不再盖掉对的 Bearer
- `config.json` 及其 `.bak.*` 写成 `0600`；启动时若 group/other 可读则收紧一次
- 插件 cache 模式拉媒体：最多 3 跳重定向，每跳解析 A/AAAA 且全部地址过策略；连接钉死已校验 IP，不再跟 aiohttp 二次 DNS；图/语音不再走 Hermes `cache_*_from_url`

### 升级说明
- **必须**重新安装随包 Hermes 插件并重启网关，媒体下载校验才会生效：
  ```bash
  pipx upgrade hermes-onebot-adapter
  hermes-onebot-adapter install --hermes-dir ~/.hermes
  hermes gateway restart
  ```
  重装不会重置工具管理里已保存的工具集
- systemd / 远程 NapCat 把 `--host 0.0.0.0` 改成 `--onebot-host 0.0.0.0`（WebUI 仍需局域网访问时再加 `--webui-host 0.0.0.0`）
- Hermes 装在 `/tmp`、他人 `$HOME`、或 `/opt` 且未写 extra roots 的，**启动会失败**（WebUI 也起不来）：把目录迁到 `$HOME` 下，或手改 `config.json` 的 `hermes_install_allowed_roots` 后再启动
- `automation_upload_allowed_roots` 若已是 `["/"]` 或 `["/tmp"]`，同样无法启动，须手改配置
- `config.json` 权限会在下次保存或启动时收到 `0600`
- 若 `~/.hermes/plugins/onebot/__pycache__` 属 root，建议 `sudo rm -rf` 后再装插件，以免网关读到旧字节码

### 文档
- README / REST API / WebUI 连接页补充安装白名单、分端口 bind、反向 WS `access_token`、重装保留工具集与升级步骤

## [1.7.0] - 2026-09-01

### 新增
- 私聊被拒回复：`dm_reject_reply_enabled`（默认关闭，静默丢弃）开启后向对方回复 `dm_reject_message`（默认 `⛔ 当前私聊策略为：{reason}`）
  - `{reason}`：禁止私聊模式和私聊黑名单 →「禁止私聊」；仅限好友且非好友 →「仅限好友」
  - WebUI「聊天配置 → 私聊设置」可开关并编辑文案

### 变更
- 私聊准入不再用黑/白名单模式二选一，改为三种模式 + 常驻名单：
  - `dm_policy`：`allow`（允许私聊）/ `deny`（禁止私聊，默认）/ `friends`（仅限好友）
  - `dm_blacklist`：无论何种模式都禁止
  - `dm_whitelist`：无论是否好友、是否禁止私聊都允许，但不能覆盖 bot 动态黑名单
  - 同一用户同时在黑白名单时，黑名单优先
  - 「仅限好友」通过 OneBot `get_friend_list` 判断（有缓存）

### 升级说明
- 无需手动迁移配置；加载时把旧 `dm_user_filter_mode` / `dm_user_list` 映射到新字段，下次保存写出
  - 旧 `whitelist` → `deny` + 白名单
  - 旧 `blacklist` → `allow` + 黑名单
- 默认仍拒绝所有私聊（与旧「白名单空」一致）；需要全员可私聊改为「允许私聊」，需要仅好友改为「仅限好友」，需要被拒时提示则开启「私聊被拒时回复」
- **不必**重装 Hermes 插件；升级并重启适配器服务即可（配置热加载后也会生效）

### 文档
- README / REST API / WebUI 同步私聊三模式、常驻名单与被拒回复

## [1.6.1] - 2026-09-01

### 修复
- shared 群聊排队：同一发送者在 busy 期间跟进、被 Hermes `redirect`/`steer` 吞进当前 turn 时，适配器不再额外等待第二帧 idle，避免任务已结束后整队持续排队（日志特征 `inflight remaining=1 — not dequeuing`）
- 插件仅在 Hermes session 没有 pending/debounce 后续时发送 idle，与网关「一轮可能吞多条跟进」的语义对齐
- `/clean` 同时释放该群 busy 槽，下一则消息可立即处理（不再只清队列、留下幽灵 busy，现场不必重启适配器）
- `/stop` `/new` `/reset` 的 3 秒延迟清理按 busy 代数识别槽位，bot 发送刷新时间戳不会再取消强制出队

### 变更
- 群聊 idle 表示 Hermes session 已空闲，不再按直推次数凑齐 inflight

### 升级说明
- 无需手动迁移配置
- **必须**重新安装随包提供的 Hermes 插件并重启 Hermes 网关；只升级适配器服务、不重装插件时，上述 idle 修复不会生效
- 升级后若群聊仍在排队，发 `/clean` 即可释放 busy；`/stop` 在 Hermes 已空闲时解不开适配器侧卡死

### 文档
- README / REST API / WebUI 聊天配置补充 session 级 idle、`/clean` 解堵与升级必须重装插件的说明

## [1.6.0] - 2026-08-31

### 新增
- 出站消息正则过滤：可在 WebUI「聊天配置」添加 Python 正则，Hermes 发往 OneBot 的文本（`send_text`、媒体/文件说明、`onebot_send_message` 的 text 段）命中后直接丢弃；群配置可覆盖开关和规则列表
- 解析器对静默丢弃引入 `DroppedEvent`（准入、未 @bot、空消息等），与会回复拒绝提示的 `FilteredEvent` 分开；入站未进 Hermes 的候选消息在 DEBUG 记 `丢弃 -- reason=`（`user_filter` / `mention` / `command` / `blacklist` / `rate_limit` / `empty`），不含正文
- WebUI 日志页可查看 `adapter.log` 尾部并下载当前文件；内存视图标明仅保留最近 500 条
- `PATCH /api/v1/config` 写入 `webui_token` 时校验长度至少 8 个字符

### 变更
- shared 群聊排队：当前任务进行中、队列为空且新消息发送者与当前任务相同的，不再入队，直接转发给 Hermes；同一发送者直推会增加 inflight，避免第一条 idle 提前放出下一个人
- Hermes 插件在 `on_processing_complete` 发送 idle（不再依赖会被 gateway 直接 pop 的 `register_post_delivery_callback`）
- WebUI 窄屏布局：顶栏改为汉堡菜单、禁止横向撑开页面；两列表单在约 700px 以下收成单列；宽表在容器内横向滚动；聊天/指令/连接/高级页保存按钮吸底；群编辑弹窗在手机上贴底全宽
- 收发预览改走独立 `onebot_adapter.onebot.message_preview` logger（不进文件）；真实 OneBot 发送（含 `api_call` 发送类、拒绝回复、文件上传成功后、自动化工具）统一打 `发送 ->`
- 插件 DEBUG 诊断复用 `logging_utils` 脱敏，不再输出完整消息正文

### 修复
- 添加群时若输入已有群号，群号输入框会被误禁用且无法改回
- 群编辑「触发关键词模式」用空数组做 option 值，已有自定义关键词时下拉选不中「自定义」
- 指令过滤统计按权限表条目计数，可能出现「所有人可用」为负数；搜索在 description/aliases 缺失时会抛错
- WebUI 登录比较对异常长度或非 ASCII 输入不再抛异常
- 卸载插件时保留 Hermes `.env` 中带引号的其他环境变量值

### 升级说明
- 无需手动迁移配置
- 升级适配器后需重新安装随包提供的 Hermes 插件并重启 Hermes 网关，确保 idle 信号与媒体策略与服务端一致

### 文档
- 补充文档索引；校正 README / REST API / AGENTS 中的排队 idle、DroppedEvent、日志端点、出站过滤、媒体投递与 notice 事件说明

## [1.5.0] - 2026-08-18

### 新增
- 全局、群聊和用户限流额度持久化到 `rate_limit.sqlite3`，支持跨重启恢复、数据库故障降级/拒绝策略，以及 WebUI/API 查询和定向重置额度

### 变更
- OneBot 正向/反向 WebSocket 的普通事件改用单 worker、有界 1024 帧 FIFO 顺序处理；API 响应仍在接收循环中即时关联，避免事件处理等待同一连接上的 API 响应时死锁，并防止突发流量创建无界任务
- Hermes 插件连接的并发帧处理增加 64 帧上限和背压，避免慢请求期间无限积累后台任务
- 使用统计的 SQLite 操作移至工作线程，降低数据库读写阻塞 asyncio 事件循环的风险
- Ruff 增加复杂度、分支数和语句数检查，约束后续解析器与分发器继续膨胀

### 修复
- shared 群聊中，绕过适配器队列的 `/` 指令不再注册 idle 回调，避免指令处理结束后误清除其他普通消息持有的 busy 状态并提前派发下一条消息

### 升级说明
- 无需手动迁移配置；首次启动会在配置文件旁自动创建 `rate_limit.sqlite3`
- 升级适配器后需重新安装随包提供的 Hermes 插件并重启 Hermes 网关，确保服务与插件版本一致

### 文档
- 补充 README 的持久化限流、额度管理和升级流程，完善 REST API 的额度查询/重置响应及错误语义

## [1.4.0] - 2026-08-10

### 新增
- 新增 `file_upload_timeout` 配置（默认 600 秒，范围 30–600 秒），群聊、私聊和闪传文件上传统一使用并支持 WebUI 热更新；Hermes 插件的 RPC 等待上限随配置自动调整
- OneBot 工具目录扩展到 100 项，新增精华与公告、签到、群相册与待办、好友和账号资料、收藏、表情回应查询、完整群文件管理、闪传与文件集、群系统消息、群荣誉及群加群选项能力
- 新增 WebUI「OneBot 工具」页面和管理 API，可逐项控制工具是否注册给 Hermes 及 `everyone` / `admin` 权限；策略不影响全权限自动化 API

### 变更
- **闪传与文件集 8 个工具默认对 Hermes 隐藏**：闪传依赖 PC 版 QQ 端能力，仅 Windows 版客户端可用（Linux 等其他平台运行 NapCat 时不可用），且涉及本机文件读取/上传；需要时可在 WebUI「OneBot 工具」页显式启用，HTTP 自动化 API 不受策略影响仍包含完整目录
- Hermes 工具管理员权限区分全局管理员和群管理员；群管理员只能操作当前群，跨群和账号级工具仅允许全局管理员调用
- 发布工作流在构建和上传 PyPI 前执行 Ruff、独立环境测试与 `twine check`，并显示可选 Hermes 协议测试的跳过原因；前端依赖改用 `npm ci` 可复现安装
- 前端构建链升级到 Vite 8 与 `@vitejs/plugin-vue` 6，清除发布前依赖审计发现的已知漏洞
- Python 包许可证元数据改用 SPDX 表达式，消除 setuptools 构建弃用警告

### 修复
- 跨事件循环返回 OneBot RPC 结果时使用线程安全唤醒，避免工具实际完成后仍等到超时计时器触发才返回
- 群文件上传等待 NapCat 响应超时后，自动轮询群历史；仅在时间、发送账号、文件名及可用时文件大小唯一匹配时确认成功，歧义或无结果时返回不可自动重试的“结果未知”，避免 NapCat 已上传但未及时回调导致工具挂起或重复上传
- OneBot API 错误兼容读取 NapCat 的 `message` / `wording` 字段，不再把 `retcode=1200` 的真实错误显示为 `msg=None`

### 文档
- 新增 NapCat `v4.18.13` 群文件完成回调临时诊断补丁、构建产物校验和复现/恢复流程
- 全面校正 README、REST API、WebUI 帮助文本和维护者指南中的架构、路径、工具数量、队列与媒体行为

## [1.3.0] - 2026-08-03

### 新增
- 新增自动化 OneBot 工具 API：`GET /api/v1/tools` 返回工具目录，41 个 `POST /api/v1/tools/<tool_name>` 路由覆盖完整工具目录
- 新增独立自动化 API key，可通过 WebUI 或 `--generate-api-key` / `--rotate-api-key` / `--revoke-api-key` 管理；配置仅持久化 SHA-256 摘要
- 新增 `/api/v1/openapi.json`、Pydantic 严格参数校验、本地文件允许目录及路径/符号链接逃逸防护
- 新增 `onebot_upload_file`，统一支持群文件和私聊文件上传；上传类 OneBot 调用使用独立的长超时

### 变更
- **破坏性变更**：WebUI 管理 API 全部迁移到 `/api/v1/*`；配置更新由 `PUT /api/config` 改为 `PATCH /api/v1/config`，旧业务路径不再保留
- WebUI session 和自动化 API key 分权；凭证只接受 `Authorization: Bearer`，不再接受 `?token=`
- 自动化 API 默认关闭；API key 拥有全部 OneBot 工具权限，包括群管理和账号管理操作
- Hermes 插件与 HTTP 自动化 API 共用同一份工具目录和处理器，工具成功/失败响应语义保持一致
- 消息发送、合并转发和文件上传会严格校验群聊/私聊目标；HTTP 调用必须显式传入匹配的 `group_id` 或 `user_id`，插件调用仅继承同类型的当前会话
- 收紧有依赖关系的工具参数：标记已读须在 `real_seq` 与 `all=true` 中二选一，开关、时长和可清空文本等参数改为显式必填

### 修复
- 未知 `/api/*` 不再回退到 SPA，而是返回 JSON 404
- 群配置写入改为磁盘保存成功后再更新内存，避免保存失败造成状态分叉
- OneBot API 将 `status=failed` 视为失败并向工具调用方返回错误；未连接 OneBot 时自动化工具 API 返回 `503`
- RPC 发送失败、超时或取消后清理 pending future，避免长期运行时泄漏；文件上传不再被普通请求的短超时提前中断

### 文档
- 更新 README、REST API 文档和 AGENTS 架构说明，补充 v1 路径、key 生命周期、工具发现和文件安全策略

## [1.2.0] - 2026-07-24

### 新增
- **使用统计**：记录通过准入与指令过滤的消息元数据（不含正文/媒体），持久化到 `~/.onebot_adapter/usage_stats.sqlite3`，默认保留 365 天。新增 `GET /api/usage/stats`、`GET /api/usage/dimensions`、`DELETE /api/usage` 端点；WebUI 仪表盘展示趋势/活跃群/活跃用户图表，可按时间、范围（全部/私聊/群聊）、群、用户过滤。配置项 `usage_stats_enabled`（默认 true）/`usage_stats_retention_days`（默认 365）在「高级设置」页管理
- **入站消息限流**：全局、群聊、个人三维度同时检查，支持滑动窗口（`sliding_window`）与令牌桶（`token_bucket`）两种算法，命中任一维度即回复原消息并拦截。全局管理员与对应群管理员豁免；个人计数在私聊和所有群之间共享；限额 0 表示禁用该维度。新增 `rate_limit_enabled` 及 `global/group/user_rate_limit_(algorithm|messages|window_seconds)`、`rate_limit_reject_message` 配置；GroupConfig 可 per-group 覆盖群聊维度。WebUI「聊天配置」页配置
- **Bot 动态用户黑名单**：默认开启，新增 `onebot_get_bot_blacklist` / `onebot_edit_bot_blacklist` 工具，支持群聊、私聊和全局临时拉黑，记录原因、发起用户与到期时间；管理员自动豁免。记录独立持久化到 `~/.onebot_adapter/bot_blacklist.sqlite3`，WebUI「聊天配置」页可配置 `bot_blacklist_enabled`/`bot_blacklist_max_duration_seconds`/`bot_blacklist_reject_message`、查看并人工解除记录
- **配置备份与审计**：`save_config` 写入前自动备份为 `config.json.bak.<ts>`，保留最近 5 个；每次保存追加一条 JSON 审计记录到 `~/.onebot_adapter/logs/config-audit.log`（按日轮转，保留 365 天），记录改动字段、来源、操作者、客户端 IP 及指纹；检测到疑似重置（≥5 字段回退默认或群配置清空）时额外告警
- **可靠事件投递**：Hermes 事件新增 delivery ID/ack；独立 cron 发送使用 `role=rpc` WS，不再参与事件重放或群聊队列状态
- **日志隐私与审计**：新增文件消息正文 `none`/`preview`/`full` 策略（默认 `preview`）和单文件大小上限 `log_file_max_bytes`（默认 10 MiB，达到即轮转）；记录 WebUI 登录/鉴权失败、WS 鉴权失败、统计清空、动态黑名单及 Hermes 配置修改审计事件
- **插件状态上报**：Hermes 插件可向适配器上报处理异常，WebUI 仪表盘显示最近一次插件错误摘要

### 变更
- 配置文件损坏或字段非法时启动 fail-fast（`ConfigLoadError`），不再回退默认配置并覆盖原文件
- WebUI Dashboard 与 ECharts 拆分为懒加载 chunk，降低首屏包体积
- 移除 `platform_hint` 旧字段迁移及无生产调用的协议/缓存辅助接口（`MediaItem.from_dict`、`CommandInfo` 等）

### 修复
- 修复 ring buffer 重连重复投递、队列合并污染缓冲事件及全客户端发送失败仍报告成功
- 修复配置热更新乱序、插件连接失败 session 泄漏、self-id 探测任务泄漏和工具管理员上下文串线
- 修复 per-group lock 长期增长及陈旧 OneBot HTTP API 文档
- 修复完整消息日志向 root 传播，导致控制台/WebUI 同时出现截断版和完整版并泄露正文的问题

### 文档
- `docs/api.md` 补全 `/api/usage/*`、`/api/bot_blacklist` 端点；Config 字段表补全 `usage_stats_*`/`rate_limit_*`/`bot_blacklist_*`/`log_file_message_mode`/`log_file_max_bytes`；GroupConfig 字段表补 `group_rate_limit_*`
- README 补充使用统计、入站消息限流、配置备份与审计、动态黑名单说明

## [1.1.0b] - 2026-07-17

### 新增
- **媒体投递模式**：新增 `media_delivery_mode` 配置字段（默认 `cache`）。`cache` 模式下适配器在 `NormalizedEvent.media_items` 中携带媒体条目，插件侧用 `cache_image_from_url`/`cache_audio_from_url`/`cache_video_from_bytes`/`cache_document_from_bytes` 下载到 `~/.hermes/cache/` 并填充 `MessageEvent.media_urls` 为本地路径；`passthrough` 模式下媒体 URL 以占位符形式内联在文本中，`media_items` 为空。热重载 `media_delivery_mode` 时向已连接插件广播新的 `ready` 帧，无需重连即可切换策略
- **notice 事件推送**：戳一戳（bot 被戳，含私聊）和群成员进退群（区分主动退群 `leave` / 被踢 `kick`）合成中文系统提示转发给 agent。新增配置项 `notify_poke_enabled` / `notify_member_change_enabled`（全局开关，GroupConfig 可 per-group 覆盖，默认关闭）。`NormalizedEvent.is_system_notice` 字段标记合成系统事件，插件侧据此设 `MessageEvent.internal=True` 绕过 Hermes 文本去抖
- **Hermes 会话隔离模式 API**：新增 `GET /api/hermes_mode`、`PUT /api/hermes_mode`、`POST /api/hermes_mode/refresh` 端点。读 `group_sessions_per_user` 优先取插件上报值，插件未连接时回退读 Hermes `config.yaml`；写入后需重启 Hermes 网关生效
- **版本更新检查**：新增 `GET /api/update_check` 端点，查询 GitHub tags API 比较当前版本，结果缓存 1 小时（错误 5 分钟）。WebUI 仪表盘显示更新提示
- **WebUI 鉴权强化**：登录改为签名 session token 机制（HMAC-SHA256 + epoch）。原始 `webui_token` 仅用于 `POST /api/login`，不可直接调用其他 API；登录有效期由 `webui_token_lifetime_hours` 控制（默认 7 天），修改后通过 bump `webui_token_epoch` 使所有已签名 token 立即失效。`/api/login` 按客户端 IP 限流（5 次失败后封禁 15 分钟），`webui_trust_proxy_headers` 控制是否信任 `X-Forwarded-For`（仅反向代理开启）
- **插件路径安全检查**：`installer.install`/`uninstall` 新增 `_is_safe_install_path` 校验，仅允许写入 `$HOME`、`/home`、`/tmp` 下，拒绝系统路径；复制时拒绝符号链接目标防 TOCTOU 攻击
- **插件版本不匹配检测**：`/api/status` 返回 `plugin_version` 和 `version_mismatch`，WebUI 仪表盘显示版本告警
- **工具扩充**：OneBot API 工具从 28 个增至 38 个，新增 `onebot_get_file`、`onebot_get_recent_contact`、`onebot_send_like`、`onebot_get_friends_with_category`、`onebot_get_profile_like`、`onebot_fetch_custom_face`、`onebot_forward_single_msg`、`onebot_set_group_special_title`、`onebot_set_online_status`、`onebot_set_signature`、`onebot_set_avatar`、`onebot_delete_friend` 等
- **共享 OneBot 处理管道**：新增 `onebot_adapter/onebot/handler.py`（`OneBotHandler`），将 WS-API 响应拦截、SeqMap 写入、`parse_event` 调用、`FilteredEvent` 派发抽到共享类，`ws_reverse`/`ws_forward` 各构造一个并委托 `handle_text(raw)`，消除重复解析逻辑
- **发送去重兜底**：新增 `send_dedup_enabled` / `send_dedup_ttl_seconds`（默认 10s），防 Gateway `send_text` 超时重试导致重复发送
- **异步工具助手**：新增 `onebot_adapter/_async_utils.py`（`log_task_exception` 等），用于 `ConfigStore.update` 等 `create_task` 调用点统一捕获后台任务异常
- **表情回应配置**：新增 `reaction_emoji_enabled` / `reaction_emoji_id`（默认 124）/ `reaction_emoji_id_queued`（默认 123，排队时贴的表情，空=不贴）；GroupConfig 可 per-group 覆盖 `reaction_emoji_enabled`
- **配置热重载扩展**：`AdapterService._on_config_change` 接入更多字段，`update_config` 在 `media_delivery_mode` 变更时广播新 `ready` 帧

### 变更
- `NameResolver` 失败时清理 lookup 锁，避免瞬时错误永久阻塞后续重试
- `WsApiTransport` 日志处理 JSON 序列化错误，避免日志本身抛异常
- `check_for_updates` 错误结果使用 5 分钟短 TTL 缓存，使临时故障更快重试
- `group_strip_first_mention` 语义：消息以 @bot 开头时移除该段（非首 @bot 保留）
- 平台提示词更新：明确不支持 @ 人（需用 `onebot_send_message` 工具传消息段数组）、clarify 工具集在 OneBot 平台禁用建议、群内序号说明、合并转发格式说明
- `installer.install` 返回字段从 `files_copied`/`config_updated` 改为 `copied`（数组）/ `env_vars`/ `note`，并在末尾自动写入默认工具集配置
- `GET /api/config` 响应剔除 `webui_token` 和 `webui_token_epoch`（口令与内部状态不通过 API 暴露）；`PUT /api/config` 拒绝客户端覆盖 `webui_token_epoch`
- `message_show_group_id` 和 `reaction_emoji_enabled` 默认值改为 `true`
- `seq_map_add` 替换内部函数 `_seq_map_add` 命名更清晰；`log_send_line` 日志改进

### 修复
- SeqMap 更新改为 fire-and-forget，避免阻塞导致重复发送
- 环形缓冲区重放处理损坏条目并确保 WebSocket 正常关闭
- 群聊忙碌状态时间戳刷新机制，防止误判超时
- 缓存强制驱逐测试用例覆盖

### 文档
- README 添加适配器服务徽章
- 平台提示词与发送说明更新（不支持 @ 人、合并转发示例修正）
- `docs/api.md` 补全 `/api/hermes_mode`、`/api/update_check` 端点；`/api/status` 补 `plugin_version`/`version_mismatch`/`hermes_group_sessions_per_user`；`/api/install_plugin` 响应字段更正为 `copied`/`env_vars`/`note`；Config 字段表删除已移除的 `group_auto_join`，补全 `media_delivery_mode`/`reaction_emoji_*`/`notify_*`/`send_dedup_*`/`webui_trust_proxy_headers` 等；GroupConfig 字段表补 `reaction_emoji_enabled`/`notify_poke_enabled`/`notify_member_change_enabled`

## [1.0.0b3] - 2026-07-07

### 新增
- 群聊消息排队机制：shared session 串行化，含 busy 槽 + FIFO 队列 + 看门狗超时兜底
- Hermes `mode_report` / `mode_refresh` 协议帧，支持动态会话隔离模式切换和热重载
- 版本检查 / 更新检测：前端仪表盘显示 GitHub 最新版本
- 插件版本不匹配检测，仪表盘版本告警
- 消息排队时贴表情回应 ID 配置

### 变更
- 重构 WebSocket 处理流程，改进多连接场景的事件分发
- 重构媒体处理：移除二进制帧支持，统一 JSON 负载，删除不再使用的媒体助手模块
- `handle_message` 改为后台任务执行，避免接收循环阻塞导致自死锁
- 并发发送请求限制，防止网关延迟引发重试风暴
- SeqMap 更新改为 fire-and-forget，避免阻塞导致重复发送
- 版本管理改为 `setuptools-scm` 动态生成，修复版本解析逻辑

### 修复
- 环形缓冲区重放增强鲁棒性，处理损坏条目并确保 WebSocket 正常关闭
- 群聊忙碌状态时间戳刷新机制，防止误判超时

### 文档
- README 补充适配器服务与 Hermes 插件关系说明，添加 API 文档链接

## [1.0.0b2] - 2026-07-03

### 变更
- `message_show_group_id` 和 `reaction_emoji_enabled` 默认值改为 `true`
- 触发关键词输入改为 tag 样式（回车添加），与私聊名单一致
- OneBot WS Token 字段新增显示/隐藏、复制、重新生成按钮
- 统一 OneBot 和 Hermes 的 token 标签为 "WS Token"
- 插件安装流程增加 `hermes plugins enable onebot-platform` 提示
- 平台提示词中合并转发示例移除错误的 QQ 号和序号

### 新增
- `MANIFEST.in` 确保 sdist 包含前端静态文件
- PyPI classifiers（Beta 状态、分类标签）和 project URLs

### 修复
- `.gitignore` 补充 IDE/OS/Vim 忽略规则

## [1.0.0b1] - 2026-07-02

首个公开测试版。

- OneBot 11 正向/反向 WebSocket 传输
- Hermes 插件桥接 + 工具集管理
- WebUI 仪表盘、连接管理、指令过滤
- 每群配置覆盖 + 群成员过滤
- /指令权限模型（everyone/admin/disabled）
- 28 个 OneBot API 工具暴露给 LLM
- ffmpeg 语音转码
- SeqMap: NapCat real_seq ↔ message_id 映射

[Unreleased]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.8.0...HEAD
[1.8.0]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.6.1...v1.7.0
[1.6.1]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.1.0b...v1.2.0
[1.1.0b]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.0.0b3...v1.1.0b
[1.0.0b3]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.0.0b2...v1.0.0b3
[1.0.0b2]: https://github.com/DNAlec/hermes-onebot-adapter/compare/v1.0.0b1...v1.0.0b2
[1.0.0b1]: https://github.com/DNAlec/hermes-onebot-adapter/releases/tag/v1.0.0b1

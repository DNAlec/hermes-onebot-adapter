# 更新日志

## [未发布]

### 新增
- **Bot 动态用户黑名单**：默认开启，新增 `onebot_get_bot_blacklist` / `onebot_edit_bot_blacklist` 工具，支持群聊、私聊和全局临时拉黑，记录原因、发起用户与到期时间；管理员自动豁免。记录独立持久化到 SQLite，WebUI 可配置最大时长和提示模板、查看并人工解除记录

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

# 运维

安装、启动与端口见根目录 [README.md](../README.md)。HTTP 路径与 Config 字段见 [api.md](api.md)。

- [自动化工具 API](#自动化工具-api)
- [环境变量](#环境变量)
- [使用统计](#使用统计)
- [入站消息限流](#入站消息限流)
- [配置备份与审计](#配置备份与审计)
- [日志](#日志)
- [配置文件](#配置文件)

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

## 使用统计

适配器记录通过准入与指令过滤的消息元数据（QQ 号、群号、时间、是否系统事件，**不含消息正文或媒体地址**）到 `~/.onebot_adapter/usage_stats.sqlite3`，默认保留 365 天。WebUI 仪表盘展示趋势、活跃群、活跃用户图表，可按时间、范围（全部/私聊/群聊）、群、用户过滤。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `usage_stats_enabled` | `true` | 统计开关；关闭后停止新增，已有历史仍可查询 |
| `usage_stats_retention_days` | `365` | 保留天数；缩短后保存配置会立即清理过期数据 |

数据也可通过 REST API 查询/清空（见 [API 文档](api.md)）。

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

## 日志

控制台和 WebUI 内存视图走独立的 `onebot_adapter.onebot.message_preview` logger（不写入文件）。文件日志由 `log_file_message_mode` 控制（`none` / `preview` / `full`，默认 `preview`），达到 `log_file_max_bytes`（默认 10 MiB）后轮转。未进入 Hermes 的候选消息只在 DEBUG 记 `丢弃 -- reason=`，不含正文。WebUI「日志」页可切换内存缓冲与 `adapter.log` 尾部，并下载当前文件。

## 配置文件

适配器服务配置持久化于 `~/.onebot_adapter/config.json`（或 `ONEBOT_ADAPTER_CONFIG` 指定路径），WebUI 修改即保存。完整字段见 [REST API 文档](api.md#config-字段)。损坏或非法的配置文件会使服务 fail-fast，不会覆盖原文件。

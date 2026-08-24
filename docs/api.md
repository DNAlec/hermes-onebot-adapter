# Hermes OneBot Adapter — REST API 文档

基础地址: `http://<host>:18820`（默认端口，可在配置中修改 `webui_port`）

---

## 鉴权

管理端点使用 WebUI session；`/api/v1/tools*` 使用独立的自动化 API key。
`/api/v1/health`、`/api/v1/auth/login` 和 `/api/v1/openapi.json` 无需鉴权。

适配器采用**签名 session token** 机制：原始 `webui_token`（首次启动时自动生成并打印到日志，也可在 `~/.onebot_adapter/config.json` 的 `webui_token` 字段查看）只能用于登录，**不能直接用于其他 API 调用**。登录成功后服务端返回一个带有效期的 HMAC 签名 token，后续请求使用该签名 token。

### 登录流程

**`POST /api/v1/auth/login`**（无需鉴权，但有失败次数限制，见下文）

请求体：
```json
{"token": "<原始 webui_token>"}
```

成功响应 `200`：
```json
{
  "session_token": "<HMAC 签名 token>",
  "expires_in": 604800
}
```

`expires_in` 单位为秒，等于配置项 `webui_token_lifetime_hours * 3600`（默认 168 小时 = 7 天，最小 1 小时）。

错误响应：
- `400` — 请求体非合法 JSON：`{"error": "invalid JSON"}`
- `401` — token 错误：`{"error": "invalid token"}`
- `429` — 该 IP 登录失败次数过多，已临时封禁：`{"error": "too many attempts", "retry_after": <秒>}`

### 后续 API 调用

拿到 `session_token` 后，仅通过 Authorization header 传递：
`Authorization: Bearer <session_token>`。URL Query 不接受 token，避免凭证进入访问日志和浏览器历史。

无 token、token 错误、签名无效或 token 过期均返回 `401`：
```json
{"error": "unauthorized"}
```

### 示例

**Python（requests）**：
```python
import requests

base = "http://host:18820"
# 1. 用原始 token 登录
r = requests.post(f"{base}/api/v1/auth/login", json={"token": "原始webui_token"})
session = r.json()["session_token"]
# 2. 后续调用用签名 token
r = requests.get(f"{base}/api/v1/status",
                 headers={"Authorization": f"Bearer {session}"})
print(r.json())
```

**curl**：
```bash
# 1. 登录拿 session_token
SESSION=$(curl -s -X POST http://host:18820/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"token":"原始webui_token"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['session_token'])")
# 2. 用签名 token 调 API
curl -H "Authorization: Bearer $SESSION" http://host:18820/api/v1/status
```

### 登录失败次数限制（防爆破）

`/api/v1/auth/login` 按客户端 IP 计数：同一 IP 累计 **5 次**登录失败后，封禁该 IP **15 分钟**，期间任何登录尝试直接返回 `429`（不再执行 token 校验）。封禁期间其他已持有有效签名 token 的 API 调用不受影响。封禁到期自动解封；登录成功会立即清零该 IP 的失败计数。计数状态仅在进程内存中，**重启适配器即清空**。

### 修改有效期

在 WebUI 高级设置页修改 `webui_token_lifetime_hours` 后保存，所有已签发的签名 token 立即失效（包括当前会话），需要重新登录。这是通过内部 `webui_token_epoch` 字段递增实现的，无需手动操作。

### 自动化 API key

工具 API 不接受 WebUI session，管理 API 也不接受自动化 key。自动化 key 拥有全部 OneBot 工具权限，包括踢人、禁言、退群、删好友和修改机器人资料。

生成并启用：

```bash
hermes-onebot-adapter --generate-api-key --enable-api
```

CLI 还提供：

- `--rotate-api-key`：替换已有 key，原 key 立即失效；新 key 仅显示一次
- `--revoke-api-key`：清除摘要并关闭自动化 API
- `--enable-api` / `--disable-api`：启停已有 key 对应的工具 API

WebUI session 可调用以下 key 管理接口（均不接收请求体）：

**`POST /api/v1/automation/key`**：生成或轮换 key，响应 `{"api_key":"hoa_...","shown_once":true}`。已有 key 会立即失效。

**`DELETE /api/v1/automation/key`**：撤销 key 并关闭自动化 API，响应 `{"revoked":true}`。

服务端仅保存 key 的 SHA-256 摘要；`GET /api/v1/config` 只返回派生字段 `automation_api_key_configured`，不会返回原始 key 或摘要。

---

## 端点

### 1. 健康检查（无需鉴权）

**`GET /api/v1/health`**

响应 `200`：
```json
{"status": "ok"}
```

### 2. 登录（无需鉴权，受失败次数限制）

见上方「鉴权 → 登录流程」章节。

---

### 3. 服务状态

**`GET /api/v1/status`**

响应 `200`：
```json
{
  "adapter_version": "x.y.z",
  "plugin_version": "x.y.z",
  "version_mismatch": false,
  "latest_plugin_status": null,
  "onebot_connected": true,
  "hermes_plugin_connected": true,
  "onebot_mode": "reverse",
  "self_id": "123456",
  "onebot_ws_port": 18800,
  "hermes_ws_port": 18810,
  "webui_port": 18820,
  "hermes_group_sessions_per_user": true
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `adapter_version` | string | 适配器版本号 |
| `plugin_version` | string\|null | Hermes 插件版本号（插件未连接时为 `null`） |
| `version_mismatch` | bool | 插件与适配器版本是否不匹配（插件未连接时为 `true`） |
| `latest_plugin_status` | object\|null | 插件最近一次状态/错误摘要；断开后为 `null` |
| `onebot_connected` | bool | OneBot 客户端是否已连接 |
| `hermes_plugin_connected` | bool | Hermes 插件是否已连接 |
| `onebot_mode` | string | OneBot 连接模式：`reverse`（被动等待）/ `forward`（主动连接） |
| `self_id` | string | 机器人 QQ 号 |
| `hermes_group_sessions_per_user` | bool | Hermes 会话隔离模式（插件上报；`true`=每人独立 session，`false`=全群共享 session，触发群聊排队） |

---

### 4. 配置管理

**`GET /api/v1/config`**

返回完整适配器配置。响应 `200`：

```json
{
  "onebot_mode": "reverse",
  "onebot_reverse_ws_port": 18800,
  "onebot_reverse_ws_path": "/onebot",
  "onebot_forward_ws_url": "ws://127.0.0.1:3001",
  "onebot_ws_token": "...",
  "self_id": "123456",
  "group_require_mention": true,
  "group_mention_first_only": false,
  "group_trigger_keywords": [],
  "group_keyword_first_only": false,
  "group_strip_first_mention": true,
  "global_admins": [],
  "dm_user_filter_mode": "whitelist",
  "dm_user_list": [],
  "groups": {},
  "global_channel_prompt": "...",
  "hermes_ws_port": 18810,
  "hermes_ws_path": "/hermes",
  "hermes_ws_token": "...",
  "hermes_install_dir": "",
  "webui_port": 18820,
  "webui_token_lifetime_hours": 168,
  "webui_trust_proxy_headers": false,
  "automation_api_enabled": false,
  "automation_api_key_configured": true,
  "automation_upload_allowed_roots": ["/tmp/hermes-onebot-adapter-uploads"],
  "log_level": "INFO",
  "log_message_preview": 100,
  "log_file_enabled": true,
  "log_file_dir": "",
  "log_file_message_mode": "preview",
  "log_file_max_bytes": 10485760,
  "log_retention_days": 3,
  "usage_stats_enabled": true,
  "usage_stats_retention_days": 365,
  "message_show_group_id": true,
  "seq_map_size": 4500,
  "reaction_emoji_enabled": true,
  "reaction_emoji_id": "124",
  "reaction_emoji_id_queued": "123",
  "file_upload_timeout": 600.0,
  "send_dedup_enabled": true,
  "send_dedup_ttl_seconds": 10.0,
  "event_queue_enabled": true,
  "event_queue_clear_on_session_reset": true,
  "event_queue_clean_command_enabled": true,
  "event_queue_max_per_chat": 50,
  "event_queue_idle_timeout": 300.0,
  "rate_limit_enabled": false,
  "global_rate_limit_algorithm": "sliding_window",
  "global_rate_limit_messages": 0,
  "global_rate_limit_window_seconds": 0.0,
  "group_rate_limit_algorithm": "sliding_window",
  "group_rate_limit_messages": 0,
  "group_rate_limit_window_seconds": 0.0,
  "user_rate_limit_algorithm": "sliding_window",
  "user_rate_limit_messages": 0,
  "user_rate_limit_window_seconds": 0.0,
  "rate_limit_reject_message": "⛔ 消息发送过于频繁，请在 {retry_after} 秒后重试",
  "rate_limit_storage_failure_mode": "memory_fallback",
  "bot_blacklist_enabled": true,
  "bot_blacklist_max_duration_seconds": 86400,
  "bot_blacklist_reject_message": "⛔ 你已被 bot 暂时拉黑，剩余时间：{remaining}。原因：{reason}",
  "media_delivery_mode": "cache",
  "command_filter_enabled": false,
  "command_filter_unknown": false,
  "command_permissions": {},
  "command_reject_message": "⛔ 你没有权限使用此指令 /{cmd}",
  "outbound_filter_enabled": false,
  "outbound_filter_patterns": [],
  "notify_poke_enabled": false,
  "notify_member_change_enabled": false
}
```

> 注：`webui_token`、`webui_token_epoch` 和 `automation_api_key_hash` 不会出现在响应中；`automation_api_key_configured` 是只读派生字段。原始 WebUI token 仅可通过 `POST /api/v1/auth/login` 验证，自动化 API key 也不会被再次返回。

完整字段说明见 [Config 字段表](#config-字段)。

---

**`PATCH /api/v1/config`**

部分更新配置，只传需要修改的字段。Body 为 JSON 对象，包含要更新的键值对。

请求 `200`：
```json
{"log_level": "DEBUG"}
```

响应 `200` — 返回更新后的完整配置（与 `GET /api/v1/config` 同结构）。

响应 `400` — 校验失败：
```json
{"error": "onebot_mode must be one of ['forward', 'reverse']"}
```

---

### 5. Hermes 安装目录状态

**`GET /api/v1/hermes_dir_status`**

响应 `200`：
```json
{
  "hermes_dir": "/home/user/.hermes",
  "exists": true
}
```

---

### 6. 插件管理

**`POST /api/v1/install_plugin`**

将 OneBot 插件安装到 Hermes。Body（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `hermes_install_dir` | string | 否 | Hermes 安装目录；留空使用配置中的值 |

响应 `200`：
```json
{
  "adapter_version": "x.y.z",
  "hermes_dir": "/home/user/.hermes",
  "plugin_dest": "/home/user/.hermes/plugins/onebot/",
  "source": "/path/to/onebot_adapter/hermes_plugin",
  "copied": ["__init__.py", "adapter.py", "markdown.py", "onebot_tools.py", "plugin.yaml"],
  "env_vars": {
    "ONEBOT_ADAPTER_URL": "ws://127.0.0.1:18810/hermes",
    "ONEBOT_ADAPTER_TOKEN": "..."
  },
  "note": "Plugin installed to ... Restart the Hermes gateway for changes to take effect. 已为 OneBot 平台启用默认工具集;请运行 hermes plugins enable onebot-platform 并重启 Hermes 网关后生效。"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `adapter_version` | string | 适配器版本号 |
| `hermes_dir` | string | 解析后的 Hermes 安装目录 |
| `plugin_dest` | string | 插件复制目标目录 |
| `source` | string | 插件源目录（随包发行） |
| `copied` | string[] | 实际复制的文件名列表 |
| `env_vars` | object | 写入 Hermes `.env` 的环境变量（`ONEBOT_ADAPTER_URL` / `ONEBOT_ADAPTER_TOKEN`） |
| `note` | string | 安装结果提示（含工具集初始化结果） |

响应 `200`（安装路径不安全时）：
```json
{
  "adapter_version": "x.y.z",
  "hermes_dir": "/etc",
  "error": "install_dir resolved to /etc, which is outside $HOME"
}
```

---

**`POST /api/v1/uninstall_plugin`**

从 Hermes 卸载 OneBot 插件。Body 同上。

响应 `200`：
```json
{
  "adapter_version": "x.y.z",
  "hermes_dir": "/home/user/.hermes",
  "plugin_dest": "/home/user/.hermes/plugins/onebot/",
  "removed": true,
  "env_cleaned": true,
  "note": "Plugin removed from ... Env vars cleaned. Restart the Hermes gateway."
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `removed` | bool | 插件目录是否已删除 |
| `env_cleaned` | bool | 是否清理了 `.env` 中的相关变量 |
| `note` | string | 卸载结果提示 |

---

### 7. 自动化工具 API

自动化 API 默认关闭，使用 WebUI 或 CLI 生成独立 API key 并启用后调用。该 key 拥有全部 OneBot 工具权限。

> **闪传与文件集工具（仅 Windows）**：8 个闪传工具（`onebot_create_flash_task`、`onebot_send_flash_msg`、`onebot_get_share_link`、`onebot_get_fileset_id`、`onebot_get_fileset_info`、`onebot_get_flash_file_list`、`onebot_get_flash_file_url`、`onebot_download_fileset`）依赖 PC 版 QQ 端能力，只有 Windows 版客户端可用，在 Linux 等其他平台运行 NapCat 时调用会失败。它们对 Hermes 默认隐藏，但 HTTP 自动化 API 始终包含完整目录；本地文件路径需位于 `automation_upload_allowed_roots` 内。

- `GET /api/v1/tools`：返回全部工具及参数 JSON Schema。
- `POST /api/v1/tools/{tool_name}`：调用指定工具。
**`GET /api/v1/openapi.json`**：返回无需鉴权即可读取的 OpenAPI 3.1 契约。

工具接口只接受自动化 key：

```http
Authorization: Bearer hoa_xxx
```

通用成功响应：

```json
{"ok": true, "data": {}}
```

常见错误：

- `400 validation_error`：JSON 参数与工具 schema 不匹配
- `401 unauthorized`：key 缺失或错误
- `403 automation_api_disabled`：自动化 API 未启用
- `403 file_not_allowed`：本地文件不在允许根目录，或 URL scheme 不受支持
- `503 onebot_unavailable`：OneBot WS 尚未连接
- `500 tool_call_failed`：工具处理器或 OneBot action 调用失败；详细异常仅记录在服务端日志

`onebot_upload_file`、`onebot_set_avatar`、群头像/相册上传、群公告图片以及消息段/转发节点中的文件引用都会经过相同安全检查。本地路径必须是允许根目录内的绝对普通文件；会解析 `..` 和符号链接后再判断。远程引用只接受 `http`/`https`。

HTTP 工具调用没有 Hermes 当前聊天上下文，因此 `onebot_send_message`、`onebot_send_forward_msg` 和 `onebot_upload_file` 必须显式提供目标：`message_type=group` 时只传 `group_id`，`message_type=private` 时只传 `user_id`。目标缺失、类型冲突或同时传入两个 ID 均返回 `400 validation_error`。`onebot_mark_msg_as_read` 同样要求在正整数 `real_seq` 与 `all=true` 中二选一。

部分消息工具接受 `real_seq`。Hermes 内部调用会自动携带当前聊天上下文，以便适配器按群号或用户 ID 查询 SeqMap；HTTP 调用不继承该上下文，schema 提供 `group_id`/`user_id` 时应显式传入以查询映射，否则会按兼容规则把 `real_seq` 直接作为 `message_id` 传给 OneBot。自动化脚本若需要稳定定位历史消息，应优先使用历史接口返回的实际 `message_id`。

例如发送群消息：

```http
POST /api/v1/tools/onebot_send_message
Authorization: Bearer hoa_xxx
Content-Type: application/json

{"message_type":"group","group_id":"123","message":[{"type":"text","data":{"text":"hello"}}]}
```

上传群文件：

```http
POST /api/v1/tools/onebot_upload_file
Authorization: Bearer hoa_xxx
Content-Type: application/json

{"message_type":"group","group_id":123,"file":"/tmp/hermes-onebot-adapter-uploads/done.zip","name":"done.zip"}
```

---

### 8. 日志

**`GET /api/v1/logs`**

返回服务端内存环形缓冲区中的最近日志行（默认最多 500 条，进程重启即空）。完整记录在文件日志中。

响应 `200`：
```json
{
  "logs": [
    "2025-01-01 12:00:00 INFO onebot_adapter.app: Service starting...",
    "2025-01-01 12:00:01 INFO onebot_adapter.app: OneBot connected"
  ],
  "source": "memory",
  "memory_limit": 500,
  "file_enabled": true,
  "file_available": true,
  "file_path": "/home/user/.onebot_adapter/logs/adapter.log",
  "file_size": 1234
}
```

**`GET /api/v1/logs/file`**

读取当前 `adapter.log` 尾部。查询参数 `lines`（默认 1000，最大 5000）。文件未启用或尚不存在时 `logs` 为空且 `file_available=false`。

**`GET /api/v1/logs/file/download`**

下载当前 `adapter.log`（`Content-Disposition: attachment`）。文件不可用时返回 `404`。

---

### 9. 群组管理

**`GET /api/v1/groups`**

返回所有已配置的群组列表。

响应 `200`：
```json
{
  "groups": [
    {
      "group_id": "123456789",
      "name": "测试群",
      "enabled": true,
      "require_mention": null,
      "mention_first_only": null,
      "trigger_keywords": null,
      "keyword_first_only": null,
      "strip_first_mention": null,
      "custom_prompt": "",
      "admins": [],
      "group_user_filter_mode": "blacklist",
      "group_user_list": [],
      "message_show_group_id": null,
      "reaction_emoji_enabled": null,
      "command_filter_enabled": null,
      "command_filter_unknown": null,
      "command_permissions": null,
      "outbound_filter_enabled": null,
      "outbound_filter_patterns": null,
      "notify_poke_enabled": null,
      "notify_member_change_enabled": null,
      "group_rate_limit_algorithm": null,
      "group_rate_limit_messages": null,
      "group_rate_limit_window_seconds": null
    }
  ]
}
```

`null` 值表示继承全局配置。完整字段说明见 [GroupConfig 字段](#groupconfig-字段)。

---

**`PUT /api/v1/groups/{group_id}`**

创建或更新指定群的配置。Body 为 JSON，包含 GroupConfig 中需要设置的字段（`group_id` 自动取 URL 路径值，无需在 body 中提供）。

请求：
```json
{"name": "测试群", "enabled": true, "require_mention": false}
```

响应 `200` — 返回该群的完整配置：
```json
{
  "group_id": "123456789",
  "name": "测试群",
  "enabled": true,
  "require_mention": false
}
```

实际响应包含完整 GroupConfig 字段，详见下方[字段表](#groupconfig-字段)。

---

**`DELETE /api/v1/groups/{group_id}`**

删除指定群的配置（回退到全局默认）。

响应 `200`：
```json
{"deleted": "123456789"}
```

---

**`POST /api/v1/groups/sync`**

从 OneBot 同步机器人加入的群列表，自动将新群加入配置（新群使用默认设置，已有配置的群不受影响）。

响应 `200`：
```json
{
  "added": ["123456789", "987654321"],
  "total": 5
}
```

---

### 10. 指令过滤

**`GET /api/v1/commands`**

返回 Hermes 已注册的 slash 指令列表（由插件推送的 snapshot）。

响应 `200`：
```json
{
  "commands": [
    {
      "name": "reset",
      "description": "重置会话",
      "source": "core",
      "aliases": ["restart"],
      "args_hint": ""
    }
  ]
}
```

---

**`POST /api/v1/commands/refresh`**

要求 Hermes 插件重新推送指令列表（刷新指令 snapshot）。

响应 `200`：
```json
{"sent": true}
```

响应 `503` — relay 未就绪：
```json
{"error": "relay not ready"}
```

---

### 11. 工具集管理

**`GET /api/v1/hermes_tools`**

返回 OneBot 平台可配置的工具集列表和当前启用状态。

响应 `200`：
```json
{
  "configurable": [
    {
      "key": "onebot",
      "label": "OneBot 工具",
      "description": "OneBot 核心工具集",
      "tools": ["onebot_send_message", "onebot_get_group_info", "..."],
      "is_plugin": false
    }
  ],
  "mcp_servers": [
    {"name": "my_mcp", "enabled": true}
  ],
  "current_enabled": ["onebot"],
  "hermes_dir_ok": true
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `configurable` | array | 可配置的工具集列表 |
| `configurable[].key` | string | 工具集标识符 |
| `configurable[].label` | string | 显示名称 |
| `configurable[].description` | string | 描述 |
| `configurable[].tools` | string[] | 包含的工具名列表 |
| `configurable[].is_plugin` | bool | 是否插件工具集 |
| `mcp_servers` | array | 可用 MCP 服务器列表 |
| `mcp_servers[].name` | string | MCP 服务器名称 |
| `mcp_servers[].enabled` | bool | 全局启用状态 |
| `current_enabled` | string[] | OneBot 平台当前启用的工具集/MCP key 列表 |
| `hermes_dir_ok` | bool | Hermes 安装目录是否有效 |

---

**`PUT /api/v1/hermes_tools`**

设置 OneBot 平台启用的工具集和 MCP 服务器。Body（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `toolsets` | string[] | 是 | 启用的工具集 key 列表 |
| `mcp_servers` | string[] | 是 | 启用的 MCP 服务器名列表 |
| `no_mcp` | bool | 否 | 设为 `true` 时屏蔽全部 MCP 服务器 |

请求：
```json
{
  "toolsets": ["onebot"],
  "mcp_servers": [],
  "no_mcp": false
}
```

响应 `200`：
```json
{
  "ok": true,
  "saved": ["onebot"],
  "platform": "onebot"
}
```

响应 `400` — key 无效：
```json
{"error": "无效的工具集 key: ['nonexistent']"}
```

---

**`POST /api/v1/hermes_tools/reset`**

重置 OneBot 平台工具集到默认值。

响应 `200`：
```json
{"ok": true}
```

#### OneBot 逐工具策略

以下接口使用 WebUI session，只管理 Hermes 插件的工具注册和权限，不影响 automation key 对 `/api/v1/tools/*` 的全量访问。

**`GET /api/v1/onebot_tool_policies`**

返回完整工具目录、默认策略、当前生效策略和稀疏覆盖：

```json
{
  "catalog": [
    {
      "name": "onebot_set_group_portrait",
      "schema": {"name": "onebot_set_group_portrait"},
      "default_registered": true,
      "default_permission": "admin",
      "category": "群聊",
      "scope": "group",
      "packet": false,
      "caveat": null
    }
  ],
  "effective_policies": {
    "onebot_set_group_portrait": {"registered": true, "permission": "admin"}
  },
  "sparse_policies": {},
  "restart_required": true
}
```

**`PUT /api/v1/onebot_tool_policies`**

```json
{
  "policies": {
    "onebot_get_group_list": {"registered": true, "permission": "everyone"},
    "onebot_set_group_portrait": {"registered": false, "permission": "admin"}
  }
}
```

`registered=false` 表示 Hermes 下次启动时不注册该工具；`permission` 只接受 `everyone` 或 `admin`。配置写入 `<hermes>/config.yaml` 的 `plugins.entries.onebot.tool_policies`，仅保存偏离默认值的条目。群管理员只能对当前群调用群级 `admin` 工具；跨群及账号级工具要求全局管理员。

**`POST /api/v1/onebot_tool_policies/reset`**

删除 OneBot 工具策略覆盖并恢复全部默认值。注册可见性变更均需重启 Hermes。

---

### 12. Hermes 会话隔离模式

OneBot 平台的 `group_sessions_per_user`（Hermes 顶层配置）决定群聊会话隔离方式：`true`=每人独立 session，`false`=全群共享 session（适配器启用群聊排队）。该值由插件连接时上报给适配器，WebUI「连接管理」页可直接修改 Hermes `config.yaml` 的此字段。

**`GET /api/v1/hermes_mode`**

返回当前生效的 `group_sessions_per_user` 值及来源。

响应 `200`（插件已连接，值来自插件上报）：
```json
{
  "group_sessions_per_user": true,
  "source": "plugin_report",
  "plugin_connected": true
}
```

响应 `200`（插件未连接，回退读 Hermes `config.yaml`）：
```json
{
  "group_sessions_per_user": true,
  "source": "hermes_config_yaml",
  "plugin_connected": false
}
```

响应 `200`（文件中无该字段，使用 Hermes 默认 `true`）：
```json
{
  "group_sessions_per_user": true,
  "source": "default",
  "plugin_connected": false
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `group_sessions_per_user` | bool | 当前生效的会话隔离模式 |
| `source` | string | 值来源：`plugin_report`（插件上报）/ `hermes_config_yaml`（文件读取）/ `default`（Hermes 默认） |
| `plugin_connected` | bool | Hermes 插件是否已连接 |

响应 `500` — 读取 Hermes `config.yaml` 失败：
```json
{"error": "读取 Hermes config.yaml 失败: <详情>"}
```

---

**`PUT /api/v1/hermes_mode`**

写入 `group_sessions_per_user` 到 Hermes `config.yaml`（顶层字段）。Body（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `group_sessions_per_user` | bool | 是 | 会话隔离模式 |

请求：
```json
{"group_sessions_per_user": false}
```

响应 `200`：
```json
{
  "ok": true,
  "written": false,
  "restart_required": true,
  "note": "已写入 Hermes config.yaml,需重启 Hermes 网关生效。重启后请点击'刷新上报值'更新显示。"
}
```

响应 `400` — `hermes_install_dir` 未配置或值类型错误：
```json
{"error": "group_sessions_per_user 必须是布尔值"}
```

> 写入后需**重启 Hermes 网关**生效。重启后点击「刷新上报值」让插件重新上报，更新 WebUI 显示。

---

**`POST /api/v1/hermes_mode/refresh`**

要求已连接的 Hermes 插件重新上报 `group_sessions_per_user`（用于 Hermes 重启或配置变更后刷新显示）。

响应 `200`：
```json
{"ok": true, "note": "已请求插件重新上报,稍后刷新页面查看"}
```

响应 `503` — relay 未就绪：
```json
{"error": "relay 未就绪"}
```

响应 `200`（插件未连接，无法刷新）：
```json
{
  "ok": false,
  "error": "Hermes 插件未连接,无法刷新。请先确保插件已连接。"
}
```

---

### 13. 版本更新检查

**`GET /api/v1/update_check`**

查询 GitHub 最新版本 tag 并与当前适配器版本比较。结果在服务端缓存 1 小时（错误结果缓存 5 分钟）。

响应 `200`（无更新）：
```json
{
  "current_version": "1.4.0",
  "latest_version": "1.4.0",
  "has_update": false,
  "changelog_url": "https://github.com/DNAlec/hermes-onebot-adapter/blob/main/CHANGELOG.md"
}
```

响应 `200`（有更新）：
```json
{
  "current_version": "1.3.0",
  "latest_version": "1.4.0",
  "has_update": true,
  "changelog_url": "https://github.com/DNAlec/hermes-onebot-adapter/blob/main/CHANGELOG.md"
}
```

响应 `200`（请求失败，含错误字段）：
```json
{
  "current_version": "1.4.0",
  "latest_version": "1.4.0",
  "has_update": false,
  "changelog_url": "https://github.com/DNAlec/hermes-onebot-adapter/blob/main/CHANGELOG.md",
  "error": "GitHub API returned 403"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_version` | string | 当前适配器版本 |
| `latest_version` | string | GitHub 最新 tag（无可用 tag 时等于 `current_version`） |
| `has_update` | bool | 是否有新版本 |
| `changelog_url` | string | CHANGELOG 链接 |
| `error` | string | 仅在请求失败时出现，描述失败原因 |

---

### 14. 使用统计

适配器记录通过准入与指令过滤的消息元数据（QQ 号、群号、时间、是否系统事件，**不含消息正文或媒体地址**）到 `~/.onebot_adapter/usage_stats.sqlite3`，默认保留 365 天。关闭 `usage_stats_enabled` 后停止新增，已有历史仍可查询。

**`GET /api/v1/usage/stats`**

查询时间范围内的统计聚合。Query 参数：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `start` | float | 否 | 起始 Unix 时间戳（默认 7 天前） |
| `end` | float | 否 | 结束 Unix 时间戳（默认当前） |
| `scope` | string | 否 | `all`（默认）/ `dm` / `group` |
| `bucket` | string | 否 | 聚合粒度：`hour` / `day`（默认） |
| `group_id` | string | 否 | 按群过滤（`scope=dm` 时不可用） |
| `user_id` | string | 否 | 按用户过滤 |
| `tz_offset_minutes` | int | 否 | 时区偏移分钟数（默认 0，范围 -1440~1440），影响 bucket 边界 |

响应 `200`：
```json
{
  "enabled": true,
  "start": 1721424000.0,
  "end": 1722028800.0,
  "bucket": "day",
  "summary": {
    "total": 1234,
    "active_groups": 3,
    "active_users": 12
  },
  "trend": [
    {"bucket_start": 1721424000.0, "count": 42},
    {"bucket_start": 1721510400.0, "count": 58}
  ],
  "top_groups": [
    {"id": "123456789", "name": "测试群", "count": 200}
  ],
  "top_users": [
    {"id": "100", "name": "Alice", "count": 80}
  ]
}
```

响应 `400` — 参数非法：
```json
{"error": "scope must be all, dm, or group"}
```

响应 `503` — 统计存储不可用：
```json
{"error": "usage statistics unavailable"}
```

---

**`GET /api/v1/usage/dimensions`**

返回时间范围内出现过的群和用户维度（用于前端过滤下拉）。Query 参数同 `start`/`end`。

响应 `200`：
```json
{
  "groups": [
    {"id": "123456789", "name": "测试群"},
    {"id": "987654321", "name": ""}
  ],
  "users": [
    {"id": "100", "name": "Alice"},
    {"id": "200", "name": ""}
  ]
}
```

---

**`DELETE /api/v1/usage`**

清空全部使用统计数据（不可恢复）。操作记入审计日志。

响应 `200`：
```json
{"ok": true, "deleted": 1234}
```

响应 `503` — 统计存储不可用。

---

### 15. Bot 动态黑名单记录

Bot 通过 `onebot_get_bot_blacklist` / `onebot_edit_bot_blacklist` 工具写入的临时拉黑记录持久化到 `~/.onebot_adapter/bot_blacklist.sqlite3`，WebUI「聊天配置」页可查看并人工解除。记录字段见响应示例。

**`GET /api/v1/bot_blacklist`**

列出当前有效的黑名单记录（已过期记录自动清理，不返回）。Query 参数均可选：

| 参数 | 类型 | 说明 |
|------|------|------|
| `scope` | string | 过滤范围：`group` / `dm` / `global` |
| `group_id` | string | 过滤群号（`scope=group` 时使用） |
| `user_id` | string | 过滤用户 QQ 号 |

响应 `200`：
```json
{
  "entries": [
    {
      "id": 1,
      "scope": "group",
      "group_id": "123456789",
      "user_id": "100",
      "created_at": 1721500000.0,
      "duration_seconds": 3600,
      "expires_at": 1721503600.0,
      "remaining_seconds": 1800,
      "remaining": "30分钟",
      "reason": "刷屏",
      "created_by_user_id": "200"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 记录主键 |
| `scope` | string | 作用域：`group`（单群）/ `dm`（私聊）/ `global`（全局） |
| `group_id` | string | 群号（`scope=group` 时有值，否则为空） |
| `user_id` | string | 被拉黑用户 QQ 号 |
| `created_at` | float | 创建时间（Unix 时间戳） |
| `duration_seconds` | int | 拉黑时长（秒） |
| `expires_at` | float | 到期时间（Unix 时间戳） |
| `remaining_seconds` | int | 剩余秒数（向上取整） |
| `remaining` | string | 剩余时间中文描述 |
| `reason` | string | 拉黑原因 |
| `created_by_user_id` | string | 发起拉黑的用户 QQ 号（bot 工具调用时传入） |

响应 `400` — `scope` 取值非法：
```json
{"error": "scope must be one of ['dm', 'global', 'group']"}
```

响应 `503` — 黑名单存储不可用：
```json
{"error": "bot blacklist unavailable"}
```

---

**`DELETE /api/v1/bot_blacklist/{entry_id}`**

人工解除单条黑名单记录。操作记入审计日志。

响应 `200`：
```json
{"ok": true, "removed": true}
```

响应 `404` — 记录不存在：
```json
{"error": "entry not found"}
```

响应 `400` — `entry_id` 非整数：
```json
{"error": "invalid entry_id"}
```

---

### 16. 限流额度管理

**`GET /api/v1/rate_limit/quota?scope=<global|group|user>&target_id=<ID>`**

查询当前生效策略、已用/剩余额度、恢复时间及持久化状态。`group` / `user` 必须传纯数字 `target_id`；`global` 不得传 `target_id`。

响应 `200`：
```json
{
  "scope": "user",
  "target_id": "123456",
  "rate_limit_enabled": true,
  "scope_enabled": true,
  "algorithm": "sliding_window",
  "limit": 10,
  "window_seconds": 60.0,
  "tracked": true,
  "used": 3.0,
  "remaining": 7.0,
  "next_available_in_seconds": 0.0,
  "full_recovery_in_seconds": 42.5,
  "persistence": {
    "status": "healthy",
    "last_success_at": 1787040000.0,
    "pending_operations": 0,
    "pending_limit": 50000,
    "fallback_exhausted": false,
    "failure_mode": "memory_fallback"
  }
}
```

`tracked=false` 表示该作用域当前没有桶；`status` 为 `not_started` / `healthy` / `degraded` / `recovering`。`next_available_in_seconds` 是下一条消息可通过的等待时间，`full_recovery_in_seconds` 是额度完全恢复的预计时间。

**`POST /api/v1/rate_limit/quota/reset`**

请求体：
```json
{"scope": "user", "target_id": "123456"}
```

仅重置指定维度，不级联清除全局或其他维度。响应中 `pending_persistence=true` 表示已在内存中重置，待数据库恢复后同步。查询和重置响应均使用 `Cache-Control: no-store`，重置操作记入审计日志。

响应 `200` 与查询响应字段相同，并增加：
```json
{
  "cleared": true,
  "pending_persistence": false
}
```

常见错误：

- `400` — `scope` 非 `global` / `group` / `user`，目标缺失、非纯数字，或为 `global` 额外传入了 `target_id`
- `401` — WebUI session 无效或缺失
- `503` — 限流器不可用，或持久化故障期间待同步操作已达到上限

---

## 数据类型

### Config 字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `onebot_mode` | string | `"reverse"` | OneBot 连接模式：`reverse` / `forward` |
| `onebot_reverse_ws_port` | int | `18800` | OneBot 反向 WS 监听端口 |
| `onebot_reverse_ws_path` | string | `"/onebot"` | OneBot 反向 WS 路径 |
| `onebot_forward_ws_url` | string | `"ws://127.0.0.1:3001"` | 正向模式 NapCat 地址 |
| `onebot_ws_token` | string | 自动生成 | OneBot WS 鉴权 token |
| `self_id` | string | `""` | 机器人 QQ 号（自动探测） |
| `group_require_mention` | bool | `true` | 群聊是否需 @bot 触发 |
| `group_mention_first_only` | bool | `false` | True=仅首 @ 触发 |
| `group_trigger_keywords` | string[] | `[]` | 群聊关键词触发列表 |
| `group_keyword_first_only` | bool | `false` | True=关键词须在开头 |
| `group_strip_first_mention` | bool | `true` | True=消息以@bot开头时移除该段(非首@bot保留) |
| `global_admins` | string[] | `[]` | 全局管理员 QQ 号列表 |
| `dm_user_filter_mode` | string | `"whitelist"` | 私聊过滤：`whitelist` / `blacklist` |
| `dm_user_list` | string[] | `[]` | 私聊用户过滤列表 |
| `groups` | object | `{}` | 群组配置，key 为群号字符串 |
| `global_channel_prompt` | string | 默认提示词 | 全局提示词；保存时物化写入 Hermes config.yaml 的 `platforms.onebot.channel_prompts`，需重启 Hermes 网关生效 |
| `hermes_ws_port` | int | `18810` | Hermes 插件 WS 端口 |
| `hermes_ws_path` | string | `"/hermes"` | Hermes 插件 WS 路径 |
| `hermes_ws_token` | string | 自动生成 | Hermes WS 鉴权 token |
| `hermes_install_dir` | string | `""` | Hermes 安装目录（插件安装/工具集读写/会话隔离模式写入的目标路径） |
| `webui_port` | int | `18820` | WebUI 端口 |
| `webui_token` | string | 自动生成 | WebUI 登录原始 token（仅用于 `/api/v1/auth/login`，不可直接调其他 API；`GET /api/v1/config` 不返回此字段） |
| `webui_token_lifetime_hours` | int | `168` | 登录有效期（小时），最小 1，默认 7 天；修改后所有已登录会话立即失效 |
| `webui_token_epoch` | int | `0` | token 纪元（内部状态，用于会话失效；不在 API 中暴露，不接受客户端设置） |
| `webui_trust_proxy_headers` | bool | `false` | 信任 `X-Forwarded-For` 获取客户端 IP（仅反向代理时开启；直连开启会被伪造 IP 绕过登录限流） |
| `automation_api_enabled` | bool | `false` | 自动化工具 API 总开关；关闭时 `/api/v1/tools*` 返回 403 |
| `automation_api_key_hash` | string | `""` | 自动化 key 的 SHA-256 摘要；仅配置文件内部使用，不通过管理 API 返回或接受客户端修改 |
| `automation_api_key_configured` | bool | 派生值 | `GET /api/v1/config` 返回的只读状态，表示是否已配置 key |
| `automation_upload_allowed_roots` | string[] | `["/tmp/hermes-onebot-adapter-uploads"]` | HTTP 工具可引用的本地文件根目录；校验解析后的真实路径 |
| `log_level` | string | `"INFO"` | 日志级别：`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |
| `log_message_preview` | int | `100` | 消息正文日志截断长度 |
| `log_file_enabled` | bool | `true` | 是否启用文件日志 |
| `log_file_dir` | string | `""` | 日志文件目录（空=默认） |
| `log_file_message_mode` | string | `"preview"` | 文件日志消息正文：`none`/`preview`/`full` |
| `log_file_max_bytes` | int | `10485760` | 单个日志文件大小上限（字节，默认 10 MiB）；达到上限后立即轮转 |
| `log_retention_days` | int | `3` | 日志保留天数 |
| `usage_stats_enabled` | bool | `true` | 用量统计开关；关闭后停止新增记录，已有历史仍可查询 |
| `usage_stats_retention_days` | int | `365` | 用量统计保留天数；缩短后保存配置会立即清理过期数据 |
| `message_show_group_id` | bool | `true` | 消息是否显示群号标识 |
| `seq_map_size` | int | `4500` | seq map 环形缓冲区大小 |
| `reaction_emoji_enabled` | bool | `true` | 消息送达 Hermes 后在原消息贴表情回应；群配置可单独覆盖 |
| `reaction_emoji_id` | string | `"124"` | 贴表情回应使用的表情 ID（QQ 表情编号） |
| `reaction_emoji_id_queued` | string | `"123"` | 消息排队时贴的表情 ID（空=不贴表情） |
| `file_upload_timeout` | float | `600.0` | 群聊、私聊和闪传上传均按此秒数等待（范围 30–600）；群上传超时后短轮询群历史，闪传超时无法确认结果，均返回不可自动重试的“结果未知” |
| `send_dedup_enabled` | bool | `true` | 发送去重开关（防 Gateway send_text 超时重试导致重复发送） |
| `send_dedup_ttl_seconds` | float | `10.0` | 发送去重 TTL（秒） |
| `event_queue_enabled` | bool | `true` | 群聊排队总开关：Hermes 不隔离群成员时是否排队 |
| `event_queue_max_per_chat` | int | `50` | 群聊排队：单群队列上限，超限拒绝入队（详见[群聊消息排队](#群聊消息排队)） |
| `event_queue_idle_timeout` | float | `300.0` | 群聊排队：plugin 无 idle 信号的超时阈值（秒），超时强制清空 busy 状态 |
| `event_queue_clear_on_session_reset` | bool | `true` | 使用 `/new`、`/reset` 时清空当前群待处理队列 |
| `event_queue_clean_command_enabled` | bool | `true` | 启用适配器本地 `/clean` 命令；清空当前群待处理队列且不转发 Hermes |
| `rate_limit_enabled` | bool | `false` | 入站消息限流总开关；全局/群聊/个人三维度同时检查，管理员豁免 |
| `global_rate_limit_algorithm` | string | `"sliding_window"` | 全局限流算法：`sliding_window` / `token_bucket` |
| `global_rate_limit_messages` | int | `0` | 全局限流消息数；`0`=禁用该维度 |
| `global_rate_limit_window_seconds` | float | `0.0` | 全局限流窗口秒数；启用时须 > 0 |
| `group_rate_limit_algorithm` | string | `"sliding_window"` | 群聊限流算法；群配置可单独覆盖 |
| `group_rate_limit_messages` | int | `0` | 每个群聊的限流消息数；`0`=禁用该维度；群配置可覆盖 |
| `group_rate_limit_window_seconds` | float | `0.0` | 群聊限流窗口秒数；群配置可覆盖 |
| `user_rate_limit_algorithm` | string | `"sliding_window"` | 个人限流算法；同一 QQ 在私聊和所有群共享计数 |
| `user_rate_limit_messages` | int | `0` | 每个 QQ 的限流消息数；`0`=禁用该维度 |
| `user_rate_limit_window_seconds` | float | `0.0` | 个人限流窗口秒数 |
| `rate_limit_reject_message` | string | `"⛔..."` | 限流提示模板；支持 `{scope}`/`{retry_after}`/`{user_id}` |
| `rate_limit_storage_failure_mode` | string | `"memory_fallback"` | 持久化故障策略：`memory_fallback` / `reject` |
| `bot_blacklist_enabled` | bool | `true` | 允许 bot 使用动态黑名单工具并在消息触发时拦截命中用户 |
| `bot_blacklist_max_duration_seconds` | int | `86400` | bot 单次动态拉黑允许的最大秒数（默认 24 小时）；bot 请求超限时自动截短 |
| `bot_blacklist_reject_message` | string | `"⛔..."` | 动态拉黑提示模板；支持 `{user_id}`/`{scope}`/`{remaining}`/`{expires_at}`/`{reason}` |
| `media_delivery_mode` | string | `"cache"` | 媒体投递模式：`cache`（默认，插件侧下载落盘到 `~/.hermes/cache/`）/ `passthrough`（URL 作为文本占位符直传） |
| `command_filter_enabled` | bool | `false` | 指令过滤总开关 |
| `command_filter_unknown` | bool | `false` | 未知指令是否过滤 |
| `command_permissions` | object | `{}` | 全局指令权限：`{指令名: "everyone"/"admin"/"disabled"}` |
| `command_reject_message` | string | `"⛔..."` | 指令拒绝回复模板（`{cmd}` 替换为指令名） |
| `outbound_filter_enabled` | bool | `false` | 出站正则过滤总开关；命中则丢弃 Hermes 发往 OneBot 的文本 |
| `outbound_filter_patterns` | string[] | `[]` | 出站过滤正则（Python `re.search`）；空=不过滤；最多 50 条、每条最多 256 字符 |
| `notify_poke_enabled` | bool | `false` | 戳一戳（bot 被戳）推送开关；开启后合成系统事件转发给 agent |
| `notify_member_change_enabled` | bool | `false` | 群成员进退群推送开关；开启后合成系统事件转发给 agent |

### GroupConfig 字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `group_id` | string | — | 群号 |
| `name` | string | `""` | 群名称 |
| `enabled` | bool | `true` | 群是否启用 |
| `require_mention` | bool\|null | `null` | 需 @bot 触发（null=跟随全局） |
| `mention_first_only` | bool\|null | `null` | 仅首 @ 触发 |
| `trigger_keywords` | string[]\|null | `null` | 关键词列表（`[]`=强制禁用） |
| `keyword_first_only` | bool\|null | `null` | 关键词须在开头 |
| `strip_first_mention` | bool\|null | `null` | 移除首 @bot 段 |
| `custom_prompt` | string | `""` | 群专属提示词（保存时物化写入 Hermes config.yaml；空=用全局提示词） |
| `admins` | string[] | `[]` | 群管理员 QQ 号 |
| `group_user_filter_mode` | string | `"blacklist"` | 用户过滤：`whitelist`/`blacklist` |
| `group_user_list` | string[] | `[]` | 用户过滤列表 |
| `message_show_group_id` | bool\|null | `null` | 显示群号标识 |
| `reaction_emoji_enabled` | bool\|null | `null` | 消息送达贴表情回应（null=跟随全局） |
| `command_filter_enabled` | bool\|null | `null` | 指令过滤开关 |
| `command_filter_unknown` | bool\|null | `null` | 未知指令过滤 |
| `command_permissions` | object\|null | `null` | 群级指令权限覆盖 |
| `outbound_filter_enabled` | bool\|null | `null` | 出站正则过滤开关（null=跟随全局） |
| `outbound_filter_patterns` | string[]\|null | `null` | 出站过滤正则（null=跟随全局，`[]`=此群无规则，不与全局合并） |
| `notify_poke_enabled` | bool\|null | `null` | 戳一戳推送开关（null=跟随全局） |
| `notify_member_change_enabled` | bool\|null | `null` | 群成员进退群推送开关（null=跟随全局） |
| `group_rate_limit_algorithm` | string\|null | `null` | 群聊限流算法覆盖（`sliding_window`/`token_bucket`，null=跟随全局） |
| `group_rate_limit_messages` | int\|null | `null` | 群聊限流消息数覆盖（`0`=禁用该群维度，null=跟随全局） |
| `group_rate_limit_window_seconds` | float\|null | `null` | 群聊限流窗口秒数覆盖（null=跟随全局） |

> `null` 值表示跟随全局配置。`[]`（空数组）和 `{}`（空对象）表示强制设为空（不等于 null）。

## 群聊消息排队

适配器内置 shared 群聊消息排队机制，防止群聊中多个群成员的消息互相打断 agent 当前任务。**只在 Hermes 配置 `group_sessions_per_user: false`（全群共享 session）且适配器 `event_queue_enabled: true` 时生效**；`per_user` 模式每人独立 session，无需排队。

### 触发条件

- Hermes 端 `group_sessions_per_user=false`（插件读 `self.config.extra.get("group_sessions_per_user", True)` 判定，与 `BasePlatformAdapter.handle_message` 完全一致）
- 适配器端 `event_queue_enabled=true`（WebUI 聊天配置页可切换）

### 排队规则

| 场景 | 行为 |
|------|------|
| 私聊（`chat_id` 为纯 QQ 号） | 直接转发，不排队 |
| Hermes 隔离群成员（per_user=True） | 直接转发，不排队 |
| 适配器排队总开关关闭 | 直接转发，不排队 |
| 群未 busy | 标记 busy（记录 user_id + 时间戳），转发 |
| 群 busy | 入队等待（含 busy 用户自身）；出队时连续同用户消息合并为一条 |
| `/new`、`/reset` | 绕过排队发给 Hermes；默认同时清空当前群待处理队列 |
| `/clean` | 默认由适配器本地清空当前群待处理队列，不发送给 Hermes |
| 其他 `/` 开头的消息 | **始终直接转发**（绕过排队） |

### idle 信号

处理完成的"idle"信号由 Hermes 插件通过 `register_post_delivery_callback` 钩子发送：每轮 agent 处理结束后，插件向适配器发 `{"type":"idle","v":1,"chat_id":"group:<gid>","group_id":"<gid>"}` 帧，适配器清空 busy 并从队列取下一条转发。

`/stop`、`/new`、`/reset` 命令会导致 Hermes 中断当前 turn 但**不触发 idle 帧**（gateway `run.py:11099-11112` 直接 pop callback 不调用），适配器会在 broadcast 这些命令 3 秒后主动清空 busy 槽防止队列卡死。

### 看门狗兜底

若插件崩溃或 idle 帧丢失导致 busy 状态永久卡死，看门狗会在 `event_queue_idle_timeout`（默认 300 秒）后强制清空 busy 并派发下一条。

### 清理时机

- 最后一个 Hermes 插件连接断开时清空所有 busy/queue
- 适配器服务停止时清空所有状态
- 插件重连重放 ring buffer 时清空 queue/busy（重新建立状态）

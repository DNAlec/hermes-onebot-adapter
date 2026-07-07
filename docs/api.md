# Hermes OneBot Adapter — REST API 文档

基础地址: `http://<host>:18820`（默认端口，可在配置中修改 `webui_port`）

---

## 鉴权

除 `/api/health` 和 `/api/login` 外，所有 `/api/*` 端点均需鉴权。

适配器采用**签名 session token** 机制：原始 `webui_token`（首次启动时自动生成并打印到日志，也可在 `~/.onebot_adapter/config.json` 的 `webui_token` 字段查看）只能用于登录，**不能直接用于其他 API 调用**。登录成功后服务端返回一个带有效期的 HMAC 签名 token，后续请求使用该签名 token。

### 登录流程

**`POST /api/login`**（无需鉴权，但有失败次数限制，见下文）

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

拿到 `session_token` 后，两种传 token 方式：

1. **Authorization header（推荐）**：`Authorization: Bearer <session_token>`
2. **Query 参数**：`?token=<session_token>`

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
r = requests.post(f"{base}/api/login", json={"token": "原始webui_token"})
session = r.json()["session_token"]
# 2. 后续调用用签名 token
r = requests.get(f"{base}/api/status",
                 headers={"Authorization": f"Bearer {session}"})
print(r.json())
```

**curl**：
```bash
# 1. 登录拿 session_token
SESSION=$(curl -s -X POST http://host:18820/api/login \
  -H "Content-Type: application/json" \
  -d '{"token":"原始webui_token"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['session_token'])")
# 2. 用签名 token 调 API
curl -H "Authorization: Bearer $SESSION" http://host:18820/api/status
```

### 登录失败次数限制（防爆破）

`/api/login` 按客户端 IP 计数：同一 IP 累计 **5 次**登录失败后，封禁该 IP **15 分钟**，期间任何登录尝试直接返回 `429`（不再执行 token 校验）。封禁期间其他已持有有效签名 token 的 API 调用不受影响。封禁到期自动解封；登录成功会立即清零该 IP 的失败计数。计数状态仅在进程内存中，**重启适配器即清空**。

### 修改有效期

在 WebUI 高级设置页修改 `webui_token_lifetime_hours` 后保存，所有已签发的签名 token 立即失效（包括当前会话），需要重新登录。这是通过内部 `webui_token_epoch` 字段递增实现的，无需手动操作。

---

## 端点

### 1. 健康检查（无需鉴权）

**`GET /api/health`**

响应 `200`：
```json
{"status": "ok"}
```

### 2. 登录（无需鉴权，受失败次数限制）

见上方「鉴权 → 登录流程」章节。

---

### 3. 服务状态

**`GET /api/status`**

响应 `200`：
```json
{
  "adapter_version": "0.1.0",
  "onebot_connected": true,
  "hermes_plugin_connected": true,
  "onebot_mode": "reverse",
  "self_id": "123456",
  "onebot_ws_port": 18800,
  "hermes_ws_port": 18810,
  "webui_port": 18820
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `adapter_version` | string | 适配器版本号 |
| `onebot_connected` | bool | OneBot 客户端是否已连接 |
| `hermes_plugin_connected` | bool | Hermes 插件是否已连接 |
| `onebot_mode` | string | OneBot 连接模式：`reverse`（被动等待）/ `forward`（主动连接） |
| `self_id` | string | 机器人 QQ 号 |

---

### 4. 配置管理

**`GET /api/config`**

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
  "group_keep_mention": false,
  "global_admins": [],
  "group_auto_join": false,
  "dm_user_filter_mode": "whitelist",
  "dm_user_list": [],
  "groups": {},
  "platform_hint": "...",
  "hermes_ws_port": 18810,
  "hermes_ws_path": "/hermes",
  "hermes_ws_token": "...",
  "hermes_install_dir": "",
  "webui_port": 18820,
  "webui_token": "...",
  "log_level": "INFO",
  "log_message_preview": 100,
  "log_file_enabled": true,
  "log_file_dir": "",
  "log_retention_days": 3,
  "message_show_group_id": false,
  "seq_map_size": 4500,
  "command_filter_enabled": false,
  "command_filter_unknown": false,
  "command_permissions": {},
  "command_reject_message": "⛔ 你没有权限使用此指令 /{cmd}"
}
```

完整字段说明见 [Config 字段表](#config-字段)。

---

**`PUT /api/config`**

部分更新配置，只传需要修改的字段。Body 为 JSON 对象，包含要更新的键值对。

请求 `200`：
```json
{"log_level": "DEBUG"}
```

响应 `200` — 返回更新后的完整配置（与 `GET /api/config` 同结构）。

响应 `400` — 校验失败：
```json
{"error": "onebot_mode must be one of ['forward', 'reverse']"}
```

---

### 5. Hermes 安装目录状态

**`GET /api/hermes_dir_status`**

响应 `200`：
```json
{
  "hermes_dir": "/home/user/.hermes/hermes-agent",
  "exists": true
}
```

---

### 6. 插件管理

**`POST /api/install_plugin`**

将 OneBot 插件安装到 Hermes。Body（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `hermes_install_dir` | string | 否 | Hermes 安装目录；留空使用配置中的值 |

响应 `200`：
```json
{
  "adapter_version": "0.1.0",
  "plugin_dest": "/home/user/.hermes/plugins/onebot/",
  "files_copied": 5,
  "config_updated": true
}
```

---

**`POST /api/uninstall_plugin`**

从 Hermes 卸载 OneBot 插件。Body 同上。

响应 `200`：
```json
{
  "removed": true,
  "plugin_dir": "/home/user/.hermes/plugins/onebot/"
}
```

---

### 7. 发送消息

**`POST /api/send`**

通过机器人发送消息到群聊或私聊。Body（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `chat_id` | string | 是 | 目标：QQ 号（私聊）或 `group:<群号>`（群聊） |
| `message` | string | 是 | 消息文本内容 |

响应 `200`：
```json
{
  "success": true,
  "message_id": "12345"
}
```

响应 `400` — 缺少必填字段：
```json
{"error": "chat_id and message required"}
```

响应 `503` — OneBot 未连接：
```json
{"error": "adapter not ready"}
```

---

### 8. 日志

**`GET /api/logs`**

返回服务端环形缓冲区中的最近日志行。

响应 `200`：
```json
{
  "logs": [
    "2025-01-01 12:00:00 INFO onebot_adapter.app: Service starting...",
    "2025-01-01 12:00:01 INFO onebot_adapter.app: OneBot connected"
  ]
}
```

---

### 9. 群组管理

**`GET /api/groups`**

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
      "trigger_keywords": [],
      "keyword_first_only": null,
      "keep_mention": null,
      "custom_prompt": "",
      "admins": [],
      "group_user_filter_mode": "blacklist",
      "group_user_list": [],
      "welcome_enabled": false,
      "welcome_message": "",
      "auto_join": false,
      "message_show_group_id": null,
      "command_filter_enabled": null,
      "command_filter_unknown": null,
      "command_permissions": null
    }
  ]
}
```

`null` 值表示继承全局配置。完整字段说明见 [GroupConfig 字段](#groupconfig-字段)。

---

**`PUT /api/groups/{group_id}`**

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
  ...
}
```

---

**`DELETE /api/groups/{group_id}`**

删除指定群的配置（回退到全局默认）。

响应 `200`：
```json
{"deleted": "123456789"}
```

---

**`POST /api/groups/sync`**

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

**`GET /api/commands`**

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

**`POST /api/commands/refresh`**

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

**`GET /api/hermes_tools`**

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

**`PUT /api/hermes_tools`**

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

**`POST /api/hermes_tools/reset`**

重置 OneBot 平台工具集到默认值。

响应 `200`：
```json
{"ok": true}
```

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
| `group_keep_mention` | bool | `false` | True=保留 @bot 段 |
| `global_admins` | string[] | `[]` | 全局管理员 QQ 号列表 |
| `group_auto_join` | bool | `false` | 是否自动加入新群 |
| `dm_user_filter_mode` | string | `"whitelist"` | 私聊过滤：`whitelist` / `blacklist` |
| `dm_user_list` | string[] | `[]` | 私聊用户过滤列表 |
| `groups` | object | `{}` | 群组配置，key 为群号字符串 |
| `platform_hint` | string | 默认提示词 | 注入 LLM 系统提示的平台说明 |
| `hermes_ws_port` | int | `18810` | Hermes 插件 WS 端口 |
| `hermes_ws_path` | string | `"/hermes"` | Hermes 插件 WS 路径 |
| `hermes_ws_token` | string | 自动生成 | Hermes WS 鉴权 token |
| `hermes_install_dir` | string | `""` | Hermes 安装目录 |
| `webui_port` | int | `18820` | WebUI 端口 |
| `webui_token` | string | 自动生成 | WebUI 登录原始 token（仅用于 `/api/login`，不可直接调其他 API） |
| `webui_token_lifetime_hours` | int | `168` | 登录有效期（小时），最小 1，默认 7 天；修改后所有已登录会话立即失效 |
| `webui_token_epoch` | int | `0` | token 纪元（内部状态，勿手动修改） |
| `log_level` | string | `"INFO"` | 日志级别：`DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `log_message_preview` | int | `100` | 消息正文日志截断长度 |
| `log_file_enabled` | bool | `true` | 是否启用文件日志 |
| `log_file_dir` | string | `""` | 日志文件目录（空=默认） |
| `log_retention_days` | int | `3` | 日志保留天数 |
| `message_show_group_id` | bool | `false` | 消息是否显示群号标识 |
| `seq_map_size` | int | `4500` | seq map 环形缓冲区大小 |
| `event_queue_enabled` | bool | `true` | 群聊排队总开关：Hermes 不隔离群成员时是否排队 |
| `event_queue_max_per_chat` | int | `50` | 群聊排队：单群队列上限，超限拒绝入队（详见[群聊消息排队](#群聊消息排队)） |
| `event_queue_idle_timeout` | float | `300.0` | 群聊排队：plugin 无 idle 信号的超时阈值（秒），超时强制清空 busy 状态 |
| `command_filter_enabled` | bool | `false` | 指令过滤总开关 |
| `command_filter_unknown` | bool | `false` | 未知指令是否过滤 |
| `command_permissions` | object | `{}` | 全局指令权限：`{指令名: "everyone"/"admin"/"disabled"}` |
| `command_reject_message` | string | `"⛔..."` | 指令拒绝回复模板（`{cmd}` 替换为指令名） |

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
| `keep_mention` | bool\|null | `null` | 保留 @ 段 |
| `custom_prompt` | string | `""` | 群专属提示词（覆盖全局 platform_hint） |
| `admins` | string[] | `[]` | 群管理员 QQ 号 |
| `group_user_filter_mode` | string | `"blacklist"` | 用户过滤：`whitelist`/`blacklist` |
| `group_user_list` | string[] | `[]` | 用户过滤列表 |
| `welcome_enabled` | bool | `false` | 新人欢迎是否启用 |
| `welcome_message` | string | `""` | 欢迎消息模板 |
| `auto_join` | bool | `false` | 自动加入 |
| `message_show_group_id` | bool\|null | `null` | 显示群号标识 |
| `command_filter_enabled` | bool\|null | `null` | 指令过滤开关 |
| `command_filter_unknown` | bool\|null | `null` | 未知指令过滤 |
| `command_permissions` | object\|null | `null` | 群级指令权限覆盖 |

> `null` 值表示跟随全局配置。`[]`（空数组）和 `{}`（空对象）表示强制设为空（不等于 null）。

## 群聊消息排队

适配器内置 shared 群聊消息排队机制，防止群聊中多个群成员的消息互相打断 agent 当前任务。**只在 Hermes 配置 `group_sessions_per_user: false`（全群共享 session）且适配器 `event_queue_enabled: true` 时生效**；`per_user` 模式每人独立 session，无需排队。

### 触发条件

- Hermes 端 `group_sessions_per_user=false`（插件读 `self.config.extra.get("group_sessions_per_user", True)` 判定，与 `BasePlatformAdapter.handle_message` 完全一致）
- 适配器端 `event_queue_enabled=true`（WebUI 连接管理页可切换）

### 排队规则

| 场景 | 行为 |
|------|------|
| 私聊（`chat_id` 为纯 QQ 号） | 直接转发，不排队 |
| Hermes 隔离群成员（per_user=True） | 直接转发，不排队 |
| 适配器排队总开关关闭 | 直接转发，不排队 |
| 群未 busy | 标记 busy（记录 user_id + 时间戳），转发 |
| 群 busy，新消息同一发送者 | **直接转发**（同人可补充当前任务） |
| 群 busy，新消息不同发送者 | 入队等待 |
| `/` 开头的消息 | **始终直接转发**（绕过排队） |

### idle 信号

处理完成的"idle"信号由 Hermes 插件通过 `register_post_delivery_callback` 钩子发送：每轮 agent 处理结束后，插件向适配器发 `{"type":"idle","v":1,"chat_id":"group:<gid>","group_id":"<gid>"}` 帧，适配器清空 busy 并从队列取下一条转发。

### 看门狗兜底

若插件崩溃或 idle 帧丢失导致 busy 状态永久卡死，看门狗会在 `event_queue_idle_timeout`（默认 300 秒）后强制清空 busy 并派发下一条。

### 清理时机

- 最后一个 Hermes 插件连接断开时清空所有 busy/queue
- 适配器服务停止时清空所有状态
- 插件重连重放 ring buffer 时清空 queue/busy（重新建立状态）

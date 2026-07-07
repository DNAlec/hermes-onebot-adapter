# AGENTS.md

Compact guide for OpenCode sessions working in this repo. Read this before editing.

## What this is

A standalone Python service (`onebot_adapter/`) that bridges OneBot 11 (NapCat/go-cqhttp) with a Hermes Agent plugin (`onebot_adapter/hermes_plugin/`). Three aiohttp apps share one process and bind separate ports:

| Port | App | Purpose |
|------|-----|---------|
| 18800 | OneBot reverse WS | OneBot dials in here (`/onebot`); **双向**——同一条 WS 既推事件也接受 API 调用 |
| 18810 | Hermes plugin WS | Plugin connects here (`/hermes?token=`) |
| 18820 | WebUI + REST API | SPA + `/api/*` + `/api/health` |

The adapter service does **all** OneBot interaction over a single WebSocket connection per OneBot instance (事件接收和 API 调用共用同一条 WS,不再有独立 HTTP API 端口); the plugin only talks to the adapter over WS. The plugin runs inside the Hermes gateway process and is installed via the WebUI installer (copies files into `~/.hermes/plugins/onebot/`).

## Developer commands

```bash
# Setup (one-time): editable install with dev deps
pip install -e ".[dev]"

# Run tests (asyncio_mode=auto in pyproject.toml — no @pytest.mark.asyncio needed)
pytest -q                          # all tests (see pytest --collect-only for current count)
pytest tests/test_parser.py -q     # one file
pytest tests/test_command_filter.py::test_config_check_permission_admin_non_admin_denied -q  # one test

# Lint (line-length=120, target py311, selects E/F/W/I/UP/B)
ruff check .

# Run the adapter service locally
hermes-onebot-adapter              # or: python -m onebot_adapter

# Frontend dev (Vite proxy → 127.0.0.1:18820)
cd frontend && npm install && npm run dev    # http://localhost:5173

# Build frontend into the package's static dir (required for WebUI to show)
cd frontend && npm run build       # runs vue-tsc --noEmit && vite build
# or use the script that also copies to site-packages:
./scripts/build_frontend.sh
```

After frontend changes you must rebuild + copy to `onebot_adapter/webui/static/` (gitignored except `.gitkeep`). Local dev needs `./scripts/build_frontend.sh` or manual `cp -r frontend/dist/* onebot_adapter/webui/static/`.

## Verification order

`ruff check .` then `pytest -q`. Both must pass before committing. The repo has no CI configured (no `.github/`), so this is the gate.

## Architecture map

```
NapCat ──反向WS──▶  adapter service  ──WS──▶  Hermes plugin ──▶ Hermes Agent
        (OneBot11)  (独立进程)        (token)    (BasePlatformAdapter)
         双向(事件+API)                纯JSON(无二进制帧)
```

Key modules:
- `onebot_adapter/app.py` — `AdapterService` composes the three aiohttp apps and lifecycle. Entry: `run()`. Creates the shared `WsApiTransport` and injects it into `OneBotApi` + `OneBotReverseServer` + `OneBotForwardClient`. `_probe_self_id` now fires on OneBot WS connect (`_on_onebot_connect`) instead of at startup, since it needs an active WS to call `get_login_info`.
- `onebot_adapter/config.py` — `AdapterConfig` dataclass + `ConfigStore` (thread-safe, hot-reload listeners). Config persists to `~/.onebot_adapter/config.json` (or `$ONEBOT_ADAPTER_CONFIG`). Per-group overrides via `GroupConfig`. **Resolve per-group values through `config.resolve_*(group_id)`**, never read `group_*` fields directly in parser/handlers — group config `None` means "fall back to global".
- `onebot_adapter/onebot/parser.py` — `parse_event()` reduces OneBot 11 events to `NormalizedEvent`. Handles @bot trigger gating, keyword triggers, merged-forward expansion, reply context, /command filtering. All group chats get `chat_id = group:<gid>` (no `:user:` suffix — Hermes' `group_sessions_per_user` is the sole source of session isolation truth, reported by the plugin via `hermes_mode_report` frames). Returns `FilteredEvent` (not a tuple) when a /command is denied — callers must check `isinstance(result, FilteredEvent)`. No longer takes `media_max_bytes`/`media_max_count` params (those config fields have been removed). Group sender prefix shows `#real_seq` (per-group sequence from NapCat); falls back to `#message_id` when `real_seq` absent (go-cqhttp/Lagrange). DMs have no prefix.
- `onebot_adapter/onebot/ws_api.py` — `WsApiTransport`: OneBot 11 API 调用的 WebSocket 传输层。用 `echo` 字段做请求-响应关联(`dict[echo, asyncio.Future]`)。`register(ws)`/`unregister(ws)` 在 WS 连接建立/断开时由 `ws_reverse`/`ws_forward` 调用;`on_text(raw)` 在 `_handle_text` 开头先被调用,命中 pending echo 的响应帧被拦截并 resolve 对应 future(返回 True),否则返回 False 交给 parser。`request(action, params, timeout)` 分配 uuid4 echo,`ws.send_json`,await future。无活跃连接抛 `RuntimeError`;WS 断开 reject 所有 pending。多连接场景(`reverse` 多实例拨入)取第一个活跃 ws 发请求。
- `onebot_adapter/onebot/api.py` — `OneBotApi`:通过注入的 `WsApiTransport` 在同一条 OneBot WS 连接上发送所有 API 调用。`send_group_msg`/`get_login_info`/`get_msg` 等方法封装常用 action,上层 `relay/hermes_ws`、`webui/routes`、`name_resolver`、`parser` 统一调用。**不再有独立的 HTTP API 端口/配置**(历史字段 `onebot_http_api`/`onebot_access_token` 已删除)。
- `onebot_adapter/relay/protocol.py` — wire protocol between adapter service and plugin. `NormalizedEvent`, `FilteredEvent`, `CommandInfo` dataclasses. All frames are JSON with `type` + `v` fields. `NormalizedEvent.real_seq` carries the NapCat per-group sequence (empty when absent).
- `onebot_adapter/relay/hermes_ws.py` — `HermesRelayServer`: WS endpoint the plugin connects to. Stores the slash-command registry pushed by the plugin (`commands_snapshot` frame) and the Hermes session-isolation mode (`hermes_mode_report` frame → `_store_hermes_mode`). `is_known_command()` / `canonical_command_name()` feed the parser's /command filter. Ring buffer (`_RING_BUFFER_SIZE=50`, `_RING_BUFFER_MAX_AGE=30s`) replays recent text-only events to reconnecting plugins; entries older than 30s are skipped **and slash commands (text starting with `/`) are never buffered** — both prevent stale `/restart` commands from creating an infinite restart loop across gateway restarts. No binary frames are sent on the /hermes WS (media is URL passthrough). **群聊排队**：`_enqueue_or_broadcast` 按 `_hermes_group_sessions_per_user` + `event_queue_enabled` 判定排队策略；`_handle_idle` 处理插件发来的 idle 帧；`_watchdog_loop` 兜底超时。详见下方"群聊消息排队"段。
- `onebot_adapter/onebot/ws_reverse.py` / `ws_forward.py` — OneBot transport (reverse WS server / forward WS client). Both call `parse_event()` with the same params and handle `FilteredEvent` via an `on_filtered` callback. Both register/unregister their WS with `WsApiTransport` on connect/disconnect, and call `transport.on_text(raw)` at the top of `_handle_text` to intercept API response frames before the parser sees them.
- `onebot_adapter/hermes_plugin/adapter.py` — `OneBotAdapter(BasePlatformAdapter)` runs inside the Hermes gateway. Imports from `gateway.*` and `hermes_cli.*` are lazy (try/except) so the file is importable standalone. On connect/reconnect it pushes a `commands_snapshot` frame built from `hermes_cli.commands.COMMAND_REGISTRY` + `hermes_cli.plugins.get_plugin_commands()`. `_handle_event` sets `_current_group_id` for tool-layer seq resolution and calls `_maybe_register_idle_callback` to register a `register_post_delivery_callback` hook for shared-group queueing (see "群聊消息排队" section). Media is received as URL placeholders in event text (no binary frames, no `cache_*_from_bytes` imports). Outbound sends pass file paths/URLs as strings in the JSON `send` frame — no binary upload, no `send_bytes`.
- `onebot_adapter/hermes_plugin/onebot_tools.py` — OneBot API tools. Tool schemas use `real_seq` (the prefix-shown group sequence); `onebot_get_group_msg_history` keeps `message_seq` (actually accepts NapCat short `message_id`, **not** `real_seq`). `onebot_get_forward_msg` keeps `message_id` (forward id, not a sequence). Tools pass `real_seq` + `group_id` to the adapter; the adapter's `_handle_api_call` intercepts and converts via SeqMap.
- `onebot_adapter/onebot/seq_map.py` — `SeqMap`: **global FIFO** `real_seq → message_id` ring buffer (configurable via `seq_map_size`, default 4500, aligned with NapCat's 5000-entry `MessageUnique` LRU). Populated in `ws_reverse`/`ws_forward` `_handle_text` **before** parser gating (all messages, not just triggered ones) and in `HermesRelayServer._handle_send` for bot's own outgoing messages (via `get_msg` to fetch `real_seq`). Used by `HermesRelayServer._resolve_seq_params` to convert LLM-supplied `real_seq` back to `message_id` for OneBot API calls. On miss, passes through `real_seq` as `message_id` (go-cqhttp/Lagrange compat).
- `onebot_adapter/webui/routes.py` — REST API + static SPA hosting. Static dir search order: package `webui/static/` → `frontend/dist/` → site-packages. `/api/send` and `/api/groups` call `api.call()` / `send_group_msg` which ride the WS API transport.

## Conventions and gotchas

- **Hermes host imports are optional.** `hermes_plugin/adapter.py` and `onebot_tools.py` wrap `from gateway.*` / `from hermes_cli.*` / `from tools.registry` in try/except. When unavailable, base classes fall back to `object` and helper functions to no-ops. Tests in `test_adapter_protocol.py` skip entirely if Hermes isn't importable (expects `$HERMES_AGENT_DIR`, default `/home/alec/.hermes/hermes-agent`). When Hermes **is** installed, `Platform("onebot")` requires the platform to be registered first (the test module calls `register(ctx)` at import time to handle this); the 8 tests in this file exercise the plugin WS protocol.
- **`asyncio_mode = "auto"`** — async test functions need no `@pytest.mark.asyncio` decorator. Just write `async def test_x():`.
- **Config hot-reload.** `ConfigStore.update()` notifies listeners via `store.on_change(cb)`. Async callbacks are scheduled with `create_task`. Components implement `update_config(new_cfg)` to pick up changes without rebuilding. When adding a new config field that components must react to, wire it in `AdapterService._on_config_change`.
- **Per-group config pattern.** `GroupConfig` fields use `None` = "follow global". Always add a `config.resolve_<field>(group_id)` helper and call it in `parser.py` rather than branching on group config directly.
- **`pyproject.toml` `package-data`** includes `webui/static/**/*` and `hermes_plugin/*.yaml`. If you add a new static asset subdirectory or plugin yaml, update `[tool.setuptools.package-data]`.
- **Voice transcoding is removed.** The adapter no longer downloads or converts voice messages. Voice (and all media) URLs are rendered as text placeholders — the LLM fetches them on demand via code execution.
- **`test_adapter_protocol.py`** inserts `$HERMES_AGENT_DIR` into `sys.path` at import time. If Hermes lives elsewhere, set `HERMES_AGENT_DIR` before running that file.

## /command filter

Implemented across `config.py` (permission model), `parser.py` (`_check_command_filter` + `_extract_command_name`), `relay/protocol.py` (`FilteredEvent`, `CommandInfo`), `hermes_plugin/adapter.py` (`_collect_commands` → `commands_snapshot`), `relay/hermes_ws.py` (`_store_commands`, `send_reject_message`). Permission levels: `everyone` / `admin` / `disabled` / unconfigured (passthrough). Filtering runs **before** media download. Denied commands return `FilteredEvent`; the service sends the reject message via the OneBot HTTP API and does not forward to Hermes.

## 群聊消息排队（shared 会话串行化）

防止 shared 群聊中多个群成员的消息互相打断 agent 当前任务。**只在 Hermes `group_sessions_per_user=false`（全群共享 session）且适配器 `event_queue_enabled=true` 时生效**；per_user 模式每人独立 session，无需排队。

**机制**：适配器侧 `HermesRelayServer` 维护 per-group busy 槽 + FIFO 队列；插件侧利用 Hermes 已有的 `register_post_delivery_callback` hook（base.py:3919），在 shared 群聊每轮处理完成后发 `{"type":"idle","v":1,"chat_id":"group:<gid>","group_id":"<gid>"}` 帧给适配器，适配器 dequeue 下一条。

**判定规则**（`HermesRelayServer._enqueue_or_broadcast`）：
- 私聊：直接广播，不排队
- Hermes 隔离群成员(``_hermes_group_sessions_per_user=True``)：直接广播，不排队
- 适配器总开关关闭(``event_queue_enabled=False``)：直接广播，不排队
- 以上条件全部不满足(共享 + 开关开)：
  - 群未 busy → 标记 busy（记录 user_id + 时间戳），广播
  - 群 busy → **一律入队** `self._queues[gid]`（FIFO,包括 busy 用户自身）
  - 出队时连续同用户消息自动合并为一条（`\n\n` 拼接 text）
- `/` 开头的消息：**始终绕过排队直接广播**（与 ring buffer 跳过 /command 同思路）

**插件侧判定**（`hermes_plugin/adapter.py::_maybe_register_idle_callback`）：读 `self.config.extra.get("group_sessions_per_user", True)`——与 `BasePlatformAdapter.handle_message`（base.py:4606）完全一致（Hermes 在 `_create_adapter` 时通过 `config.extra.setdefault("group_sessions_per_user", self.config.group_sessions_per_user)` 把顶层值注入 platform extra，run.py:8355-8363）。只有 `group_sessions_per_user=False` 且 chat_id 是群聊形式时才注册 post_delivery callback。callback 用 `generation` 关联当前 gateway run，防 stale run 错误触发 idle。

**看门狗**（`_watchdog_loop`）：周期扫 `_busy_groups`，超过 `event_queue_idle_timeout`（默认 300s，可配置）未收到 idle 帧则强制清空 busy 并派发下一条。兜底 plugin 崩溃 / idle 帧丢失导致永久卡死。

**清理时机**：最后一个 plugin client 断开时清空所有 busy/queue（无人发 idle，留着只会等看门狗超时）；`stop()` 取消 watchdog 并清空状态；ring buffer replay 开始时清空 queue/busy（重新建立状态）。

**与 ring buffer 的关系**：push_event 始终写 ring buffer（用于 plugin 重连重放）；replay 时走 `_enqueue_or_broadcast` 重新评估排队状态，避免重连瞬间把多条 shared 群消息一次性推给 plugin。

**配置**（`config.py`，WebUI「连接管理」页可调）：
- `event_queue_enabled`（默认 True）：排队总开关，Hermes 不隔离群成员时是否排队
- `event_queue_max_per_chat`（默认 50）：单群队列上限，超限拒绝入队
- `event_queue_idle_timeout`（默认 300.0 秒）：看门狗超时阈值

## Config file

Lives at `~/.onebot_adapter/config.json` (override with `ONEBOT_ADAPTER_CONFIG`). The WebUI reads/writes it via `GET/PUT /api/config`. `ConfigStore.patch(**changes)` validates + notifies listeners + you must `save_config()` to persist.

## 工具管理（Hermes 工具集）

OneBot 平台的工具集配置通过适配器 WebUI 管理（`/tools` 页），而非 `hermes tools` TUI —— Hermes host 端的 `_get_enabled_platforms()` 是硬编码白名单，不含插件平台。适配器直接读写 Hermes 的 `config.yaml`：

- **读写桥**：`onebot_adapter/hermes_config.py`。用 `ruamel.yaml` round-trip 模式保留用户注释和顶层 key 顺序；原子写（tmp + `os.replace`）。
- **配置位置**：`<hermes_install_dir>/config.yaml` 的 `platform_toolsets.onebot` + `known_plugin_toolsets.onebot`。
- **工具集列表来源**：WebUI 通过 `list_available_toolsets()` 获取可配置工具集。**优先用 Hermes 自带 venv 的 Python 跑子进程**（`_find_venv` 检测 `hermes-agent/venv/bin/python`），import `hermes_cli.tools_config` + `toolsets` 输出 JSON。这彻底绕开适配器自身 Python 环境与 Hermes 依赖不匹配的问题（如 PyYAML 只在 Hermes venv 里）。venv 不存在时 fallback 到 `sys.path` 方案（pip 安装场景）。import 失败时返回 `{"error": "hermes not importable", "detail": "..."}`，前端显示 detail 字段辅助诊断。
- **API 端点**：`GET /api/hermes_tools`（读当前状态）、`PUT /api/hermes_tools`（写 `platform_toolsets.onebot`，body 含 `toolsets`/`mcp_servers`/`no_mcp`）、`POST /api/hermes_tools/reset`（删 `platform_toolsets.onebot` 回到默认）。
- **首次安装**：`installer.install()` 末尾调用 `write_platform_toolsets(default_onebot_toolsets())`，默认启用核心工具集（减去 `_DEFAULT_OFF_TOOLSETS`）+ `onebot` 插件 toolset。
- **修改后需重启 Hermes 网关生效**（适配器只写文件，不触发热重载）。
- **MCP 服务器**：WebUI 只控制 OneBot 平台的 MCP 白名单（写入 `platform_toolsets.onebot` 的 MCP server 名），不控制 MCP 的全局 `enabled` 标志（由 Hermes 端 `mcp_servers.<name>.enabled` 管理）。`no_mcp` sentinel 写入后向 OneBot 平台屏蔽全部 MCP。
- **toolset key 约定**：插件在 `onebot_tools.py` 中用 `TOOLSET = "onebot"`（不是 `"hermes-onebot"`），这是 `toolsets.py:700` 自动生成路径按 `e.toolset == platform_name` 匹配的隐含约定。改名会导致 `resolve_toolset("hermes-onebot")` 走自动生成路径，返回 `_HERMES_CORE_TOOLS` + 28 个 QQ 工具。
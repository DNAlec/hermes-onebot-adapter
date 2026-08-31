# AGENTS.md

Compact guide for OpenCode sessions working in this repo. Read this before editing.

User-facing docs and the REST API live under [docs/README.md](docs/README.md). Update [CHANGELOG.md](CHANGELOG.md) before tagging a release. Queue/idle protocol changes require reinstalling the bundled Hermes plugin.

## What this is

A standalone Python service (`onebot_adapter/`) that bridges OneBot 11 (NapCat/go-cqhttp) with a Hermes Agent plugin (`onebot_adapter/hermes_plugin/`). Three aiohttp apps share one process and bind separate ports:

| Port | App | Purpose |
|------|-----|---------|
| 18800 | OneBot reverse WS | OneBot dials in here (`/onebot`); **双向**——同一条 WS 既推事件也接受 API 调用 |
| 18810 | Hermes plugin WS | Plugin connects here (`/hermes?token=`) |
| 18820 | WebUI + REST API | SPA + versioned `/api/v1/*`; public health at `/api/v1/health` |

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

`ruff check .` then `pytest -q`. Both must pass before committing. This is the local gate — there is no CI for PR checks yet (the only workflow is `publish.yml` for PyPI releases).

## Releasing

Tag a version and push — CI handles the rest:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

`v*` tags trigger `.github/workflows/publish.yml`: check-out full history → install dev/build dependencies → validate the tag against the setuptools-scm version → `ruff check .` → `pytest -q -rs` → build/audit the frontend via `scripts/build_frontend.sh` → build/check distributions → `pypa/gh-action-pypi-publish`. The standalone runner skips the 11 Hermes protocol tests because Hermes is not installed; run the local gate in a Hermes environment before tagging. Uses PyPI Trusted Publishing (OIDC) — no token to manage. Update `CHANGELOG.md` before tagging.

After PyPI publishes, a maintainer running `pipx upgrade hermes-onebot-adapter` will receive the new version. A local source install must rebuild the frontend first (`./scripts/build_frontend.sh`).

## Architecture map

```
NapCat ──反向WS──▶  adapter service  ──WS──▶  Hermes plugin ──▶ Hermes Agent
        (OneBot11)  (独立进程)        (token)    (BasePlatformAdapter)
         双向(事件+API)                纯JSON(无二进制帧)
```

Key modules:
- `onebot_adapter/app.py` — `AdapterService` composes the three aiohttp apps and lifecycle. Entry: `run()`. Creates the shared `WsApiTransport` and injects it into `OneBotApi` + `OneBotReverseServer` + `OneBotForwardClient`. `_probe_self_id` now fires on OneBot WS connect (`_on_onebot_connect`) instead of at startup, since it needs an active WS to call `get_login_info`.
- `onebot_adapter/config.py` — `AdapterConfig` dataclass + `ConfigStore` (thread-safe, hot-reload listeners). Config persists to `~/.onebot_adapter/config.json` (or `$ONEBOT_ADAPTER_CONFIG`). Per-group overrides via `GroupConfig`. **Resolve per-group values through `config.resolve_*(group_id)`**, never read `group_*` fields directly in parser/handlers — group config `None` means "fall back to global".
- `onebot_adapter/onebot/parser.py` — `parse_event()` reduces OneBot 11 events to `NormalizedEvent`. Handles @bot trigger gating, keyword triggers, merged-forward expansion, reply context, /command filtering. All group chats get `chat_id = group:<gid>` (no `:user:` suffix — Hermes' `group_sessions_per_user` is the sole source of session isolation truth, reported by the plugin via `hermes_mode_report` frames). Returns `FilteredEvent` when a /command is denied with a reject reply; returns `DroppedEvent` for silent drops (`user_filter` / `mention` / `empty`). Callers must check `isinstance`. Heartbeats and unhandled notices still return `None`. No longer takes `media_max_bytes`/`media_max_count` params (those config fields have been removed). Group sender prefix shows `#real_seq` (per-group sequence from NapCat); falls back to `#message_id` when `real_seq` absent (go-cqhttp/Lagrange). DMs have no prefix. Reads `media_delivery_mode` from config; in `cache` mode (default) renders placeholders without URLs (``[图1]``) and populates `NormalizedEvent.media_items` with `MediaItem` entries for the plugin to download; in `passthrough` mode renders URLs inline (``[图1](https://...)``) and leaves `media_items` empty. **notice 事件**：`parse_event()` 分派 `post_type=notice` 到 `_parse_notice_event()`，处理戳一戳（`notify/poke`，仅 bot 被戳，`target_id == self_id`）和群成员进退群（`group_increase`/`group_decrease`，`user_id != self_id`），合成中文系统提示文本（如 ``[系统] 用户 张三(12345) 戳了戳你``），设 `NormalizedEvent.is_system_notice=True`，复用 `event="message"` 协议路径（与普通消息一样进排队和 ring buffer）。戳一戳走群/DM 用户过滤，成员变动不过滤。开关由 `config.resolve_notify_poke_enabled(group_id)` / `config.resolve_notify_member_change_enabled(group_id)` 控制，默认关闭，GroupConfig 可 per-group 覆盖。退群区分 `leave`（退出了群聊）和 `kick`（被管理员移出了群聊）。
- `onebot_adapter/onebot/ws_api.py` — `WsApiTransport`: OneBot 11 API 调用的 WebSocket 传输层。用 `echo` 字段做请求-响应关联(`dict[echo, asyncio.Future]`)。`register(ws)`/`unregister(ws)` 在 WS 连接建立/断开时由 `ws_reverse`/`ws_forward` 调用;`on_text(raw)` 在 `_handle_text` 开头先被调用,命中 pending echo 的响应帧被拦截并 resolve 对应 future(返回 True),否则返回 False 交给 parser。`request(action, params, timeout)` 分配 uuid4 echo,`ws.send_json`,await future。无活跃连接抛 `RuntimeError`;WS 断开 reject 所有 pending。多连接场景(`reverse` 多实例拨入)取第一个活跃 ws 发请求。
- `onebot_adapter/onebot/api.py` — `OneBotApi`:通过注入的 `WsApiTransport` 在同一条 OneBot WS 连接上发送所有 API 调用。`send_group_msg`/`get_login_info`/`get_msg` 等方法封装常用 action,上层 `relay/hermes_ws`、`webui/routes`、`name_resolver`、`parser` 统一调用。`upload_group_file`、`upload_private_file` 和 `create_flash_task` 按 `file_upload_timeout` 等待（默认 600s，范围 30–600，可热更新）；群上传超时后短轮询 `get_group_msg_history`，仅把本次时间窗内 bot 发送且文件名/可用大小唯一匹配的文件消息视为成功，闪传超时则无法确认结果，两者均抛 `UploadOutcomeUnknownError`，relay 以 `retryable=false` 返回，防止未知结果被自动重试。NapCat 错误文本按 `msg` → `message` → `wording` 读取。**不再有独立的 HTTP API 端口/配置**(历史字段 `onebot_http_api`/`onebot_access_token` 已删除)。
- `onebot_adapter/relay/protocol.py` — wire protocol between adapter service and plugin. `NormalizedEvent`, `FilteredEvent`, `DroppedEvent`, `CommandInfo`, `MediaItem` dataclasses. `FilteredEvent`/`DroppedEvent` are process-internal (never on the wire). All frames are JSON with `type` + `v` fields. `NormalizedEvent.real_seq` carries the NapCat per-group sequence (empty when absent). `NormalizedEvent.media_items` carries one `MediaItem` per media segment when `media_delivery_mode == "cache"` (empty in `passthrough` mode). `NormalizedEvent.is_system_notice` marks synthetic notice events (戳一戳/进退群) so the plugin sets `MessageEvent.internal=True` to bypass text debounce. `ready_message` carries `media_delivery_mode` and `file_upload_timeout` so the plugin can apply the current media strategy and RPC wait limit.
- `onebot_adapter/relay/hermes_ws.py` — `HermesRelayServer`: WS endpoint the plugin connects to. The default/`role=consumer` connection is the single event consumer; short-lived cron delivery uses `role=rpc` and never receives events or affects queue state. Stores the slash-command registry pushed by the plugin (`commands_snapshot` frame) and the Hermes session-isolation mode (`hermes_mode_report` frame → `_store_hermes_mode`). `is_known_command()` / `canonical_command_name()` feed the parser's /command filter. Ring buffer (`_RING_BUFFER_SIZE=50`, `_RING_BUFFER_MAX_AGE=30s`) replays recent unacknowledged text events to reconnecting plugins; the plugin returns `event_ack` after processing. Entries older than 30s are skipped **and slash commands (text starting with `/`) are never buffered**. No binary frames are sent on the /hermes WS. `update_config` broadcasts a fresh `ready` frame when `media_delivery_mode` or `file_upload_timeout` changes so the plugin can switch strategy without reconnecting. **群聊排队**：`_enqueue_or_broadcast` 按 `_hermes_group_sessions_per_user` + `event_queue_enabled` 判定排队策略；`_handle_idle` 处理插件发来的 idle 帧；`_watchdog_loop` 兜底超时。详见下方"群聊消息排队"段。
- `onebot_adapter/onebot/handler.py` — shared OneBot text pipeline. `OneBotHandler` owns WS API response interception, SeqMap population, `parse_event`, filtering and event dispatch. `OneBotEventDispatcher` gives each transport a bounded 1024-frame FIFO with one ordered event worker; the receive loop intercepts API responses synchronously before queueing ordinary events so an event awaiting an API call on the same WS cannot deadlock. A full queue drops and rate-limits an explicit error log instead of creating unbounded tasks.
- `onebot_adapter/onebot/ws_reverse.py` / `ws_forward.py` — OneBot transports. Each constructs a shared `OneBotHandler` + `OneBotEventDispatcher`, registers/unregisters its WS with `WsApiTransport`, and feeds text frames through the dispatcher. Event parsing is ordered while correlated API response frames stay on the immediate receive-loop fast path.
- `onebot_adapter/_async_utils.py` — small async helpers (`log_task_exception` etc.) used by `ConfigStore.update` and other `create_task` call sites to surface background-task exceptions.
- `onebot_adapter/logging_utils.py` / `onebot_adapter/onebot/log_format.py` — bounded/redacted DEBUG serialization and message-flow logs. Console/WebUI preview copies use the non-propagating `onebot_adapter.onebot.message_preview` logger; persistent message copies use `onebot_adapter.file` according to `log_file_message_mode` (`none`/`preview`/`full`, default `preview`). Candidate messages that do not reach Hermes log `丢弃 -- reason=` at DEBUG (`user_filter`/`mention`/`command`/`blacklist`/`rate_limit`/`empty`) without bodies. Successful OneBot `send_*` / `upload_*_file` calls emit `发送 ->` from `OneBotApi.call`. WebUI `GET /api/v1/logs` is the 500-line memory buffer; `GET /api/v1/logs/file` tails `adapter.log` and `GET /api/v1/logs/file/download` downloads it.
- `onebot_adapter/update_check.py` — `check_for_updates()`: queries GitHub tags API, compares against `__version__` (strips setuptools-scm `.dev*`/`.dirty` suffixes), caches result 1h (errors 5min). Exposed via `GET /api/v1/update_check`.
- `onebot_adapter/rate_limit.py` — persistent global/group/user inbound rate limiter. Buckets live in `~/.onebot_adapter/rate_limit.sqlite3` (beside the configured `config.json`) and accepted multi-scope consumption is committed atomically before relay delivery. Sliding windows persist timestamps; token buckets persist tokens + wall-clock update time so downtime naturally refills/expires quota. `rate_limit_storage_failure_mode` selects `memory_fallback` (default, ordered pending operations replay after recovery) or `reject`. Disabling the master switch preserves buckets. WebUI query/reset routes call `quota()`/`reset()`; reset is scoped and audited.
- `onebot_adapter/hermes_plugin/adapter.py` — `OneBotAdapter(BasePlatformAdapter)` runs inside the Hermes gateway. Imports from `gateway.*` and `hermes_cli.*` are lazy (try/except) so the file is importable standalone. On connect/reconnect it pushes a `commands_snapshot` frame built from `hermes_cli.commands.COMMAND_REGISTRY` + `hermes_cli.plugins.get_plugin_commands()`. Shared-group queueing fires `idle` from `on_processing_complete` only when the session has no pending/debounce follow-up (Hermes pops `register_post_delivery_callback(generation=None)` without running it). Sets `MessageEvent.internal` from `NormalizedEvent.is_system_notice` so synthetic notice events (戳一戳/进退群) bypass Hermes' text debounce. Reads `media_delivery_mode` and `file_upload_timeout` from the `ready` frame; the group upload RPC wait limit is the configured timeout plus a 40s confirmation margin，私聊和闪传上传增加 30s 余量。RPC results whose futures belong to a worker-thread event loop are resolved through `call_soon_threadsafe`. In `cache` mode (default) calls `_cache_media_items` which uses `cache_image_from_url`/`cache_audio_from_url`/`cache_video_from_bytes`/`cache_document_from_bytes` from `gateway.platforms.base` to download media to `~/.hermes/cache/` and fills `MessageEvent.media_urls`/`media_types` with local paths; in `passthrough` mode media URLs stay inline in the text as placeholders and `media_urls` is empty. File segments without a URL are always skipped (LLM uses `onebot_get_file` tool). Outbound sends pass file paths/URLs as strings in the JSON `send` frame — no binary upload, no `send_bytes`.
- `onebot_adapter/hermes_plugin/onebot_tools.py` — canonical 100-tool OneBot catalog and handlers. Hermes registration reads sparse `plugins.entries.onebot.tool_policies` from Hermes `config.yaml`, omits `registered=false` entries, and wraps enabled handlers with `everyone`/`admin` checks; registration changes require a Hermes restart. **12 个工具默认隐藏**（`_DEFAULT_HIDDEN_TOOL_NAMES`：4 个原有工具 + 8 个闪传/文件集工具）；闪传工具仅 Windows 版客户端可用且涉及本机文件读取/上传，需在 WebUI 显式启用。HTTP calls use the complete raw catalog through `_api_caller`, so policy never removes or restricts automation routes. `admin` distinguishes global admins from group admins via `NormalizedEvent.is_global_admin`: group admins are limited to the current group, while account/cross-group operations require a global admin. Tool schemas use `real_seq`; essence/todo/emoji actions join the existing adapter-side SeqMap conversion.
- `onebot_adapter/onebot/seq_map.py` — `SeqMap`: **global FIFO** `real_seq → message_id` ring buffer (configurable via `seq_map_size`, default 4500, aligned with NapCat's 5000-entry `MessageUnique` LRU). Populated on the receive-loop fast path (`OneBotEventDispatcher.dispatch` / `record_inbound_seq`) **before** the bounded event queue, so overflow drops still keep mappings, and in `HermesRelayServer._handle_send` for bot's own outgoing messages (via `get_msg` to fetch `real_seq`). Used by `HermesRelayServer._resolve_seq_params` to convert LLM-supplied `real_seq` back to `message_id` for OneBot API calls. On miss, passes through `real_seq` as `message_id` (go-cqhttp/Lagrange compat).
- `onebot_adapter/webui/routes.py` — versioned management API + static SPA hosting. All business routes live under `/api/v1`; `/api/v1/health`, `/api/v1/auth/login`, and `/api/v1/openapi.json` are public, management routes use a signed WebUI session, and `/api/v1/tools*` uses the separate automation API key. Unknown `/api/*` paths return JSON 404 instead of the SPA.
- `onebot_adapter/webui/tool_api.py` — typed automation facade. Builds strict Pydantic request models from the canonical tool catalog, registers one static `POST /api/v1/tools/<tool_name>` route per tool, exposes `GET /api/v1/tools`, validates local file references against configured roots, and dispatches through the active OneBot WS transport.

## Conventions and gotchas

- **Hermes host imports are optional.** `hermes_plugin/adapter.py` and `onebot_tools.py` wrap `from gateway.*` / `from hermes_cli.*` / `from tools.registry` in try/except. When unavailable, base classes fall back to `object` and helper functions to no-ops. Tests in `test_adapter_protocol.py` skip entirely if Hermes isn't importable (expects `$HERMES_AGENT_DIR`, default `/home/alec/.hermes/hermes-agent`). When Hermes **is** installed, `Platform("onebot")` requires the platform to be registered first (the test module calls `register(ctx)` at import time to handle this); the 11 tests in this file exercise the plugin WS protocol.
- **`asyncio_mode = "auto"`** — async test functions need no `@pytest.mark.asyncio` decorator. Just write `async def test_x():`.
- **Config hot-reload.** `ConfigStore.update()` notifies listeners via `store.on_change(cb)`. Async callbacks are scheduled with `create_task`. Components implement `update_config(new_cfg)` to pick up changes without rebuilding. When adding a new config field that components must react to, wire it in `AdapterService._on_config_change`.
- **Automation API.** Disabled by default via `automation_api_enabled`. The single full-privilege key is generated/rotated in WebUI or with top-level CLI flags; only its SHA-256 digest (`automation_api_key_hash`) is persisted. Never expose the digest through `GET /api/v1/config`, accept the key in a query string, or bypass `automation_upload_allowed_roots` for HTTP-supplied local media paths.
- **Per-group config pattern.** `GroupConfig` fields use `None` = "follow global". Always add a `config.resolve_<field>(group_id)` helper and call it in `parser.py` rather than branching on group config directly.
- **`pyproject.toml` `package-data`** includes `webui/static/**/*` and `hermes_plugin/*.yaml`. If you add a new static asset subdirectory or plugin yaml, update `[tool.setuptools.package-data]`.
- **Voice transcoding is removed.** The adapter no longer downloads or converts voice messages. Media delivery is controlled by `media_delivery_mode` config field: `cache` (default, plugin downloads to `~/.hermes/cache/` via `cache_image_from_url` etc., fills `MessageEvent.media_urls` with local paths; cache failures skip the media but keep the empty `[图N]` placeholder) or `passthrough` (URLs as text placeholders). File segments without a URL are always skipped — the LLM uses the `onebot_get_file` tool to fetch them by `file_id`.
- **`test_adapter_protocol.py`** inserts `$HERMES_AGENT_DIR` into `sys.path` at import time. If Hermes lives elsewhere, set `HERMES_AGENT_DIR` before running that file.

## /command filter

Implemented across `config.py` (permission model), `parser.py` (`_check_command_filter` + `_extract_command_name`), `relay/protocol.py` (`FilteredEvent`), `hermes_plugin/adapter.py` (`_collect_commands` → `commands_snapshot`), `relay/hermes_ws.py` (`_store_commands`, `send_reject_message`). Permission levels: `everyone` / `admin` / `disabled` / unconfigured (passthrough). Filtering runs **before** media download. Denied commands return `FilteredEvent`; the service sends the reject message over the active OneBot WS API channel and does not forward to Hermes.

## 出站消息正则过滤

Hermes → OneBot 发送路径上的文本过滤，实现于 `outbound_filter.py` + `config.resolve_outbound_filter_*` + `HermesRelayServer._drop_filtered_send` / `_drop_filtered_api_call`。命中 `re.search` 后不调用 OneBot，向插件回 `result` 成功（`data.filtered=true`）以免 Gateway 重试。覆盖 `send` 帧正文/caption 以及 `send_msg`/`send_group_msg`/`send_private_msg` 的 text 段。空文本、`send_direct_message`、HTTP 自动化 API 不过滤。群配置 `None`=跟随全局，patterns 整表覆盖。

## 群聊消息排队（shared 会话串行化）

防止 shared 群聊中多个群成员的消息互相打断 agent 当前任务。**只在 Hermes `group_sessions_per_user=false`（全群共享 session）且适配器 `event_queue_enabled=true` 时生效**；per_user 模式每人独立 session，无需排队。

**机制**：适配器侧 `HermesRelayServer` 维护 per-group busy 槽 + FIFO 队列；插件侧在 `on_processing_complete` 里，仅当该 session 没有 pending/debounce 后续时发 `{"type":"idle","v":1,"chat_id":"group:<gid>","group_id":"<gid>"}` 帧。适配器把 idle 当作 session 空闲信号并 dequeue 下一条。同一发送者在 busy 且队列为空时直推（喂给 Hermes 当前 session 的 redirect/steer/pending），**不再**额外计数。

**判定规则**（`HermesRelayServer._enqueue_or_broadcast`）：
- 私聊：直接广播，不排队
- Hermes 隔离群成员(``_hermes_group_sessions_per_user=True``)：直接广播，不排队
- 适配器总开关关闭(``event_queue_enabled=False``)：直接广播，不排队
- 以上条件全部不满足(共享 + 开关开)：
  - 群未 busy → 标记 busy（记录 user_id + 时间戳），广播
  - 群 busy 且发送者 == 当前 busy 用户且队列为空 → 直接广播（刷新 busy 时间戳，不入队；仍算同一 session）
  - 群 busy 其他情况 → 入队 `self._queues[gid]`（FIFO,包括 busy 用户自身；队列非空时 busy 用户也不能插队）
  - 出队时连续同用户消息自动合并为一条（`\n\n` 拼接 text）
- `/` 开头的消息：**始终绕过排队直接广播**（与 ring buffer 跳过 /command 同思路）
- `/new`、`/reset`：`event_queue_clear_on_session_reset=true`（默认）时先清空当前群待处理队列，再广播给 Hermes；不影响当前 busy turn
- `/clean`：`event_queue_clean_command_enabled=true`（默认）时由适配器本地清空当前群待处理队列并释放 busy，不广播给 Hermes

**插件侧判定**（`hermes_plugin/adapter.py::on_processing_complete`）：读 `self.config.extra.get("group_sessions_per_user", True)`——与 `BasePlatformAdapter.handle_message` 完全一致。只有 `group_sessions_per_user=False`、chat_id 是群聊、不是 `/` 命令、且该 session 没有 `_pending_messages` / 文本 debounce 后续时才发 idle。

**看门狗**（`_watchdog_loop`）：周期扫 `_busy_groups`，超过 `event_queue_idle_timeout`（默认 300s，可配置）未收到 idle 帧则强制清空 busy 并派发下一条。兜底 plugin 崩溃 / idle 帧丢失导致永久卡死。

**`/stop` idle 丢失补救**（`_delayed_stop_cleanup`）：Hermes gateway 的 `/stop`、`/new`、`/reset` 通过 bump generation 中断当前 turn,导致 stale run 的 `post_delivery_callback` 被 pop 而不触发（`run.py:11099-11112`），adapter 收不到 idle 帧 → 队列卡死。适配器在 broadcast 这些命令后 schedule 一个 `_STOP_IDLE_DELAY`（3s）延迟任务，用 `(busy_user, epoch)` 识别槽位（send 刷新时间戳不会取消清理）：若 gateway 正常发 idle 则 epoch 已变,延迟任务 no-op；若没发 idle 则 force-clear。看门狗（300s）是最终兜底。

**清理时机**：`/clean` 清空当前群队列并释放 busy；最后一个 plugin client 断开时清空所有 busy/queue（无人发 idle，留着只会等看门狗超时）；`stop()` 取消 watchdog 并清空状态；ring buffer replay 开始时清空 queue/busy（重新建立状态）。

**与 ring buffer 的关系**：push_event 始终写 ring buffer（用于 plugin 重连重放）；replay 时走 `_enqueue_or_broadcast` 重新评估排队状态，避免重连瞬间把多条 shared 群消息一次性推给 plugin。

**配置**（`config.py`，WebUI「聊天配置」页可调）：
- `event_queue_enabled`（默认 True）：排队总开关，Hermes 不隔离群成员时是否排队
- `event_queue_max_per_chat`（默认 50）：单群队列上限，超限拒绝入队
- `event_queue_idle_timeout`（默认 300.0 秒）：看门狗超时阈值
- `event_queue_clear_on_session_reset`（默认 True）：使用 `/new`、`/reset` 时清空当前群待处理队列
- `event_queue_clean_command_enabled`（默认 True）：启用适配器本地 `/clean` 清队列并释放 busy

## Config file

Lives at `~/.onebot_adapter/config.json` (override with `ONEBOT_ADAPTER_CONFIG`). The WebUI reads/writes it via `GET/PATCH /api/v1/config`. `ConfigStore.patch(**changes)` validates + notifies listeners + you must `save_config()` to persist. API handlers that need persistence must save the candidate config successfully before `store.update()` so disk failures cannot leave memory ahead of disk.

## 工具管理（Hermes 工具集）

OneBot 平台的工具集配置通过适配器 WebUI 管理（`/tools` 页），而非 `hermes tools` TUI —— Hermes host 端的 `_get_enabled_platforms()` 是硬编码白名单，不含插件平台。适配器直接读写 Hermes 的 `config.yaml`：

- **读写桥**：`onebot_adapter/hermes_config.py`。用 `ruamel.yaml` round-trip 模式保留用户注释和顶层 key 顺序；原子写（tmp + `os.replace`）。
- **配置位置**：`<hermes_install_dir>/config.yaml` 的 `platform_toolsets.onebot` + `known_plugin_toolsets.onebot`。
- **工具集列表来源**：WebUI 通过 `list_available_toolsets()` 获取可配置工具集。**优先用 Hermes 自带 venv 的 Python 跑子进程**（`_find_venv` 检测 `hermes-agent/venv/bin/python`），import `hermes_cli.tools_config` + `toolsets` 输出 JSON。这彻底绕开适配器自身 Python 环境与 Hermes 依赖不匹配的问题（如 PyYAML 只在 Hermes venv 里）。venv 不存在时 fallback 到 `sys.path` 方案（pip 安装场景）。import 失败时返回 `{"error": "hermes not importable", "detail": "..."}`，前端显示 detail 字段辅助诊断。
- **API 端点**：`GET /api/v1/hermes_tools`（读当前状态）、`PUT /api/v1/hermes_tools`（写 `platform_toolsets.onebot`，body 含 `toolsets`/`mcp_servers`/`no_mcp`）、`POST /api/v1/hermes_tools/reset`（删 `platform_toolsets.onebot` 回到默认）。
- **首次安装**：`installer.install()` 末尾调用 `write_platform_toolsets(default_onebot_toolsets())`，默认启用核心工具集（减去 `_DEFAULT_OFF_TOOLSETS`）+ `onebot` 插件 toolset。
- **修改后需重启 Hermes 网关生效**（适配器只写文件，不触发热重载）。
- **MCP 服务器**：WebUI 只控制 OneBot 平台的 MCP 白名单（写入 `platform_toolsets.onebot` 的 MCP server 名），不控制 MCP 的全局 `enabled` 标志（由 Hermes 端 `mcp_servers.<name>.enabled` 管理）。`no_mcp` sentinel 写入后向 OneBot 平台屏蔽全部 MCP。
- **toolset key 约定**：插件在 `onebot_tools.py` 中用 `TOOLSET = "onebot"`（不是 `"hermes-onebot"`），这是 `toolsets.py:700` 自动生成路径按 `e.toolset == platform_name` 匹配的隐含约定。改名会导致 `resolve_toolset("hermes-onebot")` 走自动生成路径，返回 `_HERMES_CORE_TOOLS` + 当前注册的 OneBot 工具。

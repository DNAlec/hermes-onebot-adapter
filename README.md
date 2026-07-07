# Hermes OneBot Adapter

OneBot 11 适配器服务 + Hermes 插件，经独立服务对接 NapCat / go-cqhttp 等 OneBot 11 实现(目前仅在NapCat下测试过)。

## 架构

```
OneBot ──WS──  适配器服务  ──WS── Hermes 插件 ── Hermes Agent
```

适配器服务承担全部 OneBot 交互；插件只与适配器服务通信，不直接接触 OneBot ；不修改 Hermes 本身的代码。

## 环境要求

- Python >= 3.11
- [pipx](https://pipx.pypa.io/)（推荐）或 pip
- ffmpeg（语音转码，可选）

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

# 插件安装 (默认从 config.json 读取 URL 和 token)
hermes-onebot-adapter install                          # 安装到 ~/.hermes
hermes-onebot-adapter install --hermes-dir /opt/hermes # 指定安装目录
hermes-onebot-adapter install --adapter-url ws://host:18810/hermes --adapter-token xxx  # 手动指定连接参数
hermes-onebot-adapter uninstall                        # 卸载
hermes-onebot-adapter uninstall --hermes-dir /opt/hermes
```

## 三端口

| 端口  | 用途 |
|------|------|
| 18800 | OneBot WS 服务端 `/onebot`（反向 WS 模式，OneBot 连接此端口；正向 WS 模式不使用） |
| 18810 | Hermes 插件 WS 服务端 `/hermes?token=`（插件连接适配器的端口） |
| 18820 | WebUI + REST API + 健康检查 (`/api/health`)（详见 [API 文档](docs/api.md)） |

## 环境变量

### 适配器服务

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ONEBOT_ADAPTER_CONFIG` | `~/.onebot_adapter/config.json` | 配置文件路径 |

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
| 连接管理 | 配置 OneBot 连接模式和 WS 地址；安装/卸载 Hermes 插件 |
| 群组管理 | 查看群列表、每群启用/禁用 Bot、群成员过滤 |
| 指令过滤 | 管理 `/` 指令的权限（所有人 / 管理员 / 禁用） |
| 工具管理 | 启停 OneBot 平台的 Hermes 工具集 |
| 高级设置 | 私聊过滤、全局管理员、序列号映射等 |

## 工具集管理

安装插件时会自动写入默认工具集配置到 `<hermes>/config.yaml`。之后可通过 WebUI 的"工具管理"页面自主启停工具集。

工具集修改后写入 Hermes 的 `config.yaml`，**需重启 Hermes 网关生效**。适配器只负责写配置文件，不触发热重载。

## OneBot API 工具

插件自带 28 个 OneBot API 工具（toolset: `onebot`），LLM 可直接调用：

- **只读**：获取群列表/成员/信息、好友列表、消息历史、合并转发内容
- **消息**：发送消息、撤回、合并转发、戳一戳、表情回应
- **管理**（需适配器管理员权限）：踢人、禁言、全员禁言、设置管理员/名片/群名、退群、处理加群/好友请求

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

被过滤的指令会通过 OneBot HTTP API 向原聊天发送拒绝消息，不会送入 Hermes 处理。指令过滤在媒体下载之前执行，避免浪费带宽。

## 群聊消息排队

适配器内置 shared 群聊消息排队机制，防止群聊中多个群成员的消息互相打断 agent 当前任务。**只在 Hermes 配置 `group_sessions_per_user: false`（全群共享 session）且适配器 `event_queue_enabled: true` 时生效**；per_user 模式每人独立 session，无需排队。

### 排队规则

| 场景 | 行为 |
|------|------|
| 私聊 | 直接转发，不排队 |
| Hermes 隔离群成员（per_user=True） | 直接转发，不排队 |
| 适配器排队总开关关闭 | 直接转发，不排队 |
| 群未 busy | 标记 busy，转发 |
| 群 busy | 入队等待（含 busy 用户自身）；出队时连续同用户消息合并为一条 |
| `/` 开头的消息 | **始终直接转发**（绕过排队） |

### Hermes 会话隔离配置

`group_sessions_per_user` 是 Hermes 顶层的唯一真相源。适配器 WebUI（连接管理页）可直接修改 Hermes `config.yaml` 的此字段，修改后需重启 Hermes 网关生效。插件连接后会上报当前值给适配器，适配器据此决定是否排队。

### idle 信号

处理完成的"idle"信号由 Hermes 插件通过 `register_post_delivery_callback` 钩子发送：每轮 agent 处理结束后插件向适配器发 `idle` 帧，适配器清空 busy 并从队列取下一条转发。若插件崩溃或 idle 帧丢失，看门狗会在超时后强制清空 busy。

### 配置项（WebUI「连接管理」页面）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `event_queue_enabled` | `true` | 排队总开关：Hermes 不隔离群成员时是否排队 |
| `event_queue_max_per_chat` | `50` | 单群队列上限，超限拒绝入队 |
| `event_queue_idle_timeout` | `300.0` | plugin 无 idle 信号的超时阈值（秒），超时强制清空 busy |

## 开发

```bash
pip install -e ".[dev]"          # 开发安装（可编辑模式 + dev 依赖）
pytest -q                        # 运行测试
ruff check .                     # 代码检查
cd frontend && npm install && npm run dev   # 前端开发 (Vite 代理到 :18820)
./scripts/build_frontend.sh      # 构建前端到 webui/static/
```

## 配置文件

适配器服务配置持久化于 `~/.onebot_adapter/config.json`（或 `ONEBOT_ADAPTER_CONFIG` 指定路径），WebUI 修改即保存。

## 技术栈

- **后端**：aiohttp（WS 服务端/客户端、HTTP API、静态托管）
- **前端**：Vue 3 + Vite + TypeScript + Vue Router
- **语音转码**：ffmpeg（异步 subprocess）
- **打包**：pyproject.toml + setuptools，`hermes-onebot-adapter` CLI entry point

## License

MIT

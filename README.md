![:name](https://count.getloli.com/@hermes-onebot-adapter?name=hermes-onebot-adapter&theme=original-new&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)
# Hermes OneBot Adapter

OneBot 11 适配器服务 + Hermes 插件，经独立服务对接 NapCat / go-cqhttp 等 OneBot 11 实现（目前仅在 NapCat 下测试过）。

## 架构

```text
NapCat ──双向 OneBot 11 WS（事件 + API）── 适配器服务 ──WS── Hermes 插件 ── Hermes Agent
```

适配器服务承担全部 OneBot 交互；事件接收和 API 调用共用同一条 OneBot WebSocket，不需要独立的 OneBot HTTP API 端口。插件只与适配器服务通信，不直接接触 OneBot，也不修改 Hermes 本身的代码。

功能说明、REST API 和维护者文档见 [文档索引](docs/README.md)。

- [快速开始](#快速开始)
- [配置流程](#配置流程)
- [Windows / WSL](#windows--wsl)
- [CLI](#cli)
- [三端口](#三端口)
- [OneBot 连接](#onebot-连接)
- [更多文档](#更多文档)

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

已有安装升级：

```bash
pipx upgrade hermes-onebot-adapter
```

**1.8.0** 含路径白名单、分端口绑定和插件 cache 媒体校验，**必须重装插件并重启 Hermes 网关**（只升级服务不够）。重装不会覆盖 WebUI「工具管理」里已保存的工具集。

```bash
pipx upgrade hermes-onebot-adapter
hermes-onebot-adapter install --hermes-dir ~/.hermes
hermes gateway restart
```

远程 NapCat 请用 `--onebot-host 0.0.0.0`，不要再用 `--host 0.0.0.0`（会把 WebUI 和 Hermes WS 一起暴露）。Hermes 装在 `/opt` 时先在配置里写入 `hermes_install_allowed_roots`。完整升级注意见 [CHANGELOG](CHANGELOG.md)。

Windows 不要用原生 Python 跑本服务，请用 WSL2，见 [Windows / WSL 部署](docs/wsl.md)。

升级不会覆盖现有适配器配置。安装器会更新 `<hermes>/plugins/onebot/` 中的插件文件；重启后可在 WebUI 仪表盘确认适配器与插件版本一致。若群聊消息一直排队、`/stop` 提示没有活跃任务，发 `/clean` 可清空队列并释放 busy，不必重启适配器。

## 配置流程

1. **启动适配器服务** — `hermes-onebot-adapter`
2. **打开 WebUI** — 浏览器访问 `http://localhost:18820`，登录后进入管理界面
3. **配置 OneBot 连接** — 在 WebUI 的「连接管理」页选择连接模式（反向 WS / 正向 WS），填写 WS 地址和 token
4. **安装 Hermes 插件** — 在 WebUI 的「连接管理」页填写 Hermes 安装目录（默认须在当前用户 `$HOME` 下；`/opt` 等非常规路径先填「额外允许的 Hermes 安装根」），点击「安装插件到 Hermes」
5. **启用插件** — `hermes plugins enable onebot-platform`
6. **重启 Hermes 网关** — `hermes gateway restart`

安装插件时，Installer 自动完成三件事：

| 操作 | 说明 |
|------|------|
| 复制插件文件 | 5 个文件 → `<hermes>/plugins/onebot/` |
| 写入环境变量 | `ONEBOT_ADAPTER_URL` + `ONEBOT_ADAPTER_TOKEN` → `<hermes>/.env` |
| 初始化工具集 | 仅当 `platform_toolsets.onebot` 不存在时写入默认值；重装保留已有配置 |

以上均需**启用插件并重启 Hermes 网关**后生效。也可：`hermes-onebot-adapter install --hermes-dir ~/.hermes`。

## Windows / WSL

适配器按 Linux 部署，原生 Windows 不可用。把适配器和 Hermes 装进 **WSL2**，NapCat / QQ 留在 Windows，反向 WS 连 `18800`。完整步骤与排障见 [Windows / WSL 部署](docs/wsl.md)。

```bash
# 在 WSL 里
pipx install hermes-onebot-adapter
hermes-onebot-adapter --onebot-host 0.0.0.0
hermes-onebot-adapter install --hermes-dir ~/.hermes
hermes plugins enable onebot-platform
hermes gateway restart
```

## CLI

```bash
# 启动服务
hermes-onebot-adapter                         # 默认三个口都绑 127.0.0.1
hermes-onebot-adapter --onebot-host 0.0.0.0   # 仅暴露 OneBot 反向 WS（远程 NapCat）
hermes-onebot-adapter --host 0.0.0.0          # 三个口都暴露，不推荐（无 TLS）
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
hermes-onebot-adapter install --hermes-dir /opt/hermes # 须先在 config.json 写入 hermes_install_allowed_roots
hermes-onebot-adapter install --adapter-url ws://host:18810/hermes --adapter-token xxx  # 手动指定连接参数
hermes-onebot-adapter uninstall                        # 卸载
hermes-onebot-adapter uninstall --hermes-dir /opt/hermes
```

自动化 API 的安全约定与环境变量见 [运维](docs/ops.md#自动化工具-api)。

## 三端口

| 端口  | 用途 |
|------|------|
| 18800 | OneBot WS 服务端 `/onebot`（反向 WS 模式，OneBot 连接此端口；正向 WS 模式不使用）。远程 NapCat 用 `--onebot-host 0.0.0.0` |
| 18810 | Hermes 插件 WS 服务端 `/hermes`（`Authorization: Bearer` 优先，仍接受 `?token=`；默认只绑回环） |
| 18820 | WebUI + REST API + 健康检查 (`/api/v1/health`)（详见 [API 文档](docs/api.md)；默认只绑回环） |

## OneBot 连接

**反向 WS（推荐）**：OneBot 主动连接适配器。NapCat 面板填写（token 与适配器 `onebot_ws_token` 一致）：

```
ws://127.0.0.1:18800/onebot?access_token=<onebot_ws_token>
```

也接受 `?token=` 或请求头 `Authorization: Bearer`（header 优先）。同一条 WS 既推事件也接受 API 调用。远程 NapCat 把适配器绑到 `--onebot-host 0.0.0.0`，URL 里的主机改成适配器可达地址。

**正向 WS**：适配器主动连接 OneBot，在 WebUI 填如 `ws://127.0.0.1:3001`。模式切换可热重载，无需重启服务。

## 更多文档

| 文档 | 内容 |
|------|------|
| [文档索引](docs/README.md) | 全部文档入口 |
| [Windows / WSL 部署](docs/wsl.md) | WSL2 安装、反向 WS、网络与排障 |
| [WebUI 与工具](docs/webui.md) | 管理页、工具集、OneBot 工具与闪传 |
| [聊天与过滤](docs/chat.md) | 准入、指令/出站过滤、媒体、notice、群聊排队 |
| [运维](docs/ops.md) | 自动化 API、限流、统计、日志、配置备份 |
| [REST API](docs/api.md) | `/api/v1/*` 与 Config 字段 |
| [CHANGELOG](CHANGELOG.md) | 版本变更与升级注意 |

配置文件：`~/.onebot_adapter/config.json`（或 `ONEBOT_ADAPTER_CONFIG`）。WebUI 修改即保存；损坏或非法时 fail-fast，不会覆盖原文件。完整字段见 [Config](docs/api.md#config-字段)。

## 开发

```bash
pip install -e ".[dev]"          # 开发安装（可编辑模式 + dev 依赖）
pytest -q                        # 运行测试
ruff check .                     # 代码检查
cd frontend && npm install && npm run dev   # 前端开发 (Vite 代理到 :18820)
./scripts/build_frontend.sh      # 构建前端到 webui/static/
```

维护者架构与模块约定见 [AGENTS.md](AGENTS.md)。发布前更新 [CHANGELOG.md](CHANGELOG.md)，再打 `vX.Y.Z` 标签。

技术栈：aiohttp（后端）+ Vue 3 / Vite / TypeScript（WebUI）；`pyproject.toml` + setuptools，CLI entry point `hermes-onebot-adapter`。

## License

MIT

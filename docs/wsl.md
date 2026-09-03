# Windows / WSL 部署

适配器按 Linux/macOS 部署，**不支持原生 Windows**（写 Hermes `config.yaml` 依赖 `fcntl.flock`）。在 Windows 上请用 **WSL2**：适配器和 Hermes 装在 Linux 发行版里，NapCat / QQ 留在 Windows，用反向 WebSocket 连过来。

没有官方 Docker 镜像。本机 Windows 用 WSL2 即可，不必再套一层 Docker Desktop。

## 拓扑

```text
Windows 本机                          WSL2（Ubuntu 等）
┌─────────────────┐                  ┌──────────────────────────┐
│ NapCat / QQ     │  反向 WS :18800  │ hermes-onebot-adapter     │
│                 │ ───────────────► │   :18800 OneBot           │
│                 │                  │   :18810 插件（只给本机）  │
│ 浏览器 WebUI    │  http :18820     │   :18820 WebUI            │
│                 │ ◄─────────────── │ Hermes Agent + onebot 插件│
└─────────────────┘                  └──────────────────────────┘
```

适配器、Hermes Agent、onebot 插件必须在**同一个 WSL 发行版**里。插件跑在 Hermes 进程内，默认连 `127.0.0.1:18810`；不要把 Hermes 装在 Windows、只把适配器放进 WSL。

## 前提

- Windows 10 21H2+ 或 Windows 11，已安装 **WSL2**（不要用 WSL1）
- 发行版建议 Ubuntu 24.04（自带 Python 3.12）。Ubuntu 22.04 默认是 3.10，需要自行安装 Python >= 3.11
- 在 **WSL 内**按 Hermes 官方方式装好 Agent，确认 `hermes` 命令可用
- Windows 上已能登录的 NapCat / QQ 客户端（闪传、文件集只在这侧可用）

Windows 11 建议打开 WSL **镜像网络**（mirrored），这样 Windows 与 WSL 共用 `127.0.0.1`，NapCat 填 localhost 即可。设置位置因版本而异，常见为「设置 → 系统 → 开发者选项 / WSL」或 `%UserProfile%\.wslconfig`：

```ini
[wsl2]
networkingMode=mirrored
```

改完后在管理员 PowerShell 执行 `wsl --shutdown`，再重新打开发行版。

## 不要做的事

- 不要把 Hermes 或适配器装到 `/mnt/c/...`（drvfs 上文件锁和权限不可靠；安装目录默认也不在 `$HOME` 下，会被白名单拒绝）
- 不要用 `--host 0.0.0.0`（会把 WebUI 和 Hermes 插件口一起暴露）。只暴露 OneBot：`--onebot-host 0.0.0.0`
- 不要用正向 WS 作为第一选择：WSL 去连 Windows 上的 NapCat 时，NAT 下 `127.0.0.1` 经常不通

## 在 WSL 里安装

以下命令都在 WSL 终端执行，不要在 PowerShell / CMD 里跑。

```bash
sudo apt update
sudo apt install -y python3 python3-venv pipx
pipx ensurepath
# 重新打开终端，或: source ~/.bashrc

pipx install hermes-onebot-adapter
hermes-onebot-adapter --init-config   # 已有配置可跳过
```

启动时只把 OneBot 反向 WS 绑到全网卡，方便 Windows 侧 NapCat 连入；WebUI 和 Hermes 口仍默认 `127.0.0.1`：

```bash
hermes-onebot-adapter --onebot-host 0.0.0.0
```

记下首次启动日志里的 WebUI token。配置文件在 WSL 的 `~/.onebot_adapter/config.json`（不是 Windows 用户目录）。

Windows 浏览器打开 `http://127.0.0.1:18820` 登录。在「连接管理」填写 Hermes 安装目录（默认 `~/.hermes`，必须是 WSL 里的路径），安装插件，然后：

```bash
hermes plugins enable onebot-platform
hermes gateway restart
```

也可用 CLI：`hermes-onebot-adapter install --hermes-dir ~/.hermes`。插件安装器会写入 WSL 内 `<hermes>/.env` 的 `ONEBOT_ADAPTER_URL`（`ws://127.0.0.1:18810/hermes`），不要改成 Windows 主机名。

## 配置 Windows 上的 NapCat

1. 适配器 WebUI「连接管理」使用**反向 WS**。
2. 从 `~/.onebot_adapter/config.json` 复制 `onebot_ws_token`（或 WebUI 里查看）。
3. 在 NapCat 面板添加反向 WS：

镜像网络或 localhost 转发正常时：

```text
ws://127.0.0.1:18800/onebot?access_token=<onebot_ws_token>
```

连不上时，在 WSL 执行 `hostname -I`，把第一项 IP 填进 URL，例如 `ws://172.x.x.x:18800/onebot?access_token=...`。Windows 防火墙若拦截该网卡，为 `18800` 放行入站，或临时关闭防火墙验证。

同一条 WS 既推事件也接受 API 调用，无需再配 OneBot HTTP API。

## 验证

| 检查 | 期望 |
|------|------|
| WSL：`ss -lntp \| grep -E '18800\|18810\|18820'` | `18800` 听 `0.0.0.0` 或 `*`；`18810` / `18820` 听 `127.0.0.1` |
| Windows 浏览器 `http://127.0.0.1:18820` | 能打开 WebUI |
| WebUI 仪表盘 | OneBot 已连接、Hermes 插件已连接，两边版本一致 |
| 群聊 @bot | Hermes 有回复 |

闪传与文件集：NapCat 必须是 **Windows 版 QQ 客户端**。本地文件路径填 Windows 路径（`C:\...`），不要填 WSL 的 `/home/...`。

## 开机自启（可选）

WSL 发行版未在跑时适配器和 Hermes 网关都不会在。任选一种：

- Windows 登录后执行 `wsl -d <发行版名> -- hermes-onebot-adapter --onebot-host 0.0.0.0`（网关另按 Hermes 文档常驻）。
- 在 WSL 启用 systemd（`/etc/wsl.conf` 里 `boot.systemd=true`），再为适配器和 Hermes 网关各写一个 user/system unit。仓库不提供现成 unit。

## 故障排查

| 现象 | 处理 |
|------|------|
| NapCat 连不上 `127.0.0.1:18800` | 确认用了 `--onebot-host 0.0.0.0`；开镜像网络或改用 `hostname -I` 的地址 |
| WebUI 打不开 | 确认服务在 WSL 里已启动；用 Windows 浏览器访问，不要在 WSL 无 GUI 时依赖本机图形 |
| 插件连不上 / 工具集写失败 | Hermes 必须在同一 WSL 的 `$HOME` 下，不要用 `/mnt/c` |
| 启动报 `hermes_install_dir is outside the allowed Hermes install roots` | 把目录改回 `~/.hermes`，或先在配置里写 `hermes_install_allowed_roots`（不要加 `/mnt/c`） |
| 正向 WS 连不上 Windows NapCat | 改回反向 WS；NAT 下 WSL 的 `127.0.0.1` 不是 Windows 的 localhost |
| 闪传失败 | 确认 NapCat 跑在 Windows QQ 客户端，而不是 Linux / Docker 版 |

升级与重装插件仍按根目录 [README.md](../README.md#快速开始) 的通用步骤，全部在 WSL 内执行。功能开关见 [聊天与过滤](chat.md)、[WebUI 与工具](webui.md)。

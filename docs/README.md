# 文档索引

根目录 [README.md](../README.md) 只保留安装、连接和 CLI。功能说明、REST API 和专项材料在本目录。发版前请同步更新 [CHANGELOG.md](../CHANGELOG.md)。

| 文档 | 读者 | 内容 |
|------|------|------|
| [README.md](../README.md) | 使用者 | 架构、安装、升级、配置流程、CLI、端口与 OneBot 连接 |
| [CHANGELOG.md](../CHANGELOG.md) | 使用者 / 维护者 | 版本变更；打 `vX.Y.Z` 标签前必须写好对应条目 |
| [wsl.md](wsl.md) | 使用者 | Windows 上用 WSL2 部署适配器 + Hermes，NapCat 反向 WS 从本机连入 |
| [webui.md](webui.md) | 使用者 | WebUI 各页、工具集管理、OneBot 工具与闪传 |
| [chat.md](chat.md) | 使用者 | 准入、指令/出站过滤、Cascade 未匹配转发、媒体投递、notice、群聊排队 |
| [ops.md](ops.md) | 使用者 | 自动化 API、环境变量、限流、统计、日志、配置备份 |
| [api.md](api.md) | 自动化 / WebUI 对接 | `/api/v1/*` 管理 API、自动化工具 API、Config / GroupConfig 字段 |
| [napcat-upload-callback-diagnostic.md](napcat-upload-callback-diagnostic.md) | 维护者 | NapCat `v4.18.13` 群文件上传完成回调的临时诊断补丁与复现步骤 |
| [AGENTS.md](../AGENTS.md) | 维护者 | 架构图、模块职责、开发命令、排队与过滤实现约定 |

插件元数据见 [`onebot_adapter/hermes_plugin/plugin.yaml`](../onebot_adapter/hermes_plugin/plugin.yaml)（安装时 `version` 会被替换为包版本）。

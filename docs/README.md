# 文档索引

面向使用者的说明以仓库根目录的 [README.md](../README.md) 为主；本目录存放 REST API、专项部署和诊断材料。发版前请同步更新 [CHANGELOG.md](../CHANGELOG.md)。

| 文档 | 读者 | 内容 |
|------|------|------|
| [README.md](../README.md) | 使用者 | 安装、升级、CLI、连接模式、WebUI 功能与各项过滤/排队配置 |
| [CHANGELOG.md](../CHANGELOG.md) | 使用者 / 维护者 | 版本变更；打 `vX.Y.Z` 标签前必须写好对应条目 |
| [wsl.md](wsl.md) | 使用者 | Windows 上用 WSL2 部署适配器 + Hermes，NapCat 反向 WS 从本机连入 |
| [api.md](api.md) | 自动化 / WebUI 对接 | `/api/v1/*` 管理 API、自动化工具 API、Config / GroupConfig 字段 |
| [napcat-upload-callback-diagnostic.md](napcat-upload-callback-diagnostic.md) | 维护者 | NapCat `v4.18.13` 群文件上传完成回调的临时诊断补丁与复现步骤 |
| [AGENTS.md](../AGENTS.md) | 维护者 | 架构图、模块职责、开发命令、排队与过滤实现约定 |

插件元数据见 [`onebot_adapter/hermes_plugin/plugin.yaml`](../onebot_adapter/hermes_plugin/plugin.yaml)（安装时 `version` 会被替换为包版本）。

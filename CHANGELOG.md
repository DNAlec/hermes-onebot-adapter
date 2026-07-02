# 更新日志

## [1.0.0b2] - 2026-07-03

### 变更
- `message_show_group_id` 和 `reaction_emoji_enabled` 默认值改为 `true`
- 触发关键词输入改为 tag 样式（回车添加），与私聊名单一致
- OneBot WS Token 字段新增显示/隐藏、复制、重新生成按钮
- 统一 OneBot 和 Hermes 的 token 标签为 "WS Token"
- 插件安装流程增加 `hermes plugins enable onebot-platform` 提示
- 平台提示词中合并转发示例移除错误的 QQ 号和序号

### 新增
- `MANIFEST.in` 确保 sdist 包含前端静态文件
- PyPI classifiers（Beta 状态、分类标签）和 project URLs

### 修复
- `.gitignore` 补充 IDE/OS/Vim 忽略规则

## [1.0.0b1] - 2026-07-02

首个公开测试版。

- OneBot 11 正向/反向 WebSocket 传输
- Hermes 插件桥接 + 工具集管理
- WebUI 仪表盘、连接管理、指令过滤
- 每群配置覆盖 + 群成员过滤
- /指令权限模型（everyone/admin/disabled）
- 28 个 OneBot API 工具暴露给 LLM
- ffmpeg 语音转码
- SeqMap: NapCat real_seq ↔ message_id 映射

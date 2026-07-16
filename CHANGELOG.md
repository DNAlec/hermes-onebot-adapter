# 更新日志

## [未发布]

### 新增
- notice 事件推送：戳一戳（bot 被戳，含私聊）和群成员进退群（区分主动退群/被踢）合成中文系统提示转发给 agent
- 新增配置项 `notify_poke_enabled` / `notify_member_change_enabled`（全局开关，GroupConfig 可 per-group 覆盖）
- `NormalizedEvent.is_system_notice` 字段标记合成系统事件，插件侧据此设 `MessageEvent.internal=True`

## [1.0.0b3] - 2026-07-07

### 新增
- 群聊消息排队机制：shared session 串行化，含 busy 槽 + FIFO 队列 + 看门狗超时兜底
- Hermes `mode_report` / `mode_refresh` 协议帧，支持动态会话隔离模式切换和热重载
- 版本检查 / 更新检测：前端仪表盘显示 GitHub 最新版本
- 插件版本不匹配检测，仪表盘版本告警
- 消息排队时贴表情回应 ID 配置

### 变更
- 重构 WebSocket 处理流程，改进多连接场景的事件分发
- 重构媒体处理：移除二进制帧支持，统一 JSON 负载，删除不再使用的媒体助手模块
- `handle_message` 改为后台任务执行，避免接收循环阻塞导致自死锁
- 并发发送请求限制，防止网关延迟引发重试风暴
- SeqMap 更新改为 fire-and-forget，避免阻塞导致重复发送
- 版本管理改为 `setuptools-scm` 动态生成，修复版本解析逻辑

### 修复
- 环形缓冲区重放增强鲁棒性，处理损坏条目并确保 WebSocket 正常关闭
- 群聊忙碌状态时间戳刷新机制，防止误判超时

### 文档
- README 补充适配器服务与 Hermes 插件关系说明，添加 API 文档链接

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

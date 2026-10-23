# 发布流程

版本由 `setuptools-scm` 从 Git 标签生成，不要手工修改 `_version_scm.py`。标签采用 `vX.Y.Z`；新增功能使用次版本，兼容修复使用补丁版本。

## 准备发布

1. 合入计划发布的 PR 和本地修复，确认工作区没有遗漏文件。
2. 将 [CHANGELOG](../CHANGELOG.md) 的待发布内容归入版本条目，填写日期并更新底部比较链接；保留 `Unreleased` 供下一轮开发使用。
3. 同步 README 的升级说明和受影响的功能文档。插件协议有变时必须说明重装插件、重启 Hermes 网关。
4. 在项目开发环境依次运行 `ruff check .` 和 `pytest -q -rs`。还应在安装了 Hermes 的环境运行测试，确认 `test_adapter_protocol.py` 的 11 项测试没有被跳过。若 Hermes 用包管理器安装，要避免可编辑安装的备用导入路径干扰“未安装 Hermes”的模拟测试。
5. 构建并审计前端，再验证发行包：

   ```bash
   python -m pip install -e '.[dev]' build twine
   ./scripts/build_frontend.sh
   (cd frontend && npm audit --audit-level=high)
   python -m build
   python -m twine check dist/*
   ```

   Node.js 要求见 [README](../README.md#环境要求)。检查 wheel 包含 `webui/static/index.html`、前端 assets 和 `hermes_plugin/plugin.yaml`。未打标签时，构建版本可能带 `.dev` 或本地后缀，这只是预发布验证，不应上传到 PyPI。
6. 提交文档与变更，复核发布说明、测试结果和最终提交。仅“准备发布”时停在这里，推送版本标签会真正发布。

## 正式发布

确认最终提交已在远程默认分支后，创建并推送版本标签，例如：

```bash
git push origin main
git tag -a v1.9.0 -m 'Release 1.9.0'
git push origin v1.9.0
```

推送 `v*` 标签触发 [Build & Publish](../.github/workflows/publish.yml)：校验标签和包版本一致 → Ruff → pytest → 前端构建与漏洞审计 → 构建 wheel/sdist → Twine 校验 → PyPI Trusted Publishing。该流程不自动创建 GitHub Release；可在 PyPI 成功后，以 CHANGELOG 对应条目作为 GitHub Release 说明。

检查 Actions 成功、PyPI 版本可见后，再按 [README 升级步骤](../README.md#快速开始)验证安装、WebUI 和插件加载。队列或授权变更需重装插件并重启 Hermes，不能只升级适配器服务。

## 发布失败

先查看 Actions 失败步骤。若上传尚未开始且提交无变化，可重跑工作流；若修改了源码或同版本已部分上传，先检查 PyPI 状态，再使用新版本号发布。不要移动已发布的标签或尝试覆盖 PyPI 上的同版本文件。

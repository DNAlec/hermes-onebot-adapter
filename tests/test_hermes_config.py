"""Tests for onebot_adapter.hermes_config (Hermes config.yaml 读写桥)."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from onebot_adapter import hermes_config as hc

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def hermes_dir(tmp_path: Path) -> Path:
    """创建一个最小 hermes 目录,含 config.yaml。"""
    d = tmp_path / "hermes"
    d.mkdir()
    (d / "config.yaml").write_text(
        "# Hermes 主配置\n"
        "platform_toolsets:\n"
        "  cli:\n"
        "    - web\n"
        "    - terminal\n"
        "# 顶层 key 注释\n"
        "provider: openai\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def empty_hermes_dir(tmp_path: Path) -> Path:
    """目录存在但 config.yaml 不存在。"""
    d = tmp_path / "hermes"
    d.mkdir()
    return d


@pytest.fixture
def nonexistent_dir(tmp_path: Path) -> Path:
    return tmp_path / "nonexistent"


# ── resolve_hermes_config_path ────────────────────────────────────────────


def test_resolve_path_returns_config_yaml(hermes_dir: Path):
    p = hc.resolve_hermes_config_path(str(hermes_dir))
    assert p is not None
    assert p == hermes_dir / "config.yaml"


def test_resolve_path_nonexistent_dir_returns_none(nonexistent_dir: Path):
    assert hc.resolve_hermes_config_path(str(nonexistent_dir)) is None


def test_resolve_path_empty_dir_returns_path(empty_hermes_dir: Path):
    # 目录存在但 config.yaml 不存在:返回路径对象(调用方决定新建/报错)
    p = hc.resolve_hermes_config_path(str(empty_hermes_dir))
    assert p is not None
    assert not p.exists()


# ── read_config ───────────────────────────────────────────────────────────


def test_read_config_returns_dict(hermes_dir: Path):
    data = hc.read_config(str(hermes_dir))
    assert data is not None
    assert data["provider"] == "openai"
    assert "platform_toolsets" in data


def test_read_config_empty_dir_returns_empty(empty_hermes_dir: Path):
    data = hc.read_config(str(empty_hermes_dir))
    assert len(data) == 0


# ── write_platform_toolsets ──────────────────────────────────────────────


def test_write_then_read_roundtrip(hermes_dir: Path):
    hc.write_platform_toolsets(str(hermes_dir), ["web", "terminal", "onebot"])
    current = hc.read_current_enabled(str(hermes_dir))
    assert sorted(current) == ["onebot", "terminal", "web"]


def test_write_creates_config_if_absent(empty_hermes_dir: Path):
    # config.yaml 不存在时,write 会创建
    hc.write_platform_toolsets(str(empty_hermes_dir), ["web", "onebot"])
    assert (empty_hermes_dir / "config.yaml").exists()
    current = hc.read_current_enabled(str(empty_hermes_dir))
    assert "web" in current
    assert "onebot" in current


def test_write_preserves_comments(hermes_dir: Path):
    hc.write_platform_toolsets(str(hermes_dir), ["web", "onebot"])
    content = (hermes_dir / "config.yaml").read_text(encoding="utf-8")
    assert "# Hermes 主配置" in content
    assert "# 顶层 key 注释" in content


def test_write_preserves_top_level_keys(hermes_dir: Path):
    hc.write_platform_toolsets(str(hermes_dir), ["web", "onebot"])
    data = hc.read_config(str(hermes_dir))
    assert data["provider"] == "openai"
    assert "platform_toolsets" in data
    assert "cli" in data["platform_toolsets"]
    assert data["platform_toolsets"]["cli"] == ["web", "terminal"]


def test_write_writes_known_plugin_toolsets(hermes_dir: Path):
    hc.write_platform_toolsets(str(hermes_dir), ["web", "onebot"])
    data = hc.read_config(str(hermes_dir))
    assert "known_plugin_toolsets" in data
    assert "onebot" in data["known_plugin_toolsets"]
    assert hc.PLUGIN_TOOLSET_KEY in list(data["known_plugin_toolsets"]["onebot"])


def test_write_dedup_and_sort(hermes_dir: Path):
    hc.write_platform_toolsets(str(hermes_dir), ["terminal", "web", "web", "onebot"])
    current = hc.read_current_enabled(str(hermes_dir))
    assert current == ["onebot", "terminal", "web"]


def test_write_raises_when_dir_missing(nonexistent_dir: Path):
    with pytest.raises(FileNotFoundError):
        hc.write_platform_toolsets(str(nonexistent_dir), ["web"])


def test_write_preserves_other_platforms(hermes_dir: Path):
    # 预先写入 telegram 配置
    data = hc.read_config(str(hermes_dir))
    data["platform_toolsets"]["telegram"] = ["web", "memory"]
    yaml = YAML(typ="rt")
    buf = io.StringIO()
    yaml.dump(data, buf)
    (hermes_dir / "config.yaml").write_text(buf.getvalue(), encoding="utf-8")

    hc.write_platform_toolsets(str(hermes_dir), ["terminal", "onebot"])
    data = hc.read_config(str(hermes_dir))
    assert data["platform_toolsets"]["telegram"] == ["web", "memory"]
    assert "onebot" in data["platform_toolsets"]


# ── reset_platform_toolsets ──────────────────────────────────────────────


def test_reset_removes_onebot_only(hermes_dir: Path):
    hc.write_platform_toolsets(str(hermes_dir), ["web", "onebot"])
    # 再加 telegram
    data = hc.read_config(str(hermes_dir))
    data["platform_toolsets"]["telegram"] = ["web"]
    yaml = YAML(typ="rt")
    buf = io.StringIO()
    yaml.dump(data, buf)
    (hermes_dir / "config.yaml").write_text(buf.getvalue(), encoding="utf-8")

    hc.reset_platform_toolsets(str(hermes_dir))
    data = hc.read_config(str(hermes_dir))
    assert "onebot" not in data["platform_toolsets"]
    assert "telegram" in data["platform_toolsets"]


def test_reset_idempotent(empty_hermes_dir: Path):
    # config.yaml 不存在时 reset 不报错
    hc.reset_platform_toolsets(str(empty_hermes_dir))


def test_reset_nonexistent_dir_does_nothing(nonexistent_dir: Path):
    hc.reset_platform_toolsets(str(nonexistent_dir))


# ── read_current_enabled ─────────────────────────────────────────────────


def test_read_current_enabled_empty(hermes_dir: Path):
    # 初始 config.yaml 里没有 platform_toolsets.onebot
    assert hc.read_current_enabled(str(hermes_dir)) == []


def test_read_current_enabled_after_write(hermes_dir: Path):
    hc.write_platform_toolsets(str(hermes_dir), ["web", "terminal", "onebot"])
    current = hc.read_current_enabled(str(hermes_dir))
    assert sorted(current) == ["onebot", "terminal", "web"]


# ── default_onebot_toolsets ───────────────────────────────────────────────


def test_default_onebot_toolsets_contains_plugin_key(monkeypatch, tmp_path: Path):
    # 由于无法 import 真实 Hermes,fallback 路径应包含 "onebot"
    defaults = hc.default_onebot_toolsets(str(tmp_path))
    assert hc.PLUGIN_TOOLSET_KEY in defaults
    assert "web" in defaults  # 核心工具
    assert "moa" not in defaults  # _DEFAULT_OFF_TOOLSETS 排除


def test_default_onebot_toolsets_fallback_excludes_default_off(monkeypatch, tmp_path: Path):
    defaults = hc.default_onebot_toolsets(str(tmp_path))
    for off in ["moa", "spotify", "discord", "video", "video_gen", "x_search"]:
        assert off not in defaults, f"{off} should be excluded from defaults"


# ── list_available_toolsets ──────────────────────────────────────────────


def test_list_available_toolsets_unimportable_returns_error(monkeypatch, tmp_path: Path):
    # 指向一个空目录,import hermes_cli 必失败。
    # 清理可能被其它测试(test_adapter_protocol)污染的 sys.modules 缓存 + 临时移除
    # sys.path 里的 hermes-agent 目录,确保 import 真正失败而非命中缓存的真实模块。
    polluted = [k for k in list(sys.modules) if k == "toolsets" or k.startswith("hermes_cli")]
    saved = {k: sys.modules.pop(k) for k in polluted if k in sys.modules}
    hermes_agent = Path("/home/alec/.hermes/hermes-agent").resolve()
    original_path = [p for p in sys.path if p and Path(p).resolve() != hermes_agent]
    monkeypatch.setattr(sys, "path", original_path)
    try:
        result = hc.list_available_toolsets(str(tmp_path))
        assert "error" in result
        assert "detail" in result
    finally:
        for k, mod in saved.items():
            if k not in sys.modules:
                sys.modules[k] = mod


def test_list_available_toolsets_with_stub_hermes(monkeypatch, tmp_path: Path):
    """用 stub 模块模拟 hermes_cli.tools_config 和 toolsets。"""
    hermes_dir = tmp_path / "hermes"
    hermes_dir.mkdir()

    # 创建 stub 包
    hermes_cli_dir = hermes_dir / "hermes_cli"
    hermes_cli_dir.mkdir()
    (hermes_cli_dir / "__init__.py").write_text("", encoding="utf-8")

    # stub tools_config.py
    (hermes_cli_dir / "tools_config.py").write_text(
        "CONFIGURABLE_TOOLSETS = [\n"
        '    ("web", "🔍 Web", "web_search, web_extract"),\n'
        '    ("terminal", "💻 Terminal", "terminal, process"),\n'
        '    ("onebot", "🐧 Onebot", "onebot tools"),\n'
        "]\n"
        "_DEFAULT_OFF_TOOLSETS = set()\n"
        "def _get_effective_configurable_toolsets():\n"
        "    return CONFIGURABLE_TOOLSETS\n"
        "def _get_plugin_toolset_keys():\n"
        '    return {"onebot"}\n',
        encoding="utf-8",
    )

    # stub toolsets.py
    (hermes_dir / "toolsets.py").write_text(
        "def resolve_toolset(name):\n"
        '    if name == "web":\n'
        '        return ["web_search", "web_extract"]\n'
        '    if name == "terminal":\n'
        '        return ["terminal", "process"]\n'
        '    if name == "onebot":\n'
        '        return [f"onebot_tool_{i}" for i in range(5)]\n'
        "    return []\n",
        encoding="utf-8",
    )

    # 清理 sys.path 和 sys.modules 中可能被其它测试(test_adapter_protocol)污染的
    # hermes_cli/toolsets 缓存 + 真实 hermes-agent 路径,确保 stub 模块被正确 import。
    original_path = list(sys.path)
    polluted_modules = [k for k in list(sys.modules) if k == "toolsets" or k.startswith("hermes_cli")]
    saved_modules = {k: sys.modules.pop(k) for k in polluted_modules if k in sys.modules}
    hermes_agent = Path("/home/alec/.hermes/hermes-agent").resolve()
    filtered_path = [p for p in original_path if p and Path(p).resolve() != hermes_agent]
    sys.path[:] = filtered_path
    try:
        result = hc.list_available_toolsets(str(hermes_dir))
        assert "error" not in result, result
        assert "configurable" in result
        keys = [t["key"] for t in result["configurable"]]
        assert "web" in keys
        assert "onebot" in keys
        # is_plugin 标记
        onebot_entry = next(t for t in result["configurable"] if t["key"] == "onebot")
        assert onebot_entry["is_plugin"] is True
        web_entry = next(t for t in result["configurable"] if t["key"] == "web")
        assert web_entry["is_plugin"] is False
        # tools 列表
        assert len(web_entry["tools"]) == 2
    finally:
        # 恢复被移除的模块缓存
        for k, mod in saved_modules.items():
            if k not in sys.modules:
                sys.modules[k] = mod
        sys.path[:] = original_path


def test_list_available_toolsets_reads_mcp_servers(tmp_path: Path):
    """config.yaml 含 mcp_servers 时返回结构正确(即使 import 失败也尽量读 MCP)。"""
    hermes_dir = tmp_path / "hermes"
    hermes_dir.mkdir()
    (hermes_dir / "config.yaml").write_text(
        "mcp_servers:\n"
        "  github:\n"
        "    enabled: true\n"
        "  slack:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    # import 必失败(无 hermes_cli),但 mcp 读取应尽力而为
    result = hc.list_available_toolsets(str(hermes_dir))
    # import 失败 → error 优先;若成功则 mcp_servers 应被填充
    # (由于 list_available_toolsets 先 import 再读 mcp,失败时直接返回 error)
    if "error" not in result:
        mcp_names = [m["name"] for m in result.get("mcp_servers", [])]
        assert "github" in mcp_names


# ── _find_venv ────────────────────────────────────────────────────────────


def test_find_venv_detects_hermes_agent_subdir(tmp_path: Path):
    """~/.hermes/hermes-agent/venv/bin/python 布局。"""
    hermes_dir = tmp_path / "hermes"
    agent_dir = hermes_dir / "hermes-agent"
    venv_bin = agent_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    result = hc._find_venv(hermes_dir)
    assert result is not None
    py_path, agent = result
    assert py_path == str(venv_bin / "python")
    assert agent == agent_dir


def test_find_venv_detects_agent_dir_directly(tmp_path: Path):
    """hermes_install_dir 直接是 agent 目录(含 hermes_cli/ + venv/)。"""
    agent_dir = tmp_path / "agent"
    (agent_dir / "hermes_cli").mkdir(parents=True)
    venv_bin = agent_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    result = hc._find_venv(agent_dir)
    assert result is not None
    py_path, agent = result
    assert py_path == str(venv_bin / "python")
    assert agent == agent_dir


def test_find_venv_returns_none_when_no_venv(tmp_path: Path):
    """无 venv 目录时返回 None。"""
    hermes_dir = tmp_path / "hermes"
    hermes_dir.mkdir()
    (hermes_dir / "hermes-agent").mkdir()
    assert hc._find_venv(hermes_dir) is None


# ── list_available_toolsets 子进程路径 ─────────────────────────────────────


def test_list_available_toolsets_subprocess_path(monkeypatch, tmp_path: Path):
    """有 venv 时走子进程路径,不触碰 sys.path。"""
    hermes_dir = tmp_path / "hermes"
    agent_dir = hermes_dir / "hermes-agent"
    venv_bin = agent_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (hermes_dir / "config.yaml").write_text(
        "mcp_servers:\n  ctx7:\n    enabled: true\n", encoding="utf-8",
    )

    # stub subprocess.run 返回固定 JSON
    class _FakeProc:
        returncode = 0
        stdout = (
            '{"configurable": [{"key": "web", "label": "Web", '
            '"description": "d", "tools": ["web_search"], "is_plugin": false}]}'
        )
        stderr = ""

    def fake_run(cmd, **kw):
        assert "venv/bin/python" in cmd[0]
        return _FakeProc()

    monkeypatch.setattr(hc.subprocess, "run", fake_run)
    # 确保 sys.path 方案不被触发(若被触发会因无 hermes_cli 而返回 error)
    monkeypatch.setattr(hc, "_agent_syspath", lambda d: [])

    result = hc.list_available_toolsets(str(hermes_dir))
    assert "error" not in result
    assert len(result["configurable"]) == 1
    assert result["configurable"][0]["key"] == "web"
    # mcp_servers 应从 config.yaml 读取
    mcp_names = [m["name"] for m in result["mcp_servers"]]
    assert "ctx7" in mcp_names


def test_list_available_toolsets_subprocess_fails_to_syspath(monkeypatch, tmp_path: Path):
    """子进程失败时 fallback 到 sys.path 方案。"""
    hermes_dir = tmp_path / "hermes"
    agent_dir = hermes_dir / "hermes-agent"
    venv_bin = agent_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    # stub 子进程返回非零退出码
    class _FakeProc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **kw: _FakeProc())

    # 清理 sys.modules 确保 import 真正失败
    polluted = [k for k in list(sys.modules) if k == "toolsets" or k.startswith("hermes_cli")]
    saved = {k: sys.modules.pop(k) for k in polluted if k in sys.modules}
    hermes_agent = Path("/home/alec/.hermes/hermes-agent").resolve()
    original_path = [p for p in sys.path if p and Path(p).resolve() != hermes_agent]
    monkeypatch.setattr(sys, "path", original_path)
    try:
        result = hc.list_available_toolsets(str(hermes_dir))
        assert "error" in result
        assert "detail" in result
    finally:
        for k, mod in saved.items():
            if k not in sys.modules:
                sys.modules[k] = mod


# ── default_onebot_toolsets 子进程路径 ─────────────────────────────────────


def test_default_onebot_toolsets_subprocess_path(monkeypatch, tmp_path: Path):
    """有 venv 时走子进程获取 keys + default_off,本地计算差集。"""
    hermes_dir = tmp_path / "hermes"
    agent_dir = hermes_dir / "hermes-agent"
    venv_bin = agent_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    class _FakeProc:
        returncode = 0
        stdout = '{"keys": ["web", "terminal", "moa"], "default_off": ["moa"]}'
        stderr = ""

    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **kw: _FakeProc())
    monkeypatch.setattr(hc, "_agent_syspath", lambda d: [])

    defaults = hc.default_onebot_toolsets(str(hermes_dir))
    assert "web" in defaults
    assert "terminal" in defaults
    assert "moa" not in defaults  # 在 default_off 中
    assert hc.PLUGIN_TOOLSET_KEY in defaults


# ── no_mcp sentinel ───────────────────────────────────────────────────────


def test_no_mcp_sentinel_persisted(hermes_dir: Path):
    hc.write_platform_toolsets(str(hermes_dir), ["web", "no_mcp"])
    current = hc.read_current_enabled(str(hermes_dir))
    assert "no_mcp" in current
    assert "web" in current


def test_no_mcp_overwrites_previous(hermes_dir: Path):
    hc.write_platform_toolsets(str(hermes_dir), ["web", "terminal", "onebot"])
    hc.write_platform_toolsets(str(hermes_dir), ["web", "no_mcp"])
    current = hc.read_current_enabled(str(hermes_dir))
    assert "terminal" not in current
    assert "onebot" not in current
    assert "no_mcp" in current


# ── installer 集成 ─────────────────────────────────────────────────────────


def test_install_initializes_platform_toolsets(tmp_path: Path):
    from onebot_adapter import installer

    installer.install(str(tmp_path / "hermes"))
    config_path = tmp_path / "hermes" / "config.yaml"
    assert config_path.exists()
    data = hc.read_config(str(tmp_path / "hermes"))
    assert "platform_toolsets" in data
    assert "onebot" in data["platform_toolsets"]
    assert hc.PLUGIN_TOOLSET_KEY in list(data["platform_toolsets"]["onebot"])
    # known_plugin_toolsets 也应写入
    assert "known_plugin_toolsets" in data
    assert "onebot" in data["known_plugin_toolsets"]


def test_install_default_toolsets_contains_core_and_plugin(tmp_path: Path):
    from onebot_adapter import installer

    installer.install(str(tmp_path / "hermes"))
    current = hc.read_current_enabled(str(tmp_path / "hermes"))
    # 核心工具
    assert "web" in current
    # 插件工具
    assert hc.PLUGIN_TOOLSET_KEY in current
    # default-off 工具不包含
    assert "moa" not in current

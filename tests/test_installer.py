"""Tests for the plugin installer (install + uninstall + .env auto-write)."""
from pathlib import Path

from onebot_adapter import installer


def test_install_copies_files(tmp_path):
    result = installer.install(str(tmp_path / "hermes"))
    assert "error" not in result
    dest = Path(result["plugin_dest"])
    assert dest.exists()
    for fname in ("__init__.py", "adapter.py", "markdown.py", "onebot_tools.py", "plugin.yaml"):
        assert (dest / fname).exists(), f"{fname} not copied"
    assert "__init__.py" in result["copied"]
    assert "plugin.yaml" in result["copied"]


def test_install_creates_plugin_dir(tmp_path):
    hermes_dir = tmp_path / "hermes"
    assert not hermes_dir.exists()
    installer.install(str(hermes_dir))
    assert (hermes_dir / "plugins" / "onebot").exists()


def test_install_overwrites_existing(tmp_path):
    dest_dir = tmp_path / "hermes" / "plugins" / "onebot"
    dest_dir.mkdir(parents=True)
    stale_file = dest_dir / "__init__.py"
    stale_file.write_text("# stale")
    installer.install(str(tmp_path / "hermes"))
    content = stale_file.read_text()
    assert "register" in content


def test_install_cleans_pycache(tmp_path):
    dest_dir = tmp_path / "hermes" / "plugins" / "onebot"
    pycache = dest_dir / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "old.cpython-311.pyc").write_bytes(b"\x00")
    installer.install(str(tmp_path / "hermes"))
    assert not pycache.exists()


def test_install_default_hermes_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    result = installer.install()
    assert result["hermes_dir"] == str(tmp_path / "hermes")
    assert Path(result["plugin_dest"]).exists()


# ── .env auto-write ─────────────────────────────────────────────────────


def test_install_writes_env_vars(tmp_path):
    installer.install(
        str(tmp_path / "hermes"),
        adapter_url="ws://127.0.0.1:18810/hermes",
        adapter_token="sectok123",
    )
    env = tmp_path / "hermes" / ".env"
    assert env.exists()
    content = env.read_text()
    assert "ONEBOT_ADAPTER_URL=ws://127.0.0.1:18810/hermes" in content
    assert "ONEBOT_ADAPTER_TOKEN=sectok123" in content


def test_install_merges_existing_env(tmp_path):
    env = tmp_path / "hermes" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("EXISTING_VAR=keep_me\n")
    installer.install(
        str(tmp_path / "hermes"),
        adapter_url="ws://host/hermes",
        adapter_token="tok",
    )
    content = env.read_text()
    assert "EXISTING_VAR=keep_me" in content
    assert "ONEBOT_ADAPTER_URL=ws://host/hermes" in content


def test_install_env_vars_in_result(tmp_path):
    result = installer.install(
        str(tmp_path / "hermes"),
        adapter_url="ws://url/hermes",
        adapter_token="t",
    )
    assert result["env_vars"]["ONEBOT_ADAPTER_URL"] == "ws://url/hermes"
    assert result["env_vars"]["ONEBOT_ADAPTER_TOKEN"] == "t"


def test_install_no_env_vars_when_empty(tmp_path):
    result = installer.install(str(tmp_path / "hermes"))
    assert result["env_vars"] == {}


# ── Uninstall ────────────────────────────────────────────────────────────


def test_uninstall_removes_directory(tmp_path):
    installer.install(str(tmp_path / "hermes"))
    dest = tmp_path / "hermes" / "plugins" / "onebot"
    assert dest.exists()
    result = installer.uninstall(str(tmp_path / "hermes"))
    assert result["removed"] is True
    assert not dest.exists()


def test_uninstall_idempotent(tmp_path):
    result = installer.uninstall(str(tmp_path / "hermes"))
    assert result["removed"] is False  # didn't exist


def test_uninstall_cleans_env(tmp_path):
    installer.install(
        str(tmp_path / "hermes"),
        adapter_url="ws://url/hermes",
        adapter_token="tok",
    )
    result = installer.uninstall(str(tmp_path / "hermes"))
    assert result["env_cleaned"] is True
    # File should be deleted (no other vars)
    assert not (tmp_path / "hermes" / ".env").exists()


def test_uninstall_preserves_other_env_vars(tmp_path):
    env = tmp_path / "hermes" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("OTHER_VAR=stay\nONEBOT_ADAPTER_URL=ws://x/hermes\n")
    result = installer.uninstall(str(tmp_path / "hermes"))
    assert result["env_cleaned"] is True
    content = env.read_text()
    assert "OTHER_VAR=stay" in content
    assert "ONEBOT_ADAPTER_URL" not in content


# ── _write_env quoting + atomic write ──────────────────────────────────────


def test_write_env_quotes_values_with_spaces(tmp_path):
    """Values containing spaces are quoted so dotenv parsers read them correctly."""
    env_path = tmp_path / ".env"
    installer._write_env(env_path, {"KEY": "value with spaces"})
    content = env_path.read_text()
    assert 'KEY="value with spaces"' in content


def test_write_env_preserves_plain_values(tmp_path):
    """Values without special characters are written bare for readability."""
    env_path = tmp_path / ".env"
    installer._write_env(env_path, {"URL": "ws://127.0.0.1:18810/hermes"})
    content = env_path.read_text()
    assert "URL=ws://127.0.0.1:18810/hermes" in content
    assert '"' not in content.split("URL=")[1].split("\n")[0]


def test_read_env_strips_quotes(tmp_path):
    """_read_env strips surrounding quotes so round-trip writes are idempotent."""
    env_path = tmp_path / ".env"
    env_path.write_text('KEY="quoted value"\nBARE=plain\n')
    env = installer._read_env(env_path)
    assert env["KEY"] == "quoted value"
    assert env["BARE"] == "plain"


def test_write_env_roundtrip_idempotent(tmp_path):
    """Read → write → read produces the same values (no quote accumulation)."""
    env_path = tmp_path / ".env"
    env_path.write_text('KEY="value with spaces"\n')
    # Read, re-write (no changes), read again
    env = installer._read_env(env_path)
    installer._write_env(env_path, env)
    env2 = installer._read_env(env_path)
    assert env2["KEY"] == "value with spaces"
    # File should still have exactly one layer of quotes
    content = env_path.read_text()
    assert content.count('"') == 2  # exactly one pair of quotes


def test_write_env_atomic_no_tmp_left(tmp_path):
    """After a successful write, no .tmp file is left behind."""
    env_path = tmp_path / ".env"
    installer._write_env(env_path, {"K": "v"})
    assert env_path.exists()
    assert not (tmp_path / ".env.tmp").exists()


# ── install path safety ────────────────────────────────────────────────────


def test_install_rejects_system_path():
    """install() should refuse to write to system paths like /etc."""
    result = installer.install("/etc/hermes-test")
    assert "error" in result
    assert "$HOME" in result["error"] or "outside" in result["error"]


def test_uninstall_rejects_system_path():
    """uninstall() should refuse to operate on system paths."""
    result = installer.uninstall("/usr/local/hermes-test")
    assert "error" in result
    assert "$HOME" in result["error"] or "outside" in result["error"]

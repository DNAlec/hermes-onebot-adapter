import pytest

from onebot_adapter.config import AdapterConfig, ConfigLoadError, ConfigStore, ensure_tokens
from onebot_adapter.relay.protocol import (
    NormalizedEvent,
    event_message,
    ready_message,
    result_message,
    send_message,
)


def test_config_defaults_validate(tmp_path):
    cfg = ensure_tokens(AdapterConfig(), tmp_path / "cfg.json")
    assert cfg.onebot_mode == "reverse"
    assert cfg.hermes_ws_token
    assert cfg.onebot_ws_token
    assert cfg.validate() == []
    assert cfg.bot_blacklist_enabled is True
    assert cfg.bot_blacklist_max_duration_seconds == 86400


def test_config_default_tokens_empty_before_ensure():
    cfg = AdapterConfig()
    assert not cfg.hermes_ws_token
    assert not cfg.onebot_ws_token
    assert any("onebot_ws_token" in e for e in cfg.validate())
    assert any("hermes_ws_token" in e for e in cfg.validate())


def test_config_forward_requires_url():
    cfg = AdapterConfig(onebot_mode="forward", onebot_forward_ws_url="")
    errors = cfg.validate()
    assert any("onebot_forward_ws_url" in e for e in errors)


def test_config_media_delivery_mode_invalid():
    cfg = AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2", media_delivery_mode="bogus")
    errors = cfg.validate()
    assert any("media_delivery_mode" in e for e in errors)


def test_config_media_delivery_mode_valid():
    from onebot_adapter.config import MEDIA_DELIVERY_CACHE
    cfg = AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2", media_delivery_mode=MEDIA_DELIVERY_CACHE)
    errors = cfg.validate()
    assert not any("media_delivery_mode" in e for e in errors)


def test_protocol_ready_includes_media_delivery_mode():
    msg = ready_message(True, "0.1.0", self_id="100", media_delivery_mode="cache")
    assert msg["type"] == "ready"
    assert msg["media_delivery_mode"] == "cache"


def test_config_roundtrip(tmp_path):
    from onebot_adapter.config import load_config, save_config

    p = tmp_path / "cfg.json"
    cfg = AdapterConfig(self_id="123456", seq_map_size=100)
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.self_id == "123456"
    assert loaded.seq_map_size == 100


def test_load_config_invalid_json_does_not_fall_back(tmp_path):
    from onebot_adapter.config import load_config

    p = tmp_path / "cfg.json"
    p.write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        load_config(p)
    assert p.read_text(encoding="utf-8") == "{broken"


def test_config_store_patch_and_notify():
    store = ConfigStore(AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2"))
    seen = []
    store.on_change(lambda old, new: seen.append((old.self_id, new.self_id)))
    store.patch(self_id="999")
    assert store.config.self_id == "999"
    assert seen == [("", "999")]


def test_protocol_envelopes():
    ev = NormalizedEvent(
        message_id="1", chat_id="group:42", chat_type="group",
        user_id="u1", user_name="A", text="hi",
    )
    assert event_message(ev)["type"] == "event"
    assert ready_message(True, "0.1.0")["onebot_connected"] is True
    assert send_message("send_text", "r1", "group:42", content="x")["action"] == "send_text"
    assert result_message("r1", True, message_id="9")["success"] is True


def test_normalized_event_is_system_notice_default():
    """is_system_notice 默认 False,to_dict 输出 False。"""
    ev = NormalizedEvent(
        message_id="1", chat_id="group:42", chat_type="group",
        user_id="u1", user_name="A", text="hi",
    )
    assert ev.is_system_notice is False
    d = ev.to_dict()
    assert d["is_system_notice"] is False
    assert "rate_limit_eligible" not in d


def test_normalized_event_is_system_notice_true():
    """is_system_notice=True 时 to_dict 输出 True。"""
    ev = NormalizedEvent(
        message_id="", chat_id="group:42", chat_type="group",
        user_id="u1", user_name="A", text="[系统] ...",
        is_system_notice=True,
    )
    d = ev.to_dict()
    assert d["is_system_notice"] is True
    # event_message 透传
    msg = event_message(ev)
    assert msg["is_system_notice"] is True


# ── ensure_tokens ────────────────────────────────────────────────────────


def test_ensure_tokens_generates_when_empty(tmp_path):
    from onebot_adapter.config import load_config

    p = tmp_path / "cfg.json"
    cfg = AdapterConfig()
    assert not cfg.onebot_ws_token
    assert not cfg.hermes_ws_token
    assert not cfg.webui_token
    result = ensure_tokens(cfg, p)
    assert result.onebot_ws_token
    assert result.hermes_ws_token
    assert result.webui_token
    assert p.exists()  # persisted
    loaded = load_config(p)
    assert loaded.onebot_ws_token == result.onebot_ws_token
    assert loaded.hermes_ws_token == result.hermes_ws_token
    assert loaded.webui_token == result.webui_token


def test_ensure_tokens_preserves_existing(tmp_path):
    p = tmp_path / "cfg.json"
    cfg = AdapterConfig(
        onebot_ws_token="keep-ob", hermes_ws_token="keep-hermes", webui_token="keep-webui",
    )
    result = ensure_tokens(cfg, p)
    assert result.onebot_ws_token == "keep-ob"
    assert result.hermes_ws_token == "keep-hermes"
    assert result.webui_token == "keep-webui"
    assert not p.exists()  # no save when nothing changed


def test_ensure_tokens_generates_only_missing(tmp_path):
    from onebot_adapter.config import load_config

    p = tmp_path / "cfg.json"
    cfg = AdapterConfig(onebot_ws_token="", hermes_ws_token="keep-hermes", webui_token="keep-webui")
    result = ensure_tokens(cfg, p)
    assert result.onebot_ws_token  # generated
    assert result.hermes_ws_token == "keep-hermes"  # preserved
    assert result.webui_token == "keep-webui"  # preserved
    loaded = load_config(p)
    assert loaded.onebot_ws_token == result.onebot_ws_token
    assert loaded.hermes_ws_token == "keep-hermes"
    assert loaded.webui_token == "keep-webui"


# ── save_config comment injection ────────────────────────────────────────


def test_save_config_includes_comments(tmp_path):
    import json

    from onebot_adapter.config import save_config

    p = tmp_path / "cfg.json"
    save_config(AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2"), p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "_comment_onebot_mode" in data
    assert "_comment_onebot_ws_token" in data
    assert "_comment_hermes_ws_token" in data
    assert "_comment_webui_token_lifetime_hours" in data
    assert "_comment_dm_user_filter_mode" in data
    assert "_comment_log_level" in data
    assert "_comment_groups" in data


def test_load_config_ignores_comment_fields(tmp_path):
    from onebot_adapter.config import load_config, save_config

    p = tmp_path / "cfg.json"
    save_config(AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2"), p)
    loaded = load_config(p)
    assert not hasattr(loaded, "_comment_onebot_mode")
    assert loaded.onebot_ws_token == "t1"


def _read_audit_events(config_file):
    import json

    audit_file = config_file.parent / "logs" / "config-audit.log"
    return [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]


def test_save_config_writes_safe_audit_event(tmp_path):
    from onebot_adapter.config import save_config

    p = tmp_path / "cfg.json"
    secret_token = "audit-must-not-leak-token"
    secret_prompt = "audit-must-not-leak-prompt"
    cfg = AdapterConfig(
        onebot_ws_token=secret_token,
        hermes_ws_token="hermes-secret",
        webui_token="webui-secret",
        global_channel_prompt=secret_prompt,
        seq_map_size=123,
    )
    save_config(
        cfg,
        p,
        source="test",
        reason="test.config_change",
        actor="test_actor",
        metadata={"client_ip": "127.0.0.1", "http_method": "PUT", "http_path": "/api/config"},
        submitted_fields=["seq_map_size", "global_channel_prompt"],
    )

    audit_path = p.parent / "logs" / "config-audit.log"
    raw_audit = audit_path.read_text(encoding="utf-8")
    assert secret_token not in raw_audit
    assert secret_prompt not in raw_audit
    assert "hermes-secret" not in raw_audit
    assert "webui-secret" not in raw_audit

    event = _read_audit_events(p)[-1]
    assert event["outcome"] == "success"
    assert event["source"] == "test"
    assert event["reason"] == "test.config_change"
    assert event["actor"] == "test_actor"
    assert event["client_ip"] == "127.0.0.1"
    assert event["submitted_fields"] == ["global_channel_prompt", "seq_map_size"]
    assert "seq_map_size" in event["changed_fields"]
    assert len(event["new_fingerprint"]) == 64


def test_save_config_flags_suspicious_reset(tmp_path, caplog):
    from onebot_adapter.config import save_config

    p = tmp_path / "cfg.json"
    original = AdapterConfig(
        onebot_ws_token="t1",
        hermes_ws_token="t2",
        webui_token="t3",
        self_id="123",
        groups={"42": {"group_id": "42"}},
        event_queue_enabled=False,
        notify_poke_enabled=True,
        command_filter_enabled=True,
        seq_map_size=100,
    )
    save_config(original, p, source="test", reason="test.initial")

    reset = AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2", webui_token="t3")
    with caplog.at_level("WARNING"):
        save_config(reset, p, source="webui", reason="webui.config_patch")

    event = _read_audit_events(p)[-1]
    assert event["suspicious_reset"] is True
    assert event["old_group_count"] == 1
    assert event["new_group_count"] == 0
    assert "groups" in event["reverted_to_default_fields"]
    assert "suspicious config reset detected" in caplog.text


def test_config_save_failure_is_audited(tmp_path, monkeypatch):
    import onebot_adapter.config as config_module

    p = tmp_path / "cfg.json"

    def fail_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        config_module.save_config(
            AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2"),
            p,
            source="test",
            reason="test.failure",
        )

    event = _read_audit_events(p)[-1]
    assert event["outcome"] == "failure"
    assert event["error_type"] == "OSError"
    assert event["reason"] == "test.failure"


def test_config_audit_failure_does_not_fail_save(tmp_path, monkeypatch, caplog):
    import onebot_adapter.config as config_module

    p = tmp_path / "cfg.json"

    def fail_audit(_target, _event):
        raise OSError("simulated audit failure")

    monkeypatch.setattr(config_module, "_write_config_audit", fail_audit)
    with caplog.at_level("ERROR"):
        config_module.save_config(
            AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2"),
            p,
            source="test",
            reason="test.audit_failure",
        )

    assert p.exists()
    assert "config audit write failed" in caplog.text


def test_force_init_resets_config_preserves_tokens_and_audits(tmp_path, monkeypatch):
    from onebot_adapter.__main__ import _init_config
    from onebot_adapter.config import load_config, save_config

    p = tmp_path / "cfg.json"
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(p))
    original = AdapterConfig(
        onebot_ws_token="keep-onebot",
        hermes_ws_token="keep-hermes",
        webui_token="keep-webui",
        self_id="123",
        groups={"42": {"group_id": "42"}},
        event_queue_enabled=False,
    )
    save_config(original, p, source="test", reason="test.initial")

    assert _init_config(force=True) == 0

    reset = load_config(p)
    assert reset.onebot_ws_token == "keep-onebot"
    assert reset.hermes_ws_token == "keep-hermes"
    assert reset.webui_token == "keep-webui"
    assert reset.self_id == ""
    assert reset.groups == {}
    assert reset.event_queue_enabled is True
    event = _read_audit_events(p)[-1]
    assert event["reason"] == "cli.force_reinitialize"
    assert event["source"] == "cli"
    assert event["suspicious_reset"] is True


def test_config_validate_rejects_invalid_ports():
    cfg = AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2", onebot_reverse_ws_port=0)
    errors = cfg.validate()
    assert any("onebot_reverse_ws_port" in e for e in errors)

    cfg = AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2", hermes_ws_port=99999)
    errors = cfg.validate()
    assert any("hermes_ws_port" in e for e in errors)

    cfg = AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2", webui_port=65535)
    errors = cfg.validate()
    assert not any("webui_port" in e for e in errors)  # 65535 is valid

    cfg = AdapterConfig(onebot_ws_token="t1", hermes_ws_token="t2", webui_port=1)
    errors = cfg.validate()
    assert not any("webui_port" in e for e in errors)  # 1 is valid


def test_config_get_group_config_empty_dict():
    """get_group_config should return config for explicit empty dict (not default)."""
    from onebot_adapter.config import GroupConfig

    cfg = AdapterConfig(groups={"42": {}})
    gc = cfg.get_group_config("42")
    # Should return a GroupConfig from the empty dict, with group_id injected
    assert isinstance(gc, GroupConfig)
    assert gc.group_id == "42"


def test_config_is_admin_with_empty_string_group_id():
    """is_admin should handle group_id='' without treating it as 'no group'."""
    cfg = AdapterConfig(global_admins=["100"], groups={"42": {"group_id": "42", "admins": ["200"]}})
    # group_id="" should not match any group (is not None → checks groups[""] → not found → default)
    assert cfg.is_admin("200", group_id="") is False
    # group_id=None should skip group check
    assert cfg.is_admin("200") is False
    # global admin should always pass
    assert cfg.is_admin("100") is True
    # Valid group admin
    assert cfg.is_admin("200", group_id="42") is True


# ── notice 事件推送配置 ────────────────────────────────────────────────


def test_config_notify_defaults_disabled():
    """notify 开关默认 False。"""
    cfg = AdapterConfig(onebot_ws_token="t", hermes_ws_token="t")
    assert cfg.notify_poke_enabled is False
    assert cfg.notify_member_change_enabled is False
    assert cfg.validate() == []


def test_config_resolve_notify_poke_enabled_global():
    """resolve_notify_poke_enabled:全局开关。"""
    from onebot_adapter.config import GroupConfig

    cfg = AdapterConfig(
        onebot_ws_token="t", hermes_ws_token="t",
        notify_poke_enabled=True,
    )
    assert cfg.resolve_notify_poke_enabled(None) is True
    assert cfg.resolve_notify_poke_enabled("42") is True  # 无群配置 → 全局

    cfg2 = AdapterConfig(
        onebot_ws_token="t", hermes_ws_token="t",
        notify_poke_enabled=False,
        groups={"42": GroupConfig(group_id="42", notify_poke_enabled=True).to_dict()},
    )
    assert cfg2.resolve_notify_poke_enabled("42") is True  # 群覆盖
    assert cfg2.resolve_notify_poke_enabled(None) is False  # 私聊用全局
    assert cfg2.resolve_notify_poke_enabled("99") is False  # 其他群用全局


def test_config_resolve_notify_member_change_enabled_global():
    """resolve_notify_member_change_enabled:全局开关。"""
    from onebot_adapter.config import GroupConfig

    cfg = AdapterConfig(
        onebot_ws_token="t", hermes_ws_token="t",
        notify_member_change_enabled=True,
    )
    assert cfg.resolve_notify_member_change_enabled("42") is True

    cfg2 = AdapterConfig(
        onebot_ws_token="t", hermes_ws_token="t",
        notify_member_change_enabled=False,
        groups={"42": GroupConfig(group_id="42", notify_member_change_enabled=True).to_dict()},
    )
    assert cfg2.resolve_notify_member_change_enabled("42") is True
    assert cfg2.resolve_notify_member_change_enabled("99") is False


def test_config_validate_group_notify_fields():
    """GroupConfig 的 notice 字段类型检查。"""
    cfg = AdapterConfig(
        onebot_ws_token="t", hermes_ws_token="t",
        groups={"42": {"group_id": "42", "notify_poke_enabled": "yes"}},  # wrong type
    )
    errors = cfg.validate()
    assert any("notify_poke_enabled must be bool or null" in e for e in errors)

    cfg2 = AdapterConfig(
        onebot_ws_token="t", hermes_ws_token="t",
        groups={"42": {"group_id": "42", "notify_member_change_enabled": 1}},  # wrong type
    )
    errors = cfg2.validate()
    assert any("notify_member_change_enabled must be bool or null" in e for e in errors)

    # None 和 bool 都合法
    cfg3 = AdapterConfig(
        onebot_ws_token="t", hermes_ws_token="t",
        groups={"42": {"group_id": "42", "notify_poke_enabled": None, "notify_member_change_enabled": False}},
    )
    assert cfg3.validate() == []


def test_config_group_config_roundtrip_notify_fields():
    """GroupConfig notify 字段 to_dict/from_dict 往返。"""
    from onebot_adapter.config import GroupConfig

    gc = GroupConfig(group_id="42", notify_poke_enabled=True, notify_member_change_enabled=False)
    d = gc.to_dict()
    assert d["notify_poke_enabled"] is True
    assert d["notify_member_change_enabled"] is False
    gc2 = GroupConfig.from_dict(d)
    assert gc2.notify_poke_enabled is True
    assert gc2.notify_member_change_enabled is False

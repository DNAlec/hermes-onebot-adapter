from onebot_adapter.config import AdapterConfig, ConfigStore, ensure_tokens
from onebot_adapter.relay.protocol import (
    NormalizedEvent,
    event_message,
    ready_message,
    result_message,
    send_message,
)


def test_config_defaults_validate():
    cfg = ensure_tokens(AdapterConfig())
    assert cfg.onebot_mode == "reverse"
    assert cfg.hermes_ws_token
    assert cfg.onebot_ws_token
    assert cfg.validate() == []


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


def test_config_roundtrip(tmp_path):
    from onebot_adapter.config import load_config, save_config

    p = tmp_path / "cfg.json"
    cfg = AdapterConfig(self_id="123456", seq_map_size=100)
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.self_id == "123456"
    assert loaded.seq_map_size == 100


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
    assert "_comment_group_session_mode" in data
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

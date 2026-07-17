"""Tests for HermesRelayServer._resolve_seq_params (real_seq→message_id interception)."""
from __future__ import annotations

from unittest.mock import MagicMock

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot.seq_map import SeqMap
from onebot_adapter.relay.hermes_ws import HermesRelayServer


def _make_relay(seq_map: SeqMap | None = None) -> HermesRelayServer:
    cfg = AdapterConfig(self_id="999")
    return HermesRelayServer(
        cfg, MagicMock(), adapter_version="test",
        onebot_connected_fn=lambda: True,
        seq_map=seq_map,
    )


def test_resolve_seq_params_hit():
    """映射命中:real_seq → message_id,group_id 保留。"""
    sm = SeqMap(maxlen=10)
    sm.add("42", 15154, "1380622136")
    relay = _make_relay(sm)
    params = {"real_seq": 15154, "group_id": "42"}
    out = relay._resolve_seq_params("get_msg", params)
    assert out["message_id"] == 1380622136
    assert "real_seq" not in out
    assert out["group_id"] == "42"  # group_id 保留传给 OneBot API


def test_resolve_seq_params_miss_passthrough():
    """映射未命中:透传 real_seq 当 message_id。"""
    sm = SeqMap(maxlen=10)
    sm.add("42", 15154, "1380622136")
    relay = _make_relay(sm)
    params = {"real_seq": 99999, "group_id": "42"}
    out = relay._resolve_seq_params("get_msg", params)
    assert out["message_id"] == 99999  # 透传


def test_resolve_seq_params_no_group_id():
    """无 group_id/user_id:查不了映射,透传 real_seq 当 message_id。"""
    sm = SeqMap(maxlen=10)
    sm.add("42", 15154, "1380622136")
    relay = _make_relay(sm)
    params = {"real_seq": 888}
    out = relay._resolve_seq_params("get_msg", params)
    assert out["message_id"] == 888  # 透传


def test_resolve_seq_params_dm_uses_user_id_as_scope():
    """DM 场景:无 group_id 但有 user_id,用 user_id 作 SeqMap scope 查询。"""
    sm = SeqMap(maxlen=10)
    sm.add("10001000", 200, "6000")  # DM 存储 scope_id = user_id
    relay = _make_relay(sm)
    params = {"real_seq": 200, "user_id": "10001000"}
    out = relay._resolve_seq_params("get_msg", params)
    assert out["message_id"] == 6000
    assert out["user_id"] == "10001000"  # user_id 保留


def test_resolve_seq_params_mark_as_read_keeps_group_id():
    """mark_msg_as_read 带 real_seq:group_id 保留(NapCat 需要 group_id 定位会话)。"""
    sm = SeqMap(maxlen=10)
    sm.add("42", 100, "5000")
    relay = _make_relay(sm)
    params = {"real_seq": 100, "group_id": "42"}
    out = relay._resolve_seq_params("mark_msg_as_read", params)
    assert out["message_id"] == 5000
    assert out["group_id"] == "42"  # group_id 保留


def test_resolve_seq_params_non_seq_action_passthrough():
    """非拦截 action:params 不变。"""
    sm = SeqMap(maxlen=10)
    relay = _make_relay(sm)
    params = {"group_id": 42, "message_seq": 0}
    out = relay._resolve_seq_params("get_group_msg_history", params)
    assert out == params


def test_resolve_seq_params_mark_as_read_no_seq():
    """mark_msg_as_read 不传 real_seq(标记全部已读):params 不变。"""
    sm = SeqMap(maxlen=10)
    relay = _make_relay(sm)
    params = {"message_id": 0, "group_id": "42"}
    out = relay._resolve_seq_params("mark_msg_as_read", params)
    assert out == params


def test_resolve_seq_params_delete_msg():
    sm = SeqMap(maxlen=10)
    sm.add("42", 100, "5000")
    relay = _make_relay(sm)
    params = {"real_seq": 100, "group_id": "42"}
    out = relay._resolve_seq_params("delete_msg", params)
    assert out["message_id"] == 5000
    assert out["group_id"] == "42"


def test_resolve_seq_params_set_msg_emoji_like():
    sm = SeqMap(maxlen=10)
    sm.add("42", 100, "5000")
    relay = _make_relay(sm)
    params = {"real_seq": 100, "group_id": "42", "emoji_id": "76"}
    out = relay._resolve_seq_params("set_msg_emoji_like", params)
    assert out["message_id"] == 5000
    assert out["emoji_id"] == "76"
    assert out["group_id"] == "42"


def test_resolve_seq_params_forward_group_single_msg():
    """forward_group_single_msg: real_seq→message_id, group_id 保留。"""
    sm = SeqMap(maxlen=10)
    sm.add("42", 300, "7000")
    relay = _make_relay(sm)
    params = {"real_seq": 300, "group_id": "42"}
    out = relay._resolve_seq_params("forward_group_single_msg", params)
    assert out["message_id"] == 7000
    assert "real_seq" not in out
    assert out["group_id"] == "42"


def test_resolve_seq_params_forward_friend_single_msg():
    """forward_friend_single_msg: DM 场景用 user_id 作 scope, user_id 保留。"""
    sm = SeqMap(maxlen=10)
    sm.add("10001000", 400, "8000")
    relay = _make_relay(sm)
    params = {"real_seq": 400, "user_id": "10001000"}
    out = relay._resolve_seq_params("forward_friend_single_msg", params)
    assert out["message_id"] == 8000
    assert "real_seq" not in out
    assert out["user_id"] == "10001000"


def test_resolve_seq_params_no_seq_map():
    """无 SeqMap:不转换,params 不变。"""
    relay = _make_relay(None)
    params = {"real_seq": 100, "group_id": "42"}
    out = relay._resolve_seq_params("get_msg", params)
    assert out == params


def test_resolve_seq_params_invalid_real_seq():
    """real_seq 非数字:放回 real_seq 让 OneBot 报错,不静默丢数据。"""
    sm = SeqMap(maxlen=10)
    relay = _make_relay(sm)
    params = {"real_seq": "abc", "group_id": "42"}
    out = relay._resolve_seq_params("get_msg", params)
    assert out["real_seq"] == "abc"  # real_seq 放回
    assert "message_id" not in out

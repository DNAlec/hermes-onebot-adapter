"""Tests for SeqMap: real_seq → message_id mapping (global FIFO)."""
from __future__ import annotations

import pytest

from onebot_adapter.onebot.seq_map import SeqMap


def test_add_and_lookup():
    m = SeqMap(maxlen=10)
    m.add("42", 100, "msg-100")
    m.add("42", 101, "msg-101")
    assert m.lookup("42", 100) == "msg-100"
    assert m.lookup("42", 101) == "msg-101"
    assert m.lookup("42", 999) is None  # not present
    assert m.lookup("99", 100) is None  # different scope


def test_add_overwrites_existing_key():
    """重复 (scope_id, real_seq) 覆盖 message_id,不重复入队。"""
    m = SeqMap(maxlen=10)
    m.add("42", 100, "old")
    m.add("42", 100, "new")
    assert m.lookup("42", 100) == "new"
    # deque 长度仍为 1
    assert len(m._buf) == 1


def test_global_fifo_evicts_oldest():
    """全局 FIFO:超出上限丢弃最旧插入的条目(不分群)。"""
    m = SeqMap(maxlen=3)
    m.add("42", 1, "a")
    m.add("42", 2, "b")
    m.add("43", 1, "c")  # 不同群,但全局共享
    m.add("44", 1, "d")  # 淘汰最旧的 (42,1)
    assert m.lookup("42", 1) is None  # 被淘汰
    assert m.lookup("42", 2) == "b"
    assert m.lookup("43", 1) == "c"
    assert m.lookup("44", 1) == "d"
    assert len(m._buf) == 3


def test_scope_independent_keys():
    """不同 scope 的相同 real_seq 是不同 key。"""
    m = SeqMap(maxlen=10)
    m.add("42", 100, "g42-100")
    m.add("43", 100, "g43-100")
    assert m.lookup("42", 100) == "g42-100"
    assert m.lookup("43", 100) == "g43-100"


def test_empty_scope_ignored():
    m = SeqMap(maxlen=10)
    m.add("", 100, "x")
    assert m.lookup("", 100) is None


def test_empty_message_id_ignored():
    m = SeqMap(maxlen=10)
    m.add("42", 100, "")
    assert m.lookup("42", 100) is None


def test_invalid_maxlen():
    with pytest.raises(ValueError):
        SeqMap(maxlen=0)
    with pytest.raises(ValueError):
        SeqMap(maxlen=-1)


def test_update_maxlen_grow_preserves_entries():
    m = SeqMap(maxlen=2)
    m.add("42", 1, "a")
    m.add("42", 2, "b")
    m.update_maxlen(5)
    assert m.maxlen == 5
    assert m.lookup("42", 1) == "a"
    assert m.lookup("42", 2) == "b"
    m.add("42", 3, "c")
    assert m.lookup("42", 3) == "c"


def test_update_maxlen_shrink_truncates_oldest():
    m = SeqMap(maxlen=5)
    for i in range(1, 6):
        m.add("42", i, f"m{i}")
    m.update_maxlen(2)
    assert m.maxlen == 2
    assert m.lookup("42", 1) is None
    assert m.lookup("42", 2) is None
    assert m.lookup("42", 3) is None
    assert m.lookup("42", 4) == "m4"
    assert m.lookup("42", 5) == "m5"


def test_update_maxlen_same_is_noop():
    m = SeqMap(maxlen=5)
    m.add("42", 1, "a")
    m.update_maxlen(5)
    assert m.lookup("42", 1) == "a"


def test_update_maxlen_invalid():
    m = SeqMap(maxlen=5)
    with pytest.raises(ValueError):
        m.update_maxlen(0)


def test_global_fifo_multi_group_eviction():
    """活跃群不饿死安静群:全局 FIFO 按插入顺序淘汰,不按群。"""
    m = SeqMap(maxlen=5)
    # 群42 发3条,群43 发2条,共5条
    m.add("42", 1, "a")
    m.add("42", 2, "b")
    m.add("43", 1, "c")
    m.add("42", 3, "d")
    m.add("43", 2, "e")
    # 再加一条,淘汰最旧的 (42,1)
    m.add("44", 1, "f")
    assert m.lookup("42", 1) is None  # 最旧被淘汰
    assert m.lookup("42", 2) == "b"
    assert m.lookup("43", 1) == "c"
    assert m.lookup("44", 1) == "f"

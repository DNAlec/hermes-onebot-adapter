"""real_seq → message_id 映射表(全局 FIFO)。

NapCat 推送的消息事件里同时携带 ``message_id``(哈希短 ID,用于 API 操作)
和 ``real_seq``(NTQQ 原生群内/会话内递增序号,可读性好)。前缀展示 ``real_seq``,
LLM 调用工具时回传 ``real_seq``,本表把它转回 ``message_id`` 才能调 OneBot API。

**全局 FIFO**:与 NapCat 的 ``MessageUnique`` LRU(全局 5000 条 FIFO)对齐。
所有群/私聊消息共享一张 ``deque(maxlen=N)``,按插入顺序淘汰最旧。
默认上限 4500(略小于 NapCat 的 5000,留余量防止适配器有但 NapCat 已淘汰的假命中)。

key 为 ``(scope_id, real_seq)``:scope_id = group_id(群聊)或 user_id(私聊)。
real_seq 群内/会话内唯一,与 scope_id 组合全局唯一。

进程内存,重启清空。asyncio 单线程模型,无需加锁。
"""
from __future__ import annotations

import logging
from collections import deque

logger = logging.getLogger(__name__)


class SeqMap:
    """全局 FIFO 的 ``real_seq → message_id`` 映射。

    用 ``deque(maxlen=N)`` 存 ``(scope_id, real_seq, message_id)`` 元组,
    配合 ``dict[(scope_id, real_seq)] → message_id`` 做 O(1) 查找。
    超出上限丢弃最旧插入的条目(同时从 dict 删除),与 NapCat 的 FIFO 一致。
    """

    def __init__(self, maxlen: int = 4500) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._maxlen: int = maxlen
        self._buf: deque[tuple[str, int, str]] = deque(maxlen=maxlen)
        self._lut: dict[tuple[str, int], str] = {}

    @property
    def maxlen(self) -> int:
        return self._maxlen

    def add(self, scope_id: str, real_seq: int, message_id: str) -> None:
        """追加一条映射。``real_seq`` 重复时覆盖旧的 ``message_id``。"""
        if not scope_id or not message_id:
            return
        key = (scope_id, real_seq)
        # 已存在:覆盖 value,不重复入队
        if key in self._lut:
            self._lut[key] = message_id
            return
        # deque 将满:淘汰最旧条目(deque maxlen 自动 popleft,但需手动清 dict)
        if len(self._buf) >= self._maxlen:
            old_scope, old_seq, _ = self._buf[0]
            self._lut.pop((old_scope, old_seq), None)
        self._buf.append((scope_id, real_seq, message_id))
        self._lut[key] = message_id

    def lookup(self, scope_id: str, real_seq: int) -> str | None:
        """查 ``message_id``。未命中返回 ``None``。"""
        if not scope_id:
            return None
        return self._lut.get((scope_id, real_seq))

    def clear(self) -> None:
        """清空全部映射。"""
        self._buf.clear()
        self._lut.clear()

    def update_maxlen(self, new_maxlen: int) -> None:
        """热重载:重建 deque(因 ``deque.maxlen`` 创建后不可变)。

        保留现有映射条目(最旧的在新上限更小时被截断)。
        """
        if new_maxlen <= 0:
            raise ValueError("new_maxlen must be positive")
        if new_maxlen == self._maxlen:
            return
        old_buf = self._buf
        self._buf = deque(maxlen=new_maxlen)
        self._lut = {}
        self._maxlen = new_maxlen
        for scope_id, seq, mid in old_buf:
            if len(self._buf) >= new_maxlen:
                old_scope, old_seq, _ = self._buf[0]
                self._lut.pop((old_scope, old_seq), None)
            self._buf.append((scope_id, seq, mid))
            self._lut[(scope_id, seq)] = mid
        logger.debug("SeqMap: maxlen updated %d -> %d (entries=%d)",
                     new_maxlen, len(self._buf))


def _seq_map_add(seq_map: SeqMap, data: dict) -> None:
    """从原始 OneBot 事件提取 real_seq/message_id/scope_id 并存入 SeqMap。

    在 ws_reverse/ws_forward 的 ``_handle_text`` 里、``parse_event`` 之前调用,
    与 NapCat 的 ``onRecvMsg`` 对齐——所有消息(不论是否触发 bot)都进 FIFO。

    scope_id:群聊为 group_id,私聊为 user_id。
    """
    real_seq = str(data.get("real_seq", "") or "")
    message_id = str(data.get("message_id", "") or "")
    if not real_seq or not message_id:
        return
    if data.get("message_type") == "group":
        scope_id = str(data.get("group_id", ""))
    else:
        scope_id = str(data.get("user_id", ""))
    if not scope_id:
        return
    try:
        seq_map.add(scope_id, int(real_seq), message_id)
    except (ValueError, TypeError):
        pass

from __future__ import annotations

import asyncio
import json

from onebot_adapter.config import AdapterConfig
from onebot_adapter.onebot.handler import OneBotEventDispatcher, OneBotHandler
from onebot_adapter.onebot.seq_map import SeqMap


class _FakeHandler:
    def __init__(self) -> None:
        self.processed: list[str] = []
        self.responses: list[str] = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    def intercept_api_response(self, raw: str) -> bool:
        if not raw.startswith("response:"):
            return False
        self.responses.append(raw)
        return True

    async def handle_event_text(self, raw: str) -> None:
        if raw == "event:1":
            self.first_started.set()
            await self.release_first.wait()
        self.processed.append(raw)


async def test_dispatcher_preserves_event_order_and_fast_paths_responses():
    handler = _FakeHandler()
    dispatcher = OneBotEventDispatcher(handler, label="test", max_queue_size=4)  # type: ignore[arg-type]
    try:
        assert dispatcher.dispatch("event:1")
        assert dispatcher.dispatch("event:2")
        await asyncio.wait_for(handler.first_started.wait(), timeout=1)

        assert dispatcher.dispatch("response:echo")
        assert handler.responses == ["response:echo"]
        assert handler.processed == []

        handler.release_first.set()
        await asyncio.wait_for(dispatcher._queue.join(), timeout=1)
        assert handler.processed == ["event:1", "event:2"]
    finally:
        await dispatcher.stop()


async def test_dispatcher_bounds_pending_events():
    handler = _FakeHandler()
    dispatcher = OneBotEventDispatcher(handler, label="test", max_queue_size=1)  # type: ignore[arg-type]
    try:
        assert dispatcher.dispatch("event:1")
        assert not dispatcher.dispatch("event:2")
        assert dispatcher.queued == 1
        assert dispatcher.dropped == 1
    finally:
        await dispatcher.stop()


async def test_dispatcher_records_seq_map_before_drop():
    """Overflow drops the frame after SeqMap is updated, not before."""
    seq_map = SeqMap(maxlen=10)
    handler = OneBotHandler(
        label="test",
        config=AdapterConfig(onebot_ws_token="t", hermes_ws_token="t"),
        api=None,
        seq_map=seq_map,
    )
    dispatcher = OneBotEventDispatcher(handler, label="test", max_queue_size=1)
    try:
        first = json.dumps({
            "post_type": "message", "message_type": "group",
            "group_id": 42, "user_id": 1, "message_id": 10, "real_seq": "1",
            "message": [],
        })
        second = json.dumps({
            "post_type": "message", "message_type": "group",
            "group_id": 42, "user_id": 1, "message_id": 11, "real_seq": "2",
            "message": [],
        })
        assert dispatcher.dispatch(first)
        assert not dispatcher.dispatch(second)
        assert seq_map.lookup("42", 1) == "10"
        assert seq_map.lookup("42", 2) == "11"
    finally:
        await dispatcher.stop()

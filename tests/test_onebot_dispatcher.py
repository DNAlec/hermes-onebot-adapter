from __future__ import annotations

import asyncio

from onebot_adapter.onebot.handler import OneBotEventDispatcher


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

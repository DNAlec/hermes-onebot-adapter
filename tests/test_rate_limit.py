from __future__ import annotations

import sqlite3
import threading
import time

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore, GroupConfig
from onebot_adapter.rate_limit import MessageRateLimiter
from onebot_adapter.relay.protocol import NormalizedEvent


def _config(**overrides) -> AdapterConfig:
    values = {
        "onebot_ws_token": "onebot",
        "hermes_ws_token": "hermes",
        "rate_limit_enabled": True,
    }
    values.update(overrides)
    return AdapterConfig(**values)


def _event(*, user_id: str = "100", group_id: str | None = "42", eligible: bool = True):
    return NormalizedEvent(
        message_id="9",
        chat_id=f"group:{group_id}" if group_id else user_id,
        chat_type="group" if group_id else "dm",
        user_id=user_id,
        user_name="User",
        text="hello",
        rate_limit_eligible=eligible,
    )


async def test_sliding_window_expires():
    limiter = MessageRateLimiter()
    cfg = _config(global_rate_limit_messages=2, global_rate_limit_window_seconds=10)

    assert (await limiter.check(cfg, user_id="1", group_id=None, now=0)).allowed
    assert (await limiter.check(cfg, user_id="2", group_id=None, now=1)).allowed
    blocked = await limiter.check(cfg, user_id="3", group_id=None, now=2)
    assert not blocked.allowed
    assert blocked.scope == "global"
    assert blocked.retry_after == 8
    assert (await limiter.check(cfg, user_id="3", group_id=None, now=10)).allowed


async def test_token_bucket_refills_smoothly():
    limiter = MessageRateLimiter()
    cfg = _config(
        user_rate_limit_algorithm="token_bucket",
        user_rate_limit_messages=2,
        user_rate_limit_window_seconds=10,
    )

    assert (await limiter.check(cfg, user_id="1", group_id=None, now=0)).allowed
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=0)).allowed
    blocked = await limiter.check(cfg, user_id="1", group_id=None, now=0)
    assert not blocked.allowed
    assert blocked.retry_after == 5
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=5)).allowed


async def test_token_bucket_does_not_refill_twice_after_clock_rollback():
    limiter = MessageRateLimiter()
    cfg = _config(
        user_rate_limit_algorithm="token_bucket",
        user_rate_limit_messages=2,
        user_rate_limit_window_seconds=10,
    )

    assert (await limiter.check(cfg, user_id="1", group_id=None, now=100)).allowed
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=100)).allowed
    assert not (await limiter.check(cfg, user_id="1", group_id=None, now=90)).allowed
    assert not (await limiter.check(cfg, user_id="1", group_id=None, now=100)).allowed


async def test_scopes_are_atomic_and_user_scope_is_global():
    limiter = MessageRateLimiter()
    cfg = _config(
        group_rate_limit_messages=1,
        group_rate_limit_window_seconds=60,
        user_rate_limit_messages=2,
        user_rate_limit_window_seconds=60,
    )

    assert (await limiter.check(cfg, user_id="1", group_id="42", now=0)).allowed
    assert not (await limiter.check(cfg, user_id="1", group_id="42", now=1)).allowed
    # The rejected group message did not consume the user's second slot.
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=2)).allowed
    assert not (await limiter.check(cfg, user_id="1", group_id="99", now=3)).allowed


async def test_group_override_changes_policy_for_one_group():
    limiter = MessageRateLimiter()
    cfg = _config(
        group_rate_limit_messages=1,
        group_rate_limit_window_seconds=60,
        groups={
            "42": GroupConfig(
                group_id="42",
                group_rate_limit_messages=2,
                group_rate_limit_window_seconds=60,
            ).to_dict(),
        },
    )

    assert (await limiter.check(cfg, user_id="1", group_id="42", now=0)).allowed
    assert (await limiter.check(cfg, user_id="2", group_id="42", now=1)).allowed
    assert not (await limiter.check(cfg, user_id="3", group_id="42", now=2)).allowed
    assert (await limiter.check(cfg, user_id="1", group_id="99", now=0)).allowed
    assert not (await limiter.check(cfg, user_id="2", group_id="99", now=1)).allowed


async def test_service_rejects_with_reply_and_exempts_admin_and_member_notice():
    cfg = _config(
        global_admins=["1"],
        user_rate_limit_messages=1,
        user_rate_limit_window_seconds=60,
        rate_limit_reject_message="{scope}:{retry_after}:{user_id}",
    )
    service = AdapterService(ConfigStore(cfg))
    pushed = []
    rejected = []

    class Relay:
        has_clients = False

        async def push_event(self, event):
            pushed.append(event)
            return "broadcast"

        async def send_reject_message(self, chat_id, message, reply_to=None):
            rejected.append((chat_id, message, reply_to))
            return True

    service._relay = Relay()
    await service._on_onebot_event(_event(user_id="2"))
    await service._on_onebot_event(_event(user_id="2"))
    await service._on_onebot_event(_event(user_id="1"))
    await service._on_onebot_event(_event(user_id="2", eligible=False))

    assert len(pushed) == 3
    assert rejected == [("group:42", "个人:60:2", "9")]


def test_rate_limit_config_validation_and_group_resolvers():
    cfg = _config(global_rate_limit_messages=1, global_rate_limit_window_seconds=0)
    assert "global_rate_limit_window_seconds must be positive when the limit is enabled" in cfg.validate()
    invalid_storage = _config(rate_limit_storage_failure_mode="unknown")
    assert any("rate_limit_storage_failure_mode" in error for error in invalid_storage.validate())

    cfg = _config(
        group_rate_limit_messages=5,
        group_rate_limit_window_seconds=30,
        groups={
            "42": GroupConfig(
                group_id="42",
                group_rate_limit_algorithm="token_bucket",
                group_rate_limit_messages=2,
                group_rate_limit_window_seconds=10,
            ).to_dict(),
        },
    )
    assert cfg.validate() == []
    assert cfg.resolve_group_rate_limit_algorithm("42") == "token_bucket"
    assert cfg.resolve_group_rate_limit_messages("42") == 2
    assert cfg.resolve_group_rate_limit_window_seconds("42") == 10
    assert cfg.resolve_group_rate_limit_messages("99") == 5


async def test_persistent_sliding_window_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "rate_limit.sqlite3"
    cfg = _config(global_rate_limit_messages=1, global_rate_limit_window_seconds=60)
    monkeypatch.setattr("onebot_adapter.rate_limit.time.time", lambda: 1_700_000_000)
    first = MessageRateLimiter()
    await first.start(path)
    assert (await first.check(cfg, user_id="1", group_id=None, now=1_700_000_000)).allowed
    await first.close()

    reopened = MessageRateLimiter()
    await reopened.start(path)
    decision = await reopened.check(cfg, user_id="2", group_id=None, now=1_700_000_001)
    assert not decision.allowed
    assert decision.scope == "global"
    await reopened.close()


async def test_sliding_window_uses_incremental_event_rows(tmp_path):
    path = tmp_path / "rate_limit.sqlite3"
    cfg = _config(global_rate_limit_messages=3, global_rate_limit_window_seconds=60)
    limiter = MessageRateLimiter()
    await limiter.start(path)

    assert (await limiter.check(cfg, user_id="1", group_id=None, now=100)).allowed
    assert (await limiter.check(cfg, user_id="2", group_id=None, now=101)).allowed

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT timestamps_json FROM rate_limit_buckets").fetchone()[0] == "[]"
        assert conn.execute("SELECT COUNT(*) FROM rate_limit_sliding_events").fetchone()[0] == 2
    await limiter.close()


async def test_sqlite_commit_runs_outside_event_loop_thread(tmp_path, monkeypatch):
    path = tmp_path / "rate_limit.sqlite3"
    cfg = _config(global_rate_limit_messages=1, global_rate_limit_window_seconds=60)
    limiter = MessageRateLimiter()
    await limiter.start(path)
    assert limiter._store is not None
    original = limiter._store.save_consumption
    worker_threads: list[int] = []

    def record_thread(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(limiter._store, "save_consumption", record_thread)
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=100)).allowed
    assert worker_threads and worker_threads[0] != threading.get_ident()
    await limiter.close()


async def test_persistent_token_bucket_refills_while_stopped(tmp_path, monkeypatch):
    path = tmp_path / "rate_limit.sqlite3"
    cfg = _config(
        user_rate_limit_algorithm="token_bucket",
        user_rate_limit_messages=2,
        user_rate_limit_window_seconds=10,
    )
    monkeypatch.setattr("onebot_adapter.rate_limit.time.time", lambda: 1_700_000_000)
    first = MessageRateLimiter()
    await first.start(path)
    assert (await first.check(cfg, user_id="1", group_id=None)).allowed
    assert (await first.check(cfg, user_id="1", group_id=None)).allowed
    await first.close()

    monkeypatch.setattr("onebot_adapter.rate_limit.time.time", lambda: 1_700_000_005)
    reopened = MessageRateLimiter()
    await reopened.start(path)
    quota = await reopened.quota(cfg, "user", "1")
    assert quota["remaining"] == 1
    assert quota["used"] == 1
    await reopened.close()


async def test_disabling_rate_limit_preserves_bucket():
    limiter = MessageRateLimiter()
    enabled = _config(user_rate_limit_messages=1, user_rate_limit_window_seconds=60)
    disabled = enabled.with_overrides(rate_limit_enabled=False)
    assert (await limiter.check(enabled, user_id="1", group_id=None, now=0)).allowed
    assert (await limiter.check(disabled, user_id="1", group_id=None, now=1)).allowed
    assert not (await limiter.check(enabled, user_id="1", group_id=None, now=2)).allowed


async def test_reset_isolated_to_requested_scope():
    limiter = MessageRateLimiter()
    cfg = _config(
        global_rate_limit_messages=2,
        global_rate_limit_window_seconds=60,
        user_rate_limit_messages=1,
        user_rate_limit_window_seconds=60,
    )
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=0)).allowed
    result = await limiter.reset(cfg, "user", "1")
    assert result["cleared"] is True
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=1)).allowed
    assert not (await limiter.check(cfg, user_id="2", group_id=None, now=2)).allowed


async def test_storage_failure_falls_back_then_replays(tmp_path, monkeypatch):
    path = tmp_path / "rate_limit.sqlite3"
    cfg = _config(global_rate_limit_messages=1, global_rate_limit_window_seconds=60)
    limiter = MessageRateLimiter()
    await limiter.start(path)
    assert limiter._store is not None
    monkeypatch.setattr(
        limiter._store,
        "save_consumption",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")),
    )
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=time.time())).allowed
    assert limiter.storage_status()["status"] == "degraded"
    assert limiter.storage_status()["pending_operations"] == 1
    async with limiter._lock:
        await limiter._try_open_locked()
    assert limiter.storage_status()["status"] == "healthy"
    await limiter.close()

    reopened = MessageRateLimiter()
    await reopened.start(path)
    assert not (await reopened.check(cfg, user_id="2", group_id=None)).allowed
    await reopened.close()


async def test_recovery_preserves_token_debt_from_startup_fallback(tmp_path, monkeypatch):
    path = tmp_path / "rate_limit.sqlite3"
    now = 1_700_000_000.0
    cfg = _config(
        user_rate_limit_algorithm="token_bucket",
        user_rate_limit_messages=2,
        user_rate_limit_window_seconds=10,
    )
    monkeypatch.setattr("onebot_adapter.rate_limit.time.time", lambda: now)

    persisted = MessageRateLimiter()
    await persisted.start(path)
    assert (await persisted.check(cfg, user_id="1", group_id=None, now=now)).allowed
    assert (await persisted.check(cfg, user_id="1", group_id=None, now=now)).allowed
    await persisted.close()

    fallback = MessageRateLimiter()
    fallback._path = path
    fallback._status = "degraded"
    assert (await fallback.check(cfg, user_id="1", group_id=None, now=now)).allowed
    assert (await fallback.check(cfg, user_id="1", group_id=None, now=now)).allowed
    async with fallback._lock:
        await fallback._try_open_locked()

    quota = await fallback.quota(cfg, "user", "1")
    assert quota["used"] == 4
    assert quota["remaining"] == 0
    assert quota["next_available_in_seconds"] == 15
    assert quota["full_recovery_in_seconds"] == 20
    await fallback.close()


async def test_recovery_reports_correct_retry_for_overfull_sliding_window(tmp_path, monkeypatch):
    path = tmp_path / "rate_limit.sqlite3"
    now = 1_700_000_000.0
    cfg = _config(user_rate_limit_messages=2, user_rate_limit_window_seconds=10)
    monkeypatch.setattr("onebot_adapter.rate_limit.time.time", lambda: now + 4)

    persisted = MessageRateLimiter()
    await persisted.start(path)
    assert (await persisted.check(cfg, user_id="1", group_id=None, now=now)).allowed
    assert (await persisted.check(cfg, user_id="1", group_id=None, now=now + 1)).allowed
    await persisted.close()

    fallback = MessageRateLimiter()
    fallback._path = path
    fallback._status = "degraded"
    assert (await fallback.check(cfg, user_id="1", group_id=None, now=now + 2)).allowed
    assert (await fallback.check(cfg, user_id="1", group_id=None, now=now + 3)).allowed
    async with fallback._lock:
        await fallback._try_open_locked()

    quota = await fallback.quota(cfg, "user", "1")
    assert quota["used"] == 4
    assert quota["next_available_in_seconds"] == 8
    decision = await fallback.check(cfg, user_id="1", group_id=None, now=now + 4)
    assert not decision.allowed
    assert decision.retry_after == 8
    await fallback.close()


async def test_storage_reject_mode_rolls_back_failed_consumption(tmp_path, monkeypatch):
    path = tmp_path / "rate_limit.sqlite3"
    cfg = _config(
        global_rate_limit_messages=1,
        global_rate_limit_window_seconds=60,
        rate_limit_storage_failure_mode="reject",
    )
    limiter = MessageRateLimiter()
    await limiter.start(path)
    assert limiter._store is not None
    monkeypatch.setattr(
        limiter._store,
        "save_consumption",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")),
    )
    decision = await limiter.check(cfg, user_id="1", group_id=None)
    assert not decision.allowed
    assert decision.reason == "storage_unavailable"
    assert (await limiter.quota(cfg, "global"))["used"] == 0
    await limiter.close()


async def test_memory_fallback_rejects_when_pending_backlog_is_full(tmp_path, monkeypatch):
    path = tmp_path / "rate_limit.sqlite3"
    cfg = _config(user_rate_limit_messages=10, user_rate_limit_window_seconds=60)
    limiter = MessageRateLimiter()
    await limiter.start(path)
    assert limiter._store is not None
    monkeypatch.setattr("onebot_adapter.rate_limit._MAX_PENDING_OPERATIONS", 1)
    monkeypatch.setattr(
        limiter._store,
        "save_consumption",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")),
    )

    now = time.time()
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=now)).allowed
    rejected = await limiter.check(cfg, user_id="1", group_id=None, now=now + 1)
    assert not rejected.allowed
    assert rejected.reason == "storage_unavailable"
    assert limiter.storage_status()["pending_operations"] == 1
    assert limiter.storage_status()["fallback_exhausted"] is True
    assert (await limiter.quota(cfg, "user", "1"))["used"] == 1
    await limiter.close()


async def test_memory_pruning_keeps_unrecovered_token_debt(monkeypatch):
    limiter = MessageRateLimiter()
    cfg = _config(
        user_rate_limit_algorithm="token_bucket",
        user_rate_limit_messages=1,
        user_rate_limit_window_seconds=60,
    )
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=0)).allowed
    limiter._buckets[("user", "1")].tokens = -10

    limiter._prune_memory(301)
    monkeypatch.setattr("onebot_adapter.rate_limit.time.time", lambda: 301)
    quota = await limiter.quota(cfg, "user", "1")
    assert quota["tracked"] is True
    assert quota["used"] > 1

    limiter._prune_memory(1_000)
    assert (await limiter.quota(cfg, "user", "1"))["tracked"] is False

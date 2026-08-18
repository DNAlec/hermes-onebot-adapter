"""Persistent multi-scope inbound message rate limiting."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from onebot_adapter.config import RATE_LIMIT_SLIDING_WINDOW, RATE_LIMIT_STORAGE_REJECT, AdapterConfig

logger = logging.getLogger(__name__)
RateLimitScope = Literal["global", "group", "user"]
_GLOBAL_KEY = "*"
_RECOVERY_INTERVAL = 5.0
_MAX_PENDING_OPERATIONS = 50_000


@dataclass(frozen=True)
class RateLimitPolicy:
    algorithm: str
    messages: int
    window_seconds: float


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    scope: str = ""
    retry_after: float = 0.0
    reason: str = "quota"


class RateLimitStorageUnavailable(RuntimeError):
    """Raised when a management mutation cannot be queued safely."""


@dataclass
class _Bucket:
    policy: RateLimitPolicy
    timestamps: deque[float] = field(default_factory=deque)
    tokens: float = 0.0
    updated_at: float = 0.0
    last_seen: float = 0.0


@dataclass(frozen=True)
class _PendingOperation:
    kind: Literal["consume", "reset"]
    occurred_at: float
    entries: tuple[tuple[str, str, RateLimitPolicy], ...] = ()
    key: tuple[str, str] | None = None


class _RateLimitSqliteStore:
    """Small synchronous SQLite store serialized by the limiter's async lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def open(self) -> dict[tuple[str, str], _Bucket]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rate_limit_buckets (
                    scope TEXT NOT NULL CHECK(scope IN ('global', 'group', 'user')),
                    bucket_key TEXT NOT NULL,
                    algorithm TEXT NOT NULL,
                    messages INTEGER NOT NULL,
                    window_seconds REAL NOT NULL,
                    timestamps_json TEXT NOT NULL DEFAULT '[]',
                    tokens REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    PRIMARY KEY(scope, bucket_key)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rate_limit_sliding_events (
                    scope TEXT NOT NULL,
                    bucket_key TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    FOREIGN KEY(scope, bucket_key)
                        REFERENCES rate_limit_buckets(scope, bucket_key) ON DELETE CASCADE
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_rate_limit_sliding_events_bucket_time
                   ON rate_limit_sliding_events(scope, bucket_key, occurred_at)"""
            )
            self._migrate_json_timestamps(conn)
            conn.commit()
            self._conn = conn
            rows = conn.execute("SELECT * FROM rate_limit_buckets").fetchall()
            event_rows = conn.execute(
                "SELECT scope, bucket_key, occurred_at FROM rate_limit_sliding_events "
                "ORDER BY scope, bucket_key, occurred_at"
            ).fetchall()
        events: dict[tuple[str, str], deque[float]] = {}
        for row in event_rows:
            events.setdefault((str(row["scope"]), str(row["bucket_key"])), deque()).append(
                float(row["occurred_at"])
            )
        buckets: dict[tuple[str, str], _Bucket] = {}
        for row in rows:
            policy = RateLimitPolicy(row["algorithm"], int(row["messages"]), float(row["window_seconds"]))
            bucket_key = (str(row["scope"]), str(row["bucket_key"]))
            buckets[bucket_key] = _Bucket(
                policy=policy,
                timestamps=events.get(bucket_key, deque()),
                tokens=float(row["tokens"]),
                updated_at=float(row["updated_at"]),
                last_seen=float(row["last_seen"]),
            )
        return buckets

    def save_consumption(self, buckets: dict[tuple[str, str], _Bucket], occurred_at: float) -> None:
        conn = self._require_conn()
        with self._lock, conn:
            for (scope, key), bucket in buckets.items():
                previous = conn.execute(
                    "SELECT algorithm, messages, window_seconds FROM rate_limit_buckets "
                    "WHERE scope=? AND bucket_key=?",
                    (scope, key),
                ).fetchone()
                if previous is not None and (
                    previous["algorithm"] != bucket.policy.algorithm
                    or int(previous["messages"]) != bucket.policy.messages
                    or float(previous["window_seconds"]) != bucket.policy.window_seconds
                ):
                    conn.execute(
                        "DELETE FROM rate_limit_sliding_events WHERE scope=? AND bucket_key=?",
                        (scope, key),
                    )
                conn.execute(
                    """INSERT INTO rate_limit_buckets
                       (scope, bucket_key, algorithm, messages, window_seconds, timestamps_json,
                        tokens, updated_at, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(scope, bucket_key) DO UPDATE SET
                         algorithm=excluded.algorithm, messages=excluded.messages,
                         window_seconds=excluded.window_seconds,
                         timestamps_json=excluded.timestamps_json, tokens=excluded.tokens,
                         updated_at=excluded.updated_at, last_seen=excluded.last_seen""",
                    self._row(scope, key, bucket),
                )
                if bucket.policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
                    conn.execute(
                        "INSERT INTO rate_limit_sliding_events(scope, bucket_key, occurred_at) VALUES (?, ?, ?)",
                        (scope, key, occurred_at),
                    )
                    conn.execute(
                        "DELETE FROM rate_limit_sliding_events "
                        "WHERE scope=? AND bucket_key=? AND occurred_at<=?",
                        (scope, key, occurred_at - bucket.policy.window_seconds),
                    )

    def delete(self, bucket_key: tuple[str, str]) -> None:
        conn = self._require_conn()
        with self._lock, conn:
            conn.execute(
                "DELETE FROM rate_limit_buckets WHERE scope=? AND bucket_key=?",
                bucket_key,
            )

    def replace_all(self, buckets: dict[tuple[str, str], _Bucket]) -> None:
        conn = self._require_conn()
        with self._lock, conn:
            conn.execute("DELETE FROM rate_limit_buckets")
            for (scope, key), bucket in buckets.items():
                conn.execute(
                    "INSERT INTO rate_limit_buckets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._row(scope, key, bucket),
                )
                if bucket.policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
                    conn.executemany(
                        "INSERT INTO rate_limit_sliding_events(scope, bucket_key, occurred_at) VALUES (?, ?, ?)",
                        [(scope, key, value) for value in bucket.timestamps],
                    )

    @staticmethod
    def _row(scope: str, key: str, bucket: _Bucket) -> tuple[Any, ...]:
        return (
            scope, key, bucket.policy.algorithm, bucket.policy.messages, bucket.policy.window_seconds,
            "[]", bucket.tokens,
            bucket.updated_at, bucket.last_seen,
        )

    @staticmethod
    def _migrate_json_timestamps(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT scope, bucket_key, timestamps_json FROM rate_limit_buckets WHERE timestamps_json<>'[]'"
        ).fetchall()
        for row in rows:
            try:
                timestamps = [float(value) for value in json.loads(row["timestamps_json"])]
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning(
                    "discarding corrupt rate-limit timestamps scope=%s key=%s",
                    row["scope"], row["bucket_key"],
                )
                timestamps = []
            conn.executemany(
                "INSERT INTO rate_limit_sliding_events(scope, bucket_key, occurred_at) VALUES (?, ?, ?)",
                [(row["scope"], row["bucket_key"], value) for value in timestamps],
            )
            conn.execute(
                "UPDATE rate_limit_buckets SET timestamps_json='[]' WHERE scope=? AND bucket_key=?",
                (row["scope"], row["bucket_key"]),
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("rate-limit store is not open")
        return self._conn


class MessageRateLimiter:
    """Checks global, group and user limits atomically and persists accepted use."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = asyncio.Lock()
        self._checks = 0
        self._store: _RateLimitSqliteStore | None = None
        self._path: Path | None = None
        self._status: Literal["not_started", "healthy", "degraded", "recovering"] = "not_started"
        self._last_success_at: float | None = None
        self._pending: list[_PendingOperation] = []
        self._pending_limit_reached = False
        self._recovery_task: asyncio.Task[None] | None = None

    async def start(self, path: Path) -> None:
        self._path = path
        async with self._lock:
            await self._try_open_locked()
        self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def close(self) -> None:
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
            self._recovery_task = None
        async with self._lock:
            if self._status != "healthy" and self._pending:
                await self._try_open_locked()
            if self._store is not None:
                await asyncio.to_thread(self._store.close)
                self._store = None
            self._status = "not_started"

    async def check(
        self,
        config: AdapterConfig,
        *,
        user_id: str,
        group_id: str | None,
        now: float | None = None,
    ) -> RateLimitDecision:
        if not config.rate_limit_enabled:
            return RateLimitDecision(True)
        current = time.time() if now is None else now
        active = self._active_policies(config, user_id, group_id)
        if not active:
            return RateLimitDecision(True)

        async with self._lock:
            logical_times = [
                self._buckets[(scope, key)].updated_at
                for scope, key, _ in active
                if (scope, key) in self._buckets
            ]
            current = max([current, *logical_times])
            keys = [(scope, key) for scope, key, _ in active]
            original = {key: deepcopy(self._buckets.get(key)) for key in keys}
            blocked: list[tuple[str, float]] = []
            changed: dict[tuple[str, str], _Bucket] = {}
            for scope, key, policy in active:
                bucket = self._get_bucket(scope, key, policy, current)
                changed[(scope, key)] = bucket
                retry_after = self._retry_after(bucket, current)
                if retry_after > 0:
                    blocked.append((scope, retry_after))
            self._checks += 1
            if blocked:
                scope, retry_after = max(blocked, key=lambda item: item[1])
                return RateLimitDecision(False, scope, retry_after)

            for bucket in changed.values():
                if bucket.policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
                    bucket.timestamps.append(current)
                else:
                    bucket.tokens -= 1.0

            operation = _PendingOperation("consume", current, tuple(active))
            if self._status == "healthy" and self._store is not None:
                try:
                    await asyncio.to_thread(self._store.save_consumption, changed, current)
                    self._last_success_at = time.time()
                except Exception as exc:
                    await self._degrade_locked(exc)
                    if config.rate_limit_storage_failure_mode == RATE_LIMIT_STORAGE_REJECT:
                        self._restore(original)
                        return RateLimitDecision(False, reason="storage_unavailable", retry_after=5.0)
                    if not self._append_pending(operation):
                        self._restore(original)
                        return RateLimitDecision(False, reason="storage_unavailable", retry_after=5.0)
            elif self._path is not None:
                if config.rate_limit_storage_failure_mode == RATE_LIMIT_STORAGE_REJECT:
                    self._restore(original)
                    return RateLimitDecision(False, reason="storage_unavailable", retry_after=5.0)
                if not self._append_pending(operation):
                    self._restore(original)
                    return RateLimitDecision(False, reason="storage_unavailable", retry_after=5.0)

            if self._checks % 256 == 0:
                self._prune_memory(current)
            return RateLimitDecision(True)

    async def quota(
        self, config: AdapterConfig, scope: RateLimitScope, target_id: str | None = None,
    ) -> dict[str, Any]:
        current = time.time()
        key, policy = self._scope_policy(config, scope, target_id)
        async with self._lock:
            bucket = self._buckets.get((scope, key))
            if bucket is None or bucket.policy != policy:
                bucket = _Bucket(policy, tokens=float(policy.messages), updated_at=current, last_seen=current)
                tracked = False
            else:
                bucket = deepcopy(bucket)
                self._retry_after(bucket, current)
                tracked = True
            current = bucket.updated_at
            if policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
                used = float(len(bucket.timestamps))
                remaining = float(max(0, policy.messages - len(bucket.timestamps)))
                if remaining >= 1 or not bucket.timestamps:
                    next_available = 0.0
                else:
                    next_index = len(bucket.timestamps) - policy.messages
                    next_available = max(
                        0.0, bucket.timestamps[next_index] + policy.window_seconds - current,
                    )
                full_recovery = 0.0 if not bucket.timestamps else max(
                    0.0, bucket.timestamps[-1] + policy.window_seconds - current,
                )
            else:
                remaining = max(0.0, min(float(policy.messages), bucket.tokens))
                used = max(0.0, float(policy.messages) - bucket.tokens)
                refill_rate = (
                    policy.messages / policy.window_seconds
                    if policy.messages and policy.window_seconds else 0
                )
                next_available = (
                    0.0 if bucket.tokens >= 1 or not refill_rate
                    else (1.0 - bucket.tokens) / refill_rate
                )
                full_recovery = 0.0 if not refill_rate else used / refill_rate
            return {
                "scope": scope,
                "target_id": None if scope == "global" else target_id,
                "rate_limit_enabled": config.rate_limit_enabled,
                "scope_enabled": policy.messages > 0 and policy.window_seconds > 0,
                "algorithm": policy.algorithm,
                "limit": policy.messages,
                "window_seconds": policy.window_seconds,
                "tracked": tracked,
                "used": used,
                "remaining": remaining,
                "next_available_in_seconds": next_available,
                "full_recovery_in_seconds": full_recovery,
                "persistence": {
                    **self.storage_status(),
                    "failure_mode": config.rate_limit_storage_failure_mode,
                },
            }

    async def reset(
        self, config: AdapterConfig, scope: RateLimitScope, target_id: str | None = None,
    ) -> dict[str, Any]:
        key, _ = self._scope_policy(config, scope, target_id)
        bucket_key = (scope, key)
        async with self._lock:
            previous = self._buckets.pop(bucket_key, None)
            cleared = previous is not None
            operation = _PendingOperation("reset", time.time(), key=bucket_key)
            if self._status == "healthy" and self._store is not None:
                try:
                    await asyncio.to_thread(self._store.delete, bucket_key)
                    self._last_success_at = time.time()
                except Exception as exc:
                    await self._degrade_locked(exc)
                    self._remove_pending_for_key(bucket_key)
                    if not self._append_pending(operation):
                        if previous is not None:
                            self._buckets[bucket_key] = previous
                        raise RateLimitStorageUnavailable("rate-limit persistence backlog is full") from exc
            elif self._path is not None:
                self._remove_pending_for_key(bucket_key)
                if not self._append_pending(operation):
                    if previous is not None:
                        self._buckets[bucket_key] = previous
                    raise RateLimitStorageUnavailable("rate-limit persistence backlog is full")
        result = await self.quota(config, scope, target_id)
        result["cleared"] = cleared
        result["pending_persistence"] = self._status != "healthy"
        return result

    def storage_status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "last_success_at": self._last_success_at,
            "pending_operations": len(self._pending),
            "pending_limit": _MAX_PENDING_OPERATIONS,
            "fallback_exhausted": self._pending_limit_reached,
        }

    def clear(self) -> None:
        """Compatibility helper for direct users; disabling no longer calls it."""
        self._buckets.clear()

    async def _recovery_loop(self) -> None:
        while True:
            await asyncio.sleep(_RECOVERY_INTERVAL)
            if self._status == "healthy" or self._path is None:
                continue
            async with self._lock:
                await self._try_open_locked()

    async def _try_open_locked(self) -> None:
        if self._path is None:
            return
        repeated_attempt = self._status == "degraded"
        self._status = "recovering"
        store = _RateLimitSqliteStore(self._path)
        try:
            persisted = await asyncio.to_thread(store.open)
            for operation in self._pending:
                self._replay_operation(persisted, operation)
            self._compact_buckets(persisted, time.time())
            await asyncio.to_thread(store.replace_all, persisted)
        except Exception as exc:
            await asyncio.to_thread(store.close)
            self._status = "degraded"
            log = logger.debug if repeated_attempt else logger.error
            log("rate-limit persistence unavailable: %s", exc)
            return
        if self._store is not None:
            await asyncio.to_thread(self._store.close)
        self._store = store
        self._buckets = persisted
        self._pending.clear()
        self._pending_limit_reached = False
        self._status = "healthy"
        self._last_success_at = time.time()
        logger.info("rate-limit persistence healthy path=%s", self._path)

    async def _degrade_locked(self, exc: Exception) -> None:
        logger.error("rate-limit persistence write failed; entering degraded mode: %s", exc)
        if self._store is not None:
            await asyncio.to_thread(self._store.close)
            self._store = None
        self._status = "degraded"

    def _replay_operation(
        self, buckets: dict[tuple[str, str], _Bucket], operation: _PendingOperation,
    ) -> None:
        if operation.kind == "reset":
            if operation.key is not None:
                buckets.pop(operation.key, None)
            return
        for scope, key, policy in operation.entries:
            bucket_key = (scope, key)
            bucket = buckets.get(bucket_key)
            if bucket is None or bucket.policy != policy:
                bucket = _Bucket(policy, tokens=float(policy.messages), updated_at=operation.occurred_at)
                buckets[bucket_key] = bucket
            effective_at = max(operation.occurred_at, bucket.updated_at)
            bucket.last_seen = effective_at
            self._retry_after(bucket, effective_at)
            if policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
                bucket.timestamps.append(effective_at)
            else:
                # Recovery can merge fallback consumption with an already-empty
                # persisted bucket. Preserve that debt so every accepted message
                # delays subsequent availability instead of disappearing at zero.
                bucket.tokens -= 1.0

    @staticmethod
    def _active_policies(
        config: AdapterConfig, user_id: str, group_id: str | None,
    ) -> list[tuple[str, str, RateLimitPolicy]]:
        policies = [
            ("global", _GLOBAL_KEY, RateLimitPolicy(
                config.global_rate_limit_algorithm, config.global_rate_limit_messages,
                config.global_rate_limit_window_seconds,
            )),
            ("user", str(user_id), RateLimitPolicy(
                config.user_rate_limit_algorithm, config.user_rate_limit_messages,
                config.user_rate_limit_window_seconds,
            )),
        ]
        if group_id is not None:
            policies.append(("group", str(group_id), RateLimitPolicy(
                config.resolve_group_rate_limit_algorithm(group_id),
                config.resolve_group_rate_limit_messages(group_id),
                config.resolve_group_rate_limit_window_seconds(group_id),
            )))
        return [
            (scope, key, policy) for scope, key, policy in policies
            if policy.messages > 0 and policy.window_seconds > 0
        ]

    @staticmethod
    def _scope_policy(
        config: AdapterConfig, scope: RateLimitScope, target_id: str | None,
    ) -> tuple[str, RateLimitPolicy]:
        if scope == "global":
            return _GLOBAL_KEY, RateLimitPolicy(
                config.global_rate_limit_algorithm, config.global_rate_limit_messages,
                config.global_rate_limit_window_seconds,
            )
        if target_id is None:
            raise ValueError("target_id is required")
        if scope == "group":
            return target_id, RateLimitPolicy(
                config.resolve_group_rate_limit_algorithm(target_id),
                config.resolve_group_rate_limit_messages(target_id),
                config.resolve_group_rate_limit_window_seconds(target_id),
            )
        return target_id, RateLimitPolicy(
            config.user_rate_limit_algorithm, config.user_rate_limit_messages,
            config.user_rate_limit_window_seconds,
        )

    def _get_bucket(self, scope: str, key: str, policy: RateLimitPolicy, now: float) -> _Bucket:
        bucket_key = (scope, key)
        bucket = self._buckets.get(bucket_key)
        if bucket is None or bucket.policy != policy:
            bucket = _Bucket(policy=policy, tokens=float(policy.messages), updated_at=now, last_seen=now)
            self._buckets[bucket_key] = bucket
        bucket.last_seen = now
        return bucket

    @staticmethod
    def _retry_after(bucket: _Bucket, now: float) -> float:
        policy = bucket.policy
        now = max(now, bucket.updated_at)
        if policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
            cutoff = now - policy.window_seconds
            while bucket.timestamps and bucket.timestamps[0] <= cutoff:
                bucket.timestamps.popleft()
            if len(bucket.timestamps) < policy.messages:
                bucket.updated_at = now
                return 0.0
            bucket.updated_at = now
            next_index = len(bucket.timestamps) - policy.messages
            return max(0.0, bucket.timestamps[next_index] + policy.window_seconds - now)
        elapsed = max(0.0, now - bucket.updated_at)
        refill_rate = policy.messages / policy.window_seconds
        bucket.tokens = min(float(policy.messages), bucket.tokens + elapsed * refill_rate)
        bucket.updated_at = now
        if bucket.tokens >= 1.0:
            return 0.0
        return (1.0 - bucket.tokens) / refill_rate

    def _append_pending(self, operation: _PendingOperation) -> bool:
        if len(self._pending) >= _MAX_PENDING_OPERATIONS:
            if not self._pending_limit_reached:
                logger.error(
                    "rate-limit persistence backlog reached %d operations; rejecting until recovery",
                    _MAX_PENDING_OPERATIONS,
                )
            self._pending_limit_reached = True
            return False
        self._pending.append(operation)
        return True

    def _remove_pending_for_key(self, bucket_key: tuple[str, str]) -> None:
        compacted: list[_PendingOperation] = []
        for operation in self._pending:
            if operation.kind == "reset":
                if operation.key != bucket_key:
                    compacted.append(operation)
                continue
            entries = tuple(entry for entry in operation.entries if entry[:2] != bucket_key)
            if entries:
                compacted.append(_PendingOperation("consume", operation.occurred_at, entries))
        self._pending = compacted
        if len(self._pending) < _MAX_PENDING_OPERATIONS:
            self._pending_limit_reached = False

    def _restore(self, original: dict[tuple[str, str], _Bucket | None]) -> None:
        for key, bucket in original.items():
            if bucket is None:
                self._buckets.pop(key, None)
            else:
                self._buckets[key] = bucket

    def _compact_buckets(self, buckets: dict[tuple[str, str], _Bucket], now: float) -> None:
        recovered: list[tuple[str, str]] = []
        for key, bucket in buckets.items():
            self._retry_after(bucket, now)
            if bucket.policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
                is_recovered = not bucket.timestamps
            else:
                is_recovered = bucket.tokens >= bucket.policy.messages
            if is_recovered:
                recovered.append(key)
        for key in recovered:
            del buckets[key]

    def _prune_memory(self, now: float) -> None:
        recovered: list[tuple[str, str]] = []
        for key, bucket in self._buckets.items():
            if now - bucket.last_seen <= max(300.0, bucket.policy.window_seconds * 2):
                continue
            self._retry_after(bucket, now)
            if bucket.policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
                is_recovered = not bucket.timestamps
            else:
                is_recovered = bucket.tokens >= bucket.policy.messages
            if is_recovered:
                recovered.append(key)
        for key in recovered:
            del self._buckets[key]

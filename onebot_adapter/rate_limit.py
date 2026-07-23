"""In-memory multi-scope inbound message rate limiting."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from onebot_adapter.config import (
    RATE_LIMIT_SLIDING_WINDOW,
    AdapterConfig,
)


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


@dataclass
class _Bucket:
    policy: RateLimitPolicy
    timestamps: deque[float] = field(default_factory=deque)
    tokens: float = 0.0
    updated_at: float = 0.0
    last_seen: float = 0.0


class MessageRateLimiter:
    """Checks global, per-group, and globally keyed per-user limits atomically."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = asyncio.Lock()
        self._checks = 0

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

        current = time.monotonic() if now is None else now
        policies = [
            (
                "global",
                "*",
                RateLimitPolicy(
                    config.global_rate_limit_algorithm,
                    config.global_rate_limit_messages,
                    config.global_rate_limit_window_seconds,
                ),
            ),
            (
                "user",
                str(user_id),
                RateLimitPolicy(
                    config.user_rate_limit_algorithm,
                    config.user_rate_limit_messages,
                    config.user_rate_limit_window_seconds,
                ),
            ),
        ]
        if group_id is not None:
            policies.append(
                (
                    "group",
                    str(group_id),
                    RateLimitPolicy(
                        config.resolve_group_rate_limit_algorithm(group_id),
                        config.resolve_group_rate_limit_messages(group_id),
                        config.resolve_group_rate_limit_window_seconds(group_id),
                    ),
                )
            )
        active = [
            (scope, key, policy)
            for scope, key, policy in policies
            if policy.messages > 0 and policy.window_seconds > 0
        ]
        if not active:
            return RateLimitDecision(True)

        async with self._lock:
            blocked: list[tuple[str, float]] = []
            buckets: list[_Bucket] = []
            for scope, key, policy in active:
                bucket = self._get_bucket(scope, key, policy, current)
                buckets.append(bucket)
                retry_after = self._retry_after(bucket, current)
                if retry_after > 0:
                    blocked.append((scope, retry_after))

            self._checks += 1
            if self._checks % 256 == 0:
                self._prune(current)
            if blocked:
                scope, retry_after = max(blocked, key=lambda item: item[1])
                return RateLimitDecision(False, scope, retry_after)

            for bucket in buckets:
                if bucket.policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
                    bucket.timestamps.append(current)
                else:
                    bucket.tokens -= 1.0
            return RateLimitDecision(True)

    def clear(self) -> None:
        self._buckets.clear()

    def _get_bucket(self, scope: str, key: str, policy: RateLimitPolicy, now: float) -> _Bucket:
        bucket_key = (scope, key)
        bucket = self._buckets.get(bucket_key)
        if bucket is None or bucket.policy != policy:
            bucket = _Bucket(
                policy=policy,
                tokens=float(policy.messages),
                updated_at=now,
                last_seen=now,
            )
            self._buckets[bucket_key] = bucket
        bucket.last_seen = now
        return bucket

    @staticmethod
    def _retry_after(bucket: _Bucket, now: float) -> float:
        policy = bucket.policy
        if policy.algorithm == RATE_LIMIT_SLIDING_WINDOW:
            cutoff = now - policy.window_seconds
            while bucket.timestamps and bucket.timestamps[0] <= cutoff:
                bucket.timestamps.popleft()
            if len(bucket.timestamps) < policy.messages:
                return 0.0
            return max(0.0, bucket.timestamps[0] + policy.window_seconds - now)

        elapsed = max(0.0, now - bucket.updated_at)
        refill_rate = policy.messages / policy.window_seconds
        bucket.tokens = min(float(policy.messages), bucket.tokens + elapsed * refill_rate)
        bucket.updated_at = now
        if bucket.tokens >= 1.0:
            return 0.0
        return (1.0 - bucket.tokens) / refill_rate

    def _prune(self, now: float) -> None:
        stale = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.last_seen > max(300.0, bucket.policy.window_seconds * 2)
        ]
        for key in stale:
            del self._buckets[key]

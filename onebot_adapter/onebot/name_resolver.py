"""OneBot user/group name resolution with in-memory TTL cache.

Resolves QQ user IDs to display names for @ mention rendering, and QQ
group IDs to group names for session context and message labels.
Group chats: prefers group card (群名片) → nickname, with stranger_info fallback.
DMs: uses nickname from get_stranger_info.

Cache properties:
  - TTL: 10 minutes for successful lookups.
  - Failures are NOT cached — a transient API error doesn't blacklist a
    user for 10 minutes; the next call retries immediately.
  - Bounded size (default 4096 entries) with FIFO eviction to prevent
    unbounded memory growth on long-running bots with many transient
    users. Eviction removes the oldest inserted entry.
  - Per-key ``asyncio.Lock`` deduplicates concurrent lookups for the same
    key without serialising unrelated lookups (the previous single global
    lock made all resolution serial).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_TTL = 600  # 10 minutes for successful lookups
_CACHE_MAX = 4096  # bounded cache size (FIFO eviction)


class NameResolver:
    """Resolves QQ user IDs to display names with caching.

    Cache key format: ``f"{group_id}:{user_id}"`` for group lookups,
    ``f"dm:{user_id}"`` for DM lookups. Each entry stores ``(name, expiry_ts)``.
    """

    def __init__(self, api: Any) -> None:
        self._api = api
        self._cache: dict[str, tuple[str, float]] = {}
        self._keys_order: deque[str] = deque()  # FIFO for size eviction
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _evict_if_needed(self) -> None:
        """FIFO-evict oldest entries when the cache exceeds _CACHE_MAX.

        Skips keys whose lock is currently held (in-flight lookup) so that
        a concurrent ``resolve()`` for the same key doesn't create a new lock
        and bypass the per-key dedup. If all keys are locked, evicts the
        oldest anyway to prevent unbounded growth — the in-flight lookup for
        that key will simply re-fetch on its next call (it hasn't stored yet).
        """
        skipped = 0
        while len(self._cache) > _CACHE_MAX and self._keys_order:
            if skipped >= len(self._keys_order):
                # Every key is locked — evict the oldest to bound memory.
                # Its in-flight lookup hasn't stored a result yet, so removing
                # the (non-existent) cache entry is harmless; the lock stays.
                old_key = self._keys_order[0]
                self._keys_order.popleft()
                self._cache.pop(old_key, None)
                # Don't pop the lock — it's still held by an in-flight lookup.
                skipped = 0
                continue
            old_key = self._keys_order[0]
            lock = self._locks.get(old_key)
            if lock is not None and lock.locked():
                # This key has an in-flight lookup; skip eviction to preserve
                # the dedup guarantee. Rotate to the back and try the next key.
                self._keys_order.rotate(-1)
                skipped += 1
                continue
            self._keys_order.popleft()
            self._cache.pop(old_key, None)
            self._locks.pop(old_key, None)
            skipped = 0  # reset after a successful eviction

    def _store(self, key: str, name: str) -> None:
        """Cache a successful lookup with TTL."""
        if key not in self._cache:
            self._keys_order.append(key)
        self._cache[key] = (name, time.time() + _CACHE_TTL)
        self._evict_if_needed()

    async def resolve(self, user_id: str, group_id: str = "") -> str:
        """Resolve a QQ user ID to a display name.

        Returns the name string, or empty string if all API calls fail.
        Cached results have a 10-minute TTL; concurrent lookups for the
        same key are deduplicated via a per-key lock. Failed lookups are
        NOT cached so a transient error doesn't blacklist a user.  The
        per-key lock created for a failed lookup is cleaned up afterwards
        so the ``_locks`` dict doesn't grow unbounded from transient errors.
        """
        if not user_id:
            return ""
        cache_key = f"{group_id}:{user_id}" if group_id else f"dm:{user_id}"

        # Fast path: cache hit (no lock needed for read)
        cached = self._cache.get(cache_key)
        if cached and cached[1] > time.time():
            return cached[0]

        # Slow path: acquire per-key lock to dedupe concurrent lookups
        async with self._get_lock(cache_key):
            # Double-check after acquiring lock
            cached = self._cache.get(cache_key)
            if cached and cached[1] > time.time():
                return cached[0]

            name = await self._fetch_name(user_id, group_id)
            # Only cache successful (non-empty) results; failures return ""
            # without caching so the next call retries immediately.
            if name:
                self._store(cache_key, name)

        # Clean up the lock for failed lookups so _locks doesn't leak:
        # a failed key is never stored in _cache/_keys_order, so the
        # eviction loop can't reach it.  Only clean up when no other
        # coroutine is waiting on the same lock (locked() is False).
        if not name:
            lock = self._locks.get(cache_key)
            if lock is not None and not lock.locked():
                self._locks.pop(cache_key, None)
        return name

    async def resolve_group_name(self, group_id: str) -> str:
        """Resolve a QQ group ID to its group name (群名).

        Returns the group name string, or empty string if the API call
        fails.  Cached under ``grp:<group_id>`` with a 10-minute TTL;
        concurrent lookups are deduplicated via a per-key lock.  Failed
        lookups are NOT cached.  The per-key lock for a failed lookup is
        cleaned up afterwards so ``_locks`` doesn't leak.
        """
        if not group_id:
            return ""
        cache_key = f"grp:{group_id}"

        # Fast path
        cached = self._cache.get(cache_key)
        if cached and cached[1] > time.time():
            return cached[0]

        async with self._get_lock(cache_key):
            cached = self._cache.get(cache_key)
            if cached and cached[1] > time.time():
                return cached[0]

            name = await self._fetch_group_name(group_id)
            if name:
                self._store(cache_key, name)

        # Clean up the lock for failed lookups (see resolve() for details).
        if not name:
            lock = self._locks.get(cache_key)
            if lock is not None and not lock.locked():
                self._locks.pop(cache_key, None)
        return name

    async def _fetch_group_name(self, group_id: str) -> str:
        """Fetch group name from OneBot API."""
        try:
            info = await self._api.get_group_info(int(group_id), no_cache=False)
            name = info.get("group_name") or ""
            logger.debug("name_resolver: resolved group_name group=%s -> %r", group_id, name)
            return name
        except Exception as exc:
            logger.warning("group name resolution failed for group %s: %s", group_id, exc)
            return ""

    async def _fetch_name(self, user_id: str, group_id: str) -> str:
        """Fetch name from OneBot API with fallback."""
        try:
            if group_id:
                try:
                    info = await self._api.get_group_member_info(
                        int(group_id), int(user_id), no_cache=False,
                    )
                    name = info.get("card") or info.get("nickname") or ""
                    if name:
                        logger.debug(
                            "name_resolver: resolved group_member group=%s user=%s -> %r",
                            group_id, user_id, name,
                        )
                        return name
                    # card and nickname both empty — fall through to stranger_info
                except Exception:
                    # Group member lookup failed (user not in group, etc.)
                    # Fall through to stranger_info as fallback
                    logger.debug(
                        "get_group_member_info failed for %s:%s, trying stranger_info",
                        group_id, user_id, exc_info=True,
                    )

                # Fallback: get_stranger_info
                try:
                    info = await self._api.get_stranger_info(int(user_id))
                    name = info.get("nickname") or ""
                    logger.debug("name_resolver: resolved stranger_info user=%s -> %r", user_id, name)
                    return name
                except Exception as exc:
                    logger.warning("name resolution failed for user %s: %s", user_id, exc)
                    return ""
            else:
                info = await self._api.get_stranger_info(int(user_id))
                name = info.get("nickname") or ""
                logger.debug("name_resolver: resolved stranger_info(dm) user=%s -> %r", user_id, name)
                return name
        except Exception as exc:
            logger.warning("name resolution failed for user %s: %s", user_id, exc)
            return ""

"""Tests for the NameResolver class."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from onebot_adapter.onebot.name_resolver import NameResolver


def _mock_api(
    *,
    group_member_responses: dict[tuple[int, int], dict] | None = None,
    stranger_responses: dict[int, dict] | None = None,
    group_info_responses: dict[int, dict] | None = None,
) -> MagicMock:
    api = MagicMock()
    gm = group_member_responses or {}
    sm = stranger_responses or {}
    gi = group_info_responses or {}

    async def _get_group_member_info(group_id, user_id, no_cache=False):
        return gm.get((group_id, user_id), {"card": "", "nickname": ""})

    async def _get_stranger_info(user_id, no_cache=True):
        return sm.get(user_id, {"nickname": ""})

    async def _get_group_info(group_id, no_cache=True):
        return gi.get(group_id, {"group_name": ""})

    api.get_group_member_info = _get_group_member_info
    api.get_stranger_info = _get_stranger_info
    api.get_group_info = _get_group_info
    return api


# ── Group resolution ────────────────────────────────────────────────────


async def test_resolve_group_with_card():
    api = _mock_api(group_member_responses={(42, 123): {"card": "张三A", "nickname": "张三"}})
    resolver = NameResolver(api)
    name = await resolver.resolve("123", "42")
    assert name == "张三A"


async def test_resolve_group_card_empty_falls_back_to_nickname():
    api = _mock_api(group_member_responses={(42, 456): {"card": "", "nickname": "李四"}})
    resolver = NameResolver(api)
    name = await resolver.resolve("456", "42")
    assert name == "李四"


async def test_resolve_group_both_empty_falls_back_to_stranger():
    """When card and nickname both empty, fall back to get_stranger_info."""
    api = _mock_api(
        group_member_responses={(42, 789): {"card": "", "nickname": ""}},
        stranger_responses={789: {"nickname": "王五"}},
    )
    resolver = NameResolver(api)
    name = await resolver.resolve("789", "42")
    assert name == "王五"


async def test_resolve_group_member_lookup_fails_falls_back_to_stranger():
    """When get_group_member_info raises, fall back to get_stranger_info."""
    api = MagicMock()

    async def _failing_gmi(group_id, user_id, no_cache=False):
        raise RuntimeError("user not in group")

    async def _ok_stranger(user_id, no_cache=True):
        return {"nickname": "赵六"}

    api.get_group_member_info = _failing_gmi
    api.get_stranger_info = _ok_stranger
    resolver = NameResolver(api)
    name = await resolver.resolve("999", "42")
    assert name == "赵六"


# ── DM resolution ────────────────────────────────────────────────────────


async def test_resolve_dm():
    api = _mock_api(stranger_responses={111: {"nickname": "DirectUser"}})
    resolver = NameResolver(api)
    name = await resolver.resolve("111")
    assert name == "DirectUser"


async def test_resolve_dm_failure_returns_empty():
    api = MagicMock()

    async def _failing(user_id, no_cache=True):
        raise RuntimeError("not found")

    api.get_stranger_info = _failing
    resolver = NameResolver(api)
    name = await resolver.resolve("222")
    assert name == ""


async def test_failure_not_cached_retries_on_next_call():
    """Failed lookups should not be cached so the next call retries.

    Both get_group_member_info and get_stranger_info must fail on the first
    call (so the result is ""), then succeed on the second call to prove the
    failure wasn't cached.
    """
    call_count = 0
    api = MagicMock()

    async def _gmi(group_id, user_id, no_cache=False):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return {"card": "Later", "nickname": "L"}

    async def _stranger(user_id, no_cache=True):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RuntimeError("transient stranger")
        return {"nickname": "StrangerOK"}

    api.get_group_member_info = _gmi
    api.get_stranger_info = _stranger
    resolver = NameResolver(api)

    name1 = await resolver.resolve("123", "42")
    assert name1 == ""  # both APIs failed
    # Not cached → next call retries and succeeds
    name2 = await resolver.resolve("123", "42")
    assert name2 == "Later"
    assert call_count >= 3  # retried both APIs on the second call


# ── Caching ──────────────────────────────────────────────────────────────


async def test_cache_hit_avoids_api_call():
    call_count = 0
    api = MagicMock()

    async def _gmi(group_id, user_id, no_cache=False):
        nonlocal call_count
        call_count += 1
        return {"card": "Cached", "nickname": "CN"}

    api.get_group_member_info = _gmi
    api.get_stranger_info = AsyncMock(return_value={"nickname": "S"})
    resolver = NameResolver(api)

    # First call hits the API
    name1 = await resolver.resolve("123", "42")
    assert name1 == "Cached"
    assert call_count == 1

    # Second call uses cache
    name2 = await resolver.resolve("123", "42")
    assert name2 == "Cached"
    assert call_count == 1


async def test_cache_expiry_triggers_new_fetch():
    api = MagicMock()
    call_count = 0

    async def _gmi(group_id, user_id, no_cache=False):
        nonlocal call_count
        call_count += 1
        return {"card": f"Name{call_count}", "nickname": "N"}

    api.get_group_member_info = _gmi
    api.get_stranger_info = AsyncMock(return_value={"nickname": "S"})
    resolver = NameResolver(api)

    name1 = await resolver.resolve("123", "42")
    assert name1 == "Name1"
    assert call_count == 1

    # Manually expire the cache
    for key in list(resolver._cache.keys()):
        resolver._cache[key] = (resolver._cache[key][0], time.time() - 1)

    name2 = await resolver.resolve("123", "42")
    assert name2 == "Name2"
    assert call_count == 2


async def test_empty_user_id_returns_empty():
    api = MagicMock()
    resolver = NameResolver(api)
    name = await resolver.resolve("")
    assert name == ""


async def test_concurrent_lookups_deduplicated():
    """Multiple concurrent resolve() calls for same key should only hit API once."""
    call_count = 0
    api = MagicMock()

    async def _gmi(group_id, user_id, no_cache=False):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # Simulate slow API
        return {"card": "Concurrent", "nickname": "C"}

    api.get_group_member_info = _gmi
    api.get_stranger_info = AsyncMock(return_value={"nickname": "S"})
    resolver = NameResolver(api)

    # Launch 5 concurrent resolves for the same key
    results = await asyncio.gather(*[resolver.resolve("123", "42") for _ in range(5)])
    assert all(r == "Concurrent" for r in results)
    # Due to lock deduplication, only one API call should be made
    assert call_count == 1


# ── Group name resolution ───────────────────────────────────────────────


async def test_resolve_group_name_basic():
    api = _mock_api(group_info_responses={42: {"group_name": "测试群"}})
    resolver = NameResolver(api)
    name = await resolver.resolve_group_name("42")
    assert name == "测试群"


async def test_resolve_group_name_empty_when_api_fails():
    api = MagicMock()

    async def _failing(group_id, no_cache=True):
        raise RuntimeError("group not found")

    api.get_group_info = _failing
    resolver = NameResolver(api)
    name = await resolver.resolve_group_name("999")
    assert name == ""


async def test_resolve_group_name_empty_group_name_field():
    """When group_name field is empty/missing, return empty string."""
    api = _mock_api(group_info_responses={42: {"group_name": ""}})
    resolver = NameResolver(api)
    name = await resolver.resolve_group_name("42")
    assert name == ""


async def test_resolve_group_name_empty_group_id_returns_empty():
    api = MagicMock()
    resolver = NameResolver(api)
    name = await resolver.resolve_group_name("")
    assert name == ""


async def test_resolve_group_name_cached():
    """Second call for the same group should use cache, not hit API again."""
    call_count = 0
    api = MagicMock()

    async def _ggi(group_id, no_cache=True):
        nonlocal call_count
        call_count += 1
        return {"group_name": "Cached群"}

    api.get_group_info = _ggi
    resolver = NameResolver(api)

    name1 = await resolver.resolve_group_name("42")
    assert name1 == "Cached群"
    assert call_count == 1

    name2 = await resolver.resolve_group_name("42")
    assert name2 == "Cached群"
    assert call_count == 1


async def test_resolve_group_name_cache_expiry():
    api = MagicMock()
    call_count = 0

    async def _ggi(group_id, no_cache=True):
        nonlocal call_count
        call_count += 1
        return {"group_name": f"群{call_count}"}

    api.get_group_info = _ggi
    resolver = NameResolver(api)

    name1 = await resolver.resolve_group_name("42")
    assert name1 == "群1"
    assert call_count == 1

    # Manually expire
    for key in list(resolver._cache.keys()):
        resolver._cache[key] = (resolver._cache[key][0], time.time() - 1)

    name2 = await resolver.resolve_group_name("42")
    assert name2 == "群2"
    assert call_count == 2


async def test_resolve_group_name_concurrent_deduplicated():
    call_count = 0
    api = MagicMock()

    async def _ggi(group_id, no_cache=True):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return {"group_name": "Concurrent群"}

    api.get_group_info = _ggi
    resolver = NameResolver(api)

    results = await asyncio.gather(*[resolver.resolve_group_name("42") for _ in range(5)])
    assert all(r == "Concurrent群" for r in results)
    assert call_count == 1


async def test_eviction_skips_in_flight_locks():
    """_evict_if_needed should not evict a key whose lock is currently held,
    preserving the per-key dedup guarantee even at high cache churn."""
    from unittest.mock import AsyncMock

    from onebot_adapter.onebot.name_resolver import _CACHE_MAX

    api = MagicMock()

    async def _gmi(group_id, user_id, no_cache=False):
        return {"card": f"user{user_id}", "nickname": "n"}

    api.get_group_member_info = _gmi
    api.get_stranger_info = AsyncMock(return_value={"nickname": "s"})
    resolver = NameResolver(api)

    # Pre-populate the cache with one entry, then acquire its lock to simulate
    # an in-flight lookup. This entry is in _keys_order so _evict_if_needed
    # will check it.
    await resolver.resolve("0", "1")  # stores key "1:0" in cache
    locked_key = "1:0"
    lock = resolver._get_lock(locked_key)
    await lock.acquire()
    assert lock.locked()

    # Fill the cache past _CACHE_MAX to trigger eviction. The locked key
    # "1:0" should NOT be evicted (its lock is held). We use user_ids
    # starting from 1 to avoid touching "1:0".
    for i in range(1, _CACHE_MAX + 2):
        await resolver.resolve(str(i), "1")

    # The locked key should still be in _cache and _locks (skipped by eviction)
    assert locked_key in resolver._cache, "locked key was evicted despite held lock"
    assert locked_key in resolver._locks
    lock.release()


async def test_eviction_force_evicts_oldest_when_all_keys_locked():
    """When every key's lock is held, _evict_if_needed must still bound the
    cache size by force-evicting the oldest entry (rather than giving up and
    letting the cache grow unbounded).
    """
    from onebot_adapter.onebot.name_resolver import _CACHE_MAX

    api = MagicMock()

    async def _gmi(group_id, user_id, no_cache=False):
        return {"card": f"user{user_id}", "nickname": "n"}

    api.get_group_member_info = _gmi
    api.get_stranger_info = AsyncMock(return_value={"nickname": "s"})
    resolver = NameResolver(api)

    # Fill the cache exactly to _CACHE_MAX.
    for i in range(_CACHE_MAX):
        await resolver.resolve(str(i), "1")

    assert len(resolver._cache) == _CACHE_MAX

    # Acquire every lock so _evict_if_needed can't find an unlocked key.
    held_locks = []
    for key in list(resolver._keys_order):
        lock = resolver._get_lock(key)
        await lock.acquire()
        held_locks.append(lock)

    # Now call _store with a new key — this exceeds _CACHE_MAX and triggers
    # _evict_if_needed, which should force-evict the oldest locked entry.
    resolver._store("1:new", "newuser")

    # Cache must stay bounded (not exceed _CACHE_MAX + 1).
    assert len(resolver._cache) <= _CACHE_MAX, (
        f"cache grew to {len(resolver._cache)} > {_CACHE_MAX} when all keys locked"
    )

    # Release all locks.
    for lock in held_locks:
        lock.release()

"""Tests for FriendCache used by DM friends policy."""
from __future__ import annotations

from unittest.mock import AsyncMock

from onebot_adapter.onebot.friend_cache import FriendCache, friend_ids_from_payload


def test_friend_ids_from_list_and_wrapped_payloads():
    assert friend_ids_from_payload([{"user_id": 100}, {"user_id": "200"}]) == {"100", "200"}
    assert friend_ids_from_payload({"friends": [{"user_id": 1}]}) == {"1"}
    assert friend_ids_from_payload({"data": {"list": [3, "4"]}}) == {"3", "4"}
    assert friend_ids_from_payload({"user_id": 9}) == {"9"}
    assert friend_ids_from_payload(None) == set()


async def test_is_friend_loads_and_caches_list():
    api = AsyncMock()
    api.get_friend_list = AsyncMock(return_value=[{"user_id": 100}])
    cache = FriendCache(api, ttl=60)
    assert await cache.is_friend("100") is True
    assert await cache.is_friend("200") is False
    assert api.get_friend_list.await_count == 1
    assert await cache.is_friend("100") is True
    assert api.get_friend_list.await_count == 1


async def test_observe_friend_add_and_delete():
    api = AsyncMock()
    api.get_friend_list = AsyncMock(return_value=[{"user_id": 100}])
    cache = FriendCache(api, ttl=60)
    assert await cache.is_friend("200") is False
    cache.observe_event({"post_type": "notice", "notice_type": "friend_add", "user_id": 200})
    assert await cache.is_friend("200") is True
    cache.observe_event({"post_type": "notice", "notice_type": "friend_decrease", "user_id": "200"})
    assert await cache.is_friend("200") is False
    assert api.get_friend_list.await_count == 1


async def test_get_friend_list_failure_fail_closed_then_stale():
    api = AsyncMock()
    api.get_friend_list = AsyncMock(side_effect=RuntimeError("down"))
    cache = FriendCache(api, ttl=0)
    assert await cache.is_friend("100") is False

    api.get_friend_list = AsyncMock(return_value=[{"user_id": 100}])
    assert await cache.is_friend("100") is True
    api.get_friend_list = AsyncMock(side_effect=RuntimeError("down again"))
    assert await cache.is_friend("100") is True

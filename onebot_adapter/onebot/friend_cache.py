"""Cached OneBot friend list for DM ``friends`` policy lookups."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60.0


def friend_ids_from_payload(payload: Any) -> set[str]:
    """Extract QQ ids from ``get_friend_list`` payloads of various shapes."""
    if isinstance(payload, dict):
        nested = payload.get("friends")
        if nested is None:
            nested = payload.get("list")
        if nested is None:
            nested = payload.get("data")
        if nested is not None:
            return friend_ids_from_payload(nested)
        user_id = payload.get("user_id")
        return {str(user_id)} if user_id is not None and str(user_id) else set()
    if not isinstance(payload, list):
        return set()
    ids: set[str] = set()
    for item in payload:
        if isinstance(item, dict):
            user_id = item.get("user_id")
            if user_id is not None and str(user_id):
                ids.add(str(user_id))
        elif isinstance(item, (int, str)) and str(item):
            ids.add(str(item))
    return ids


class FriendCache:
    """TTL cache of friend QQ ids, refreshed via ``get_friend_list``.

    ``observe_event`` applies ``friend_add`` / ``friend_decrease`` immediately
    so a just-accepted friend is not blocked until the next refresh.
    Lookup failures fail closed (not a friend) when the cache has never loaded.
    """

    def __init__(self, api: Any, *, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self._api = api
        self._ttl = ttl
        self._ids: set[str] | None = None
        self._loaded_at = 0.0
        self._lock: asyncio.Lock | None = None

    def observe_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict) or event.get("post_type") != "notice":
            return
        if self._ids is None:
            return
        user_id = str(event.get("user_id", "") or "")
        if not user_id:
            return
        notice_type = event.get("notice_type")
        if notice_type == "friend_add":
            self._ids.add(user_id)
        elif notice_type in {"friend_decrease", "friend_delete"}:
            self._ids.discard(user_id)

    async def is_friend(self, user_id: str) -> bool:
        return str(user_id) in await self._get_ids()

    async def _get_ids(self) -> set[str]:
        now = time.monotonic()
        if self._ids is not None and now - self._loaded_at < self._ttl:
            return self._ids
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.monotonic()
            if self._ids is not None and now - self._loaded_at < self._ttl:
                return self._ids
            try:
                payload = await self._api.get_friend_list()
            except Exception:
                logger.warning("get_friend_list failed; friend check fail-closed if cache empty", exc_info=True)
                return self._ids if self._ids is not None else set()
            self._ids = friend_ids_from_payload(payload)
            self._loaded_at = time.monotonic()
            return self._ids

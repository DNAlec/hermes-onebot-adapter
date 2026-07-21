"""Persistent usage statistics for events accepted by the OneBot parser."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from onebot_adapter.relay.protocol import NormalizedEvent

logger = logging.getLogger(__name__)


class UsageStatsStore:
    """Small async facade over a single SQLite connection.

    SQLite work is serialized by a lock. Queries are aggregate-only and each
    write is a compact transaction, keeping event-loop pauses bounded.
    """

    def __init__(self, path: Path, retention_days: int = 365) -> None:
        self.path = path
        self.retention_days = retention_days
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._open()
        await self.prune()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at REAL NOT NULL,
                    chat_type TEXT NOT NULL CHECK(chat_type IN ('dm', 'group')),
                    group_id TEXT,
                    user_id TEXT NOT NULL,
                    is_system_notice INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_usage_events_time
                    ON usage_events(occurred_at);
                CREATE INDEX IF NOT EXISTS idx_usage_events_group_time
                    ON usage_events(group_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_usage_events_user_time
                    ON usage_events(user_id, occurred_at);
                CREATE TABLE IF NOT EXISTS usage_groups (
                    group_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                """
            )
            self._conn.commit()

    async def close(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        self._close()

    def _close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(24 * 60 * 60)
            try:
                await self.prune()
            except Exception:
                logger.exception("usage statistics retention cleanup failed")

    async def update_retention(self, days: int, *, prune_now: bool = False) -> None:
        self.retention_days = days
        if prune_now:
            await self.prune()

    async def record(self, event: NormalizedEvent) -> None:
        self._record(event, time.time())

    def _record(self, event: NormalizedEvent, occurred_at: float) -> None:
        conn = self._require_conn()
        group_id = event.chat_id.removeprefix("group:") if event.chat_type == "group" else None
        with self._lock, conn:
            conn.execute(
                "INSERT INTO usage_events "
                "(occurred_at, chat_type, group_id, user_id, is_system_notice) VALUES (?, ?, ?, ?, ?)",
                (occurred_at, event.chat_type, group_id, event.user_id, int(event.is_system_notice)),
            )
            conn.execute(
                """INSERT INTO usage_users(user_id, name, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     name=CASE WHEN excluded.name <> '' THEN excluded.name ELSE usage_users.name END,
                     updated_at=excluded.updated_at""",
                (event.user_id, event.user_name, occurred_at),
            )
            if group_id is not None:
                group_name = event.chat_name
                prefix = f"{group_id}("
                if group_name.startswith(prefix) and group_name.endswith(")"):
                    group_name = group_name[len(prefix):-1]
                elif group_name == group_id:
                    group_name = ""
                conn.execute(
                    """INSERT INTO usage_groups(group_id, name, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(group_id) DO UPDATE SET
                         name=CASE WHEN excluded.name <> '' THEN excluded.name ELSE usage_groups.name END,
                         updated_at=excluded.updated_at""",
                    (group_id, group_name, occurred_at),
                )

    async def prune(self) -> int:
        cutoff = time.time() - self.retention_days * 86400
        return self._prune(cutoff)

    def _prune(self, cutoff: float) -> int:
        conn = self._require_conn()
        with self._lock, conn:
            cursor = conn.execute("DELETE FROM usage_events WHERE occurred_at < ?", (cutoff,))
            conn.execute(
                "DELETE FROM usage_groups WHERE group_id NOT IN "
                "(SELECT DISTINCT group_id FROM usage_events WHERE group_id IS NOT NULL)"
            )
            conn.execute("DELETE FROM usage_users WHERE user_id NOT IN (SELECT DISTINCT user_id FROM usage_events)")
            return cursor.rowcount

    async def clear(self) -> int:
        return self._clear()

    def _clear(self) -> int:
        conn = self._require_conn()
        with self._lock, conn:
            count = int(conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0])
            conn.execute("DELETE FROM usage_events")
            conn.execute("DELETE FROM usage_groups")
            conn.execute("DELETE FROM usage_users")
            return count

    async def dimensions(self, start: float, end: float) -> dict[str, list[dict[str, Any]]]:
        return self._dimensions(start, end)

    def _dimensions(self, start: float, end: float) -> dict[str, list[dict[str, Any]]]:
        conn = self._require_conn()
        with self._lock:
            groups = conn.execute(
                """SELECT DISTINCT e.group_id AS id, COALESCE(g.name, '') AS name
                   FROM usage_events e LEFT JOIN usage_groups g ON g.group_id=e.group_id
                   WHERE e.occurred_at >= ? AND e.occurred_at < ? AND e.group_id IS NOT NULL
                   ORDER BY name, id""",
                (start, end),
            ).fetchall()
            users = conn.execute(
                """SELECT DISTINCT e.user_id AS id, COALESCE(u.name, '') AS name
                   FROM usage_events e LEFT JOIN usage_users u ON u.user_id=e.user_id
                   WHERE e.occurred_at >= ? AND e.occurred_at < ?
                   ORDER BY name, id""",
                (start, end),
            ).fetchall()
        return {
            "groups": [dict(row) for row in groups],
            "users": [dict(row) for row in users],
        }

    async def query(
        self,
        *,
        start: float,
        end: float,
        scope: str,
        group_id: str | None,
        user_id: str | None,
        bucket: str,
        tz_offset_minutes: int,
    ) -> dict[str, Any]:
        return self._query(
            start,
            end,
            scope,
            group_id,
            user_id,
            bucket,
            tz_offset_minutes,
        )

    def _query(
        self,
        start: float,
        end: float,
        scope: str,
        group_id: str | None,
        user_id: str | None,
        bucket: str,
        tz_offset_minutes: int,
    ) -> dict[str, Any]:
        conn = self._require_conn()
        where = ["e.occurred_at >= ?", "e.occurred_at < ?"]
        params: list[Any] = [start, end]
        if scope == "dm":
            where.append("e.chat_type = 'dm'")
        elif scope == "group":
            where.append("e.chat_type = 'group'")
        if group_id is not None:
            where.append("e.group_id = ?")
            params.append(group_id)
        if user_id is not None:
            where.append("e.user_id = ?")
            params.append(user_id)
        where_sql = " AND ".join(where)
        bucket_seconds = 3600 if bucket == "hour" else 86400
        offset_seconds = tz_offset_minutes * 60
        bucket_expr = (
            f"CAST((e.occurred_at + {offset_seconds}) / {bucket_seconds} AS INTEGER) "
            f"* {bucket_seconds} - {offset_seconds}"
        )
        with self._lock:
            summary = conn.execute(
                f"""SELECT COUNT(*) AS total,
                           COUNT(DISTINCT CASE WHEN e.chat_type='group' THEN e.group_id END) AS active_groups,
                           COUNT(DISTINCT e.user_id) AS active_users
                    FROM usage_events e WHERE {where_sql}""",
                params,
            ).fetchone()
            trend = conn.execute(
                f"""SELECT {bucket_expr} AS bucket_start, COUNT(*) AS count
                    FROM usage_events e WHERE {where_sql}
                    GROUP BY bucket_start ORDER BY bucket_start""",
                params,
            ).fetchall()
            groups = conn.execute(
                f"""SELECT e.group_id AS id, COALESCE(g.name, '') AS name, COUNT(*) AS count
                    FROM usage_events e LEFT JOIN usage_groups g ON g.group_id=e.group_id
                    WHERE {where_sql} AND e.chat_type='group'
                    GROUP BY e.group_id ORDER BY count DESC, e.group_id LIMIT 10""",
                params,
            ).fetchall()
            users = conn.execute(
                f"""SELECT e.user_id AS id, COALESCE(u.name, '') AS name, COUNT(*) AS count
                    FROM usage_events e LEFT JOIN usage_users u ON u.user_id=e.user_id
                    WHERE {where_sql}
                    GROUP BY e.user_id ORDER BY count DESC, e.user_id LIMIT 10""",
                params,
            ).fetchall()
        return {
            "summary": dict(summary),
            "trend": [dict(row) for row in trend],
            "top_groups": [dict(row) for row in groups],
            "top_users": [dict(row) for row in users],
        }

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("usage statistics store is not started")
        return self._conn

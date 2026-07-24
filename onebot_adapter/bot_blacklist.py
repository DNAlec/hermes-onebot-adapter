"""Persistent, bot-managed user blacklist independent of admission filters."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_SCOPES = frozenset({"group", "dm", "global"})


def format_duration(seconds: float) -> str:
    remaining = max(0, int(seconds + 0.999))
    if remaining < 60:
        return f"{remaining}秒"
    parts: list[str] = []
    for unit_seconds, label in ((86400, "天"), (3600, "小时"), (60, "分钟")):
        value, remaining = divmod(remaining, unit_seconds)
        if value:
            parts.append(f"{value}{label}")
        if len(parts) == 2:
            break
    return "".join(parts) or "不足1分钟"


@dataclass(frozen=True)
class BotBlacklistEntry:
    id: int
    scope: str
    group_id: str
    user_id: str
    created_at: float
    duration_seconds: int
    expires_at: float
    reason: str
    created_by_user_id: str

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else now
        remaining = max(0, int(self.expires_at - current + 0.999))
        return {
            "id": self.id,
            "scope": self.scope,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "duration_seconds": self.duration_seconds,
            "expires_at": self.expires_at,
            "remaining_seconds": remaining,
            "remaining": format_duration(remaining),
            "reason": self.reason,
            "created_by_user_id": self.created_by_user_id,
        }


class BotBlacklistStore:
    """Serialized SQLite store for active bot blacklist entries."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_blacklist_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL CHECK(scope IN ('group', 'dm', 'global')),
                    group_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    reason TEXT NOT NULL,
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    UNIQUE(scope, group_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_bot_blacklist_expiry
                    ON bot_blacklist_entries(expires_at);
                CREATE INDEX IF NOT EXISTS idx_bot_blacklist_user
                    ON bot_blacklist_entries(user_id);
                """
            )
            self._conn.commit()
        self.prune()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def set(self, *, scope: str, user_id: str, duration_seconds: int, reason: str,
            created_by_user_id: str, group_id: str = "", now: float | None = None) -> BotBlacklistEntry:
        scope, group_id, user_id = self._normalize_key(scope, group_id, user_id)
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if not reason.strip():
            raise ValueError("reason must not be empty")
        created_at = time.time() if now is None else now
        expires_at = created_at + duration_seconds
        conn = self._require_conn()
        with self._lock, conn:
            conn.execute(
                """INSERT INTO bot_blacklist_entries
                   (scope, group_id, user_id, created_at, duration_seconds, expires_at, reason, created_by_user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(scope, group_id, user_id) DO UPDATE SET
                     created_at=excluded.created_at, duration_seconds=excluded.duration_seconds,
                     expires_at=excluded.expires_at, reason=excluded.reason,
                     created_by_user_id=excluded.created_by_user_id""",
                (scope, group_id, user_id, created_at, duration_seconds, expires_at,
                 reason.strip(), str(created_by_user_id)),
            )
            row = conn.execute(
                "SELECT * FROM bot_blacklist_entries WHERE scope=? AND group_id=? AND user_id=?",
                (scope, group_id, user_id),
            ).fetchone()
        assert row is not None
        return self._entry(row)

    def remove(self, *, scope: str, user_id: str, group_id: str = "") -> bool:
        scope, group_id, user_id = self._normalize_key(scope, group_id, user_id)
        conn = self._require_conn()
        with self._lock, conn:
            cursor = conn.execute(
                "DELETE FROM bot_blacklist_entries WHERE scope=? AND group_id=? AND user_id=?",
                (scope, group_id, user_id),
            )
        return cursor.rowcount > 0

    def remove_id(self, entry_id: int) -> bool:
        conn = self._require_conn()
        with self._lock, conn:
            cursor = conn.execute("DELETE FROM bot_blacklist_entries WHERE id=?", (entry_id,))
        return cursor.rowcount > 0

    def list(self, *, scope: str | None = None, group_id: str | None = None,
             user_id: str | None = None, now: float | None = None) -> list[BotBlacklistEntry]:
        current = time.time() if now is None else now
        self.prune(current)
        clauses = ["expires_at > ?"]
        params: list[Any] = [current]
        if scope is not None:
            if scope not in VALID_SCOPES:
                raise ValueError(f"scope must be one of {sorted(VALID_SCOPES)}")
            clauses.append("scope = ?")
            params.append(scope)
        if group_id is not None:
            clauses.append("group_id = ?")
            params.append(str(group_id))
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(str(user_id))
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM bot_blacklist_entries WHERE " + " AND ".join(clauses)
                + " ORDER BY expires_at DESC, id DESC", params,
            ).fetchall()
        return [self._entry(row) for row in rows]

    def match(self, *, user_id: str, group_id: str | None, now: float | None = None) -> BotBlacklistEntry | None:
        current = time.time() if now is None else now
        self.prune(current)
        conn = self._require_conn()
        if group_id is None:
            sql = """SELECT * FROM bot_blacklist_entries
                     WHERE user_id=? AND expires_at>? AND scope IN ('dm', 'global')
                     ORDER BY expires_at DESC, id DESC LIMIT 1"""
            params = (str(user_id), current)
        else:
            sql = """SELECT * FROM bot_blacklist_entries
                     WHERE user_id=? AND expires_at>?
                       AND (scope='global' OR (scope='group' AND group_id=?))
                     ORDER BY expires_at DESC, id DESC LIMIT 1"""
            params = (str(user_id), current, str(group_id))
        with self._lock:
            row = conn.execute(sql, params).fetchone()
        return self._entry(row) if row is not None else None

    def clamp(self, max_duration_seconds: int, now: float | None = None) -> int:
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        current = time.time() if now is None else now
        maximum_expiry = current + max_duration_seconds
        conn = self._require_conn()
        with self._lock, conn:
            cursor = conn.execute(
                """UPDATE bot_blacklist_entries
                   SET expires_at=?, duration_seconds=MAX(1, CAST(? - created_at AS INTEGER))
                   WHERE expires_at > ?""",
                (maximum_expiry, maximum_expiry, maximum_expiry),
            )
        self.prune(current)
        return cursor.rowcount

    def prune(self, now: float | None = None) -> int:
        current = time.time() if now is None else now
        conn = self._require_conn()
        with self._lock, conn:
            cursor = conn.execute("DELETE FROM bot_blacklist_entries WHERE expires_at <= ?", (current,))
        return cursor.rowcount

    @staticmethod
    def _normalize_key(scope: str, group_id: str, user_id: str) -> tuple[str, str, str]:
        scope = str(scope)
        if scope not in VALID_SCOPES:
            raise ValueError(f"scope must be one of {sorted(VALID_SCOPES)}")
        uid = str(user_id).strip()
        if not uid:
            raise ValueError("user_id must not be empty")
        gid = str(group_id).strip()
        if scope == "group" and not gid:
            raise ValueError("group_id is required when scope=group")
        if scope != "group":
            gid = ""
        return scope, gid, uid

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("bot blacklist store is not started")
        return self._conn

    @staticmethod
    def _entry(row: sqlite3.Row) -> BotBlacklistEntry:
        return BotBlacklistEntry(
            id=int(row["id"]), scope=str(row["scope"]), group_id=str(row["group_id"]),
            user_id=str(row["user_id"]), created_at=float(row["created_at"]),
            duration_seconds=int(row["duration_seconds"]), expires_at=float(row["expires_at"]),
            reason=str(row["reason"]), created_by_user_id=str(row["created_by_user_id"]),
        )

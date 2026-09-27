"""SQLite через aiosqlite: настройки (kv), созданные события, лог запросов.

Для одного пользователя (ТЗ §3.3) SQLite достаточно; при переезде на
PostgreSQL достаточно переписать этот модуль — остальной код его не видит.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    calendar_id TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    start_iso   TEXT,
    end_iso     TEXT,
    rrule       TEXT,
    link        TEXT,
    via_ics     INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS reqlog (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,           -- text | voice | forward | ...
    outcome    TEXT NOT NULL,           -- created | cancelled | clarified | failed | no_event
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS reminders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_pk       INTEGER,             -- ссылка на events.id
    title          TEXT NOT NULL,
    start_iso      TEXT NOT NULL,
    remind_at      TEXT NOT NULL,       -- ISO (UTC), когда отправить
    minutes_before INTEGER NOT NULL,
    sent           INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database.connect() not called"
        return self._db

    # ---- kv / settings ----

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self.db.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def get_setting_json(self, key: str, default: Any = None) -> Any:
        raw = await self.get_setting(key)
        return json.loads(raw) if raw else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def set_setting_json(self, key: str, value: Any) -> None:
        await self.set_setting(key, json.dumps(value, ensure_ascii=False))

    # ---- events ----

    async def add_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        title: str,
        start_iso: str | None,
        end_iso: str | None,
        rrule: str | None = None,
        link: str | None = None,
        via_ics: bool = False,
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO events (calendar_id, event_id, title, start_iso, end_iso, "
            "rrule, link, via_ics) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (calendar_id, event_id, title, start_iso, end_iso, rrule, link, int(via_ics)),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_last_event(self) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM events WHERE deleted = 0 ORDER BY id DESC LIMIT 1"
        ) as cur:
            return await cur.fetchone()

    async def list_events(self, limit: int = 10) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM events WHERE deleted = 0 ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return list(await cur.fetchall())

    async def mark_deleted(self, event_id: int) -> None:
        await self.db.execute("UPDATE events SET deleted = 1 WHERE id = ?", (event_id,))
        await self.db.commit()

    # ---- reminders (уведомления в Telegram) ----

    async def add_reminder(self, *, event_pk: int | None, title: str, start_iso: str,
                           remind_at: str, minutes_before: int) -> None:
        await self.db.execute(
            "INSERT INTO reminders (event_pk, title, start_iso, remind_at, minutes_before) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_pk, title, start_iso, remind_at, minutes_before),
        )
        await self.db.commit()

    async def due_reminders(self, now_iso: str) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM reminders WHERE sent = 0 AND remind_at <= ? ORDER BY remind_at",
            (now_iso,),
        ) as cur:
            return list(await cur.fetchall())

    async def mark_reminder_sent(self, reminder_id: int) -> None:
        await self.db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
        await self.db.commit()

    async def cancel_reminders_for_event(self, event_pk: int) -> None:
        await self.db.execute(
            "DELETE FROM reminders WHERE event_pk = ? AND sent = 0", (event_pk,)
        )
        await self.db.commit()

    # ---- request log (ТЗ §3.3) ----

    async def log_request(self, source: str, outcome: str, detail: str = "") -> None:
        await self.db.execute(
            "INSERT INTO reqlog (source, outcome, detail) VALUES (?, ?, ?)",
            (source, outcome, detail[:500]),
        )
        await self.db.commit()

    # ---- расширенные запросы (v2: /today, перенос, отмена, /stats) ----

    async def all_events(self) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM events WHERE deleted = 0 ORDER BY start_iso"
        ) as cur:
            return list(await cur.fetchall())

    async def get_event_by_pk(self, pk: int) -> aiosqlite.Row | None:
        async with self.db.execute("SELECT * FROM events WHERE id = ?", (pk,)) as cur:
            return await cur.fetchone()

    async def update_event_times(self, pk: int, start_iso: str, end_iso: str | None) -> None:
        await self.db.execute(
            "UPDATE events SET start_iso = ?, end_iso = ? WHERE id = ?",
            (start_iso, end_iso, pk),
        )
        await self.db.commit()

    async def stats(self) -> dict:
        async with self.db.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN via_ics = 1 THEN 1 ELSE 0 END) AS ics, "
            "SUM(CASE WHEN deleted = 1 THEN 1 ELSE 0 END) AS del FROM events"
        ) as cur:
            ev = dict(await cur.fetchone())
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM reminders WHERE sent = 0"
        ) as cur:
            rem_pending = (await cur.fetchone())["n"]
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM reminders WHERE sent = 1"
        ) as cur:
            rem_sent = (await cur.fetchone())["n"]
        async with self.db.execute(
            "SELECT outcome, COUNT(*) AS n FROM reqlog GROUP BY outcome"
        ) as cur:
            reqlog = {r["outcome"]: r["n"] for r in await cur.fetchall()}
        return {
            "events": ev["n"] or 0,
            "events_ics": ev["ics"] or 0,
            "events_deleted": ev["del"] or 0,
            "reminders_pending": rem_pending,
            "reminders_sent": rem_sent,
            "reqlog": reqlog,
        }


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

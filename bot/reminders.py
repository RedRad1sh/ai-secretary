"""Напоминания в Telegram: планирование при создании события + фоновый прогон.

- Локально / на VDS: фоновая задача в main.py проверяет каждые 30 секунд.
- На serverless (YC Functions): прогон «попутно» при любом сообщении боту +
  по Timer-триггеру YC (см. docs/DEPLOY_YC.md, раздел «Напоминания»).
"""

from __future__ import annotations

import logging
from html import escape
from datetime import datetime, timedelta, timezone

from bot.config import Config
from bot.models import EventDraft, get_tz

log = logging.getLogger(__name__)


def reminder_minutes(settings_value: str) -> int | None:
    """'10' -> 10, 'off' -> None, 'popup10' (старое значение) -> 10."""
    v = (settings_value or "").strip().lower()
    if v in ("off", "выкл", "none", ""):
        return None
    if v.isdigit():
        return int(v)
    return 10


async def schedule_for_event(db, event_pk: int | None, draft: EventDraft,
                             minutes: int | None) -> bool:
    """Планирует напоминание(я) за N минут до начала. False — нечего планировать.

    Повторяющиеся события: напоминание на каждое из ближайших 8 вхождений.
    """
    if minutes is None or draft.start is None:
        return False

    starts: list[datetime] = [draft.start]
    if draft.recurrence:
        starts = draft.next_occurrences(8, after=datetime.now(tz=draft.start.tzinfo) + timedelta(minutes=minutes), inclusive=True)

    planned = False
    for occ in starts:
        remind_at = occ - timedelta(minutes=minutes)
        if remind_at <= datetime.now(tz=occ.tzinfo or timezone.utc):
            continue
        await db.add_reminder(
            event_pk=event_pk,
            title=draft.title,
            start_iso=occ.astimezone(timezone.utc).isoformat(),
            remind_at=remind_at.astimezone(timezone.utc).isoformat(),
            minutes_before=minutes,
        )
        planned = True
    if planned:
        log.info("Напоминаний запланировано: %d («%s» за %d мин)",
                 len(starts), draft.title, minutes)
    return planned


async def sweep(bot, db, cfg: Config, chat_id: int | None = None) -> int:
    """Отправляет все созревшие напоминания. Возвращает число отправленных."""
    now = datetime.now(timezone.utc)
    recipient = chat_id or cfg.allowed_user_id
    if recipient and cfg.is_paid(recipient):
        await replenish_recurring(db, now)
    rows = await db.due_reminders(now.isoformat(timespec="seconds"))
    if not rows:
        return 0

    tz_name = await db.get_setting("timezone", cfg.default_tz)
    tz = get_tz(tz_name)
    chat_id = chat_id or cfg.allowed_user_id
    sent = 0
    for r in rows:
        if chat_id is None:
            log.warning("ALLOWED_USER_ID не задан — некому слать напоминания")
            break
        try:
            start = datetime.fromisoformat(r["start_iso"])
            local_start = start.astimezone(tz)
            mins_left = max(0, round((start - now).total_seconds() / 60))
            text = (
                f"⏰ <b>Через {mins_left} мин</b> "
                f"({local_start.strftime('%H:%M')} {local_start.strftime('%d.%m')}) — "
                f"<b>{escape(r['title'])}</b>"
            )
            await bot.send_message(chat_id, text)
            await db.mark_reminder_sent(r["id"])
            sent += 1
        except Exception:  # noqa: BLE001 — не мешаем остальным напоминаниям
            log.exception("Не удалось отправить напоминание #%s", r["id"])

    return sent


async def sweep_all(bot, owner_db, cfg: Config) -> int:
    from bot.access import user_db_path
    from bot.db import Database
    sent = 0
    for user_id in sorted(cfg.all_user_ids):
        if user_id == cfg.allowed_user_id:
            sent += await sweep(bot, owner_db, cfg, user_id)
            continue
        path = user_db_path(cfg, user_id)
        if not path.exists():
            continue
        db = Database(path)
        await db.connect()
        try:
            sent += await sweep(bot, db, cfg, user_id)
        finally:
            await db.close()
    return sent


async def replenish_recurring(db, now: datetime) -> None:
    """Rolling horizon survives restarts; stored occurrences prevent duplicates."""
    from dateutil.rrule import rrulestr
    minutes = reminder_minutes(await db.get_setting("reminders", "10"))
    if minutes is None:
        return
    for row in await db.all_events():
        if not row["rrule"] or not row["start_iso"]:
            continue
        try:
            start = datetime.fromisoformat(row["start_iso"])
            rule = rrulestr(row["rrule"], dtstart=start)
            for occ in rule.xafter(now + timedelta(minutes=minutes), count=8, inc=True):
                await db.add_reminder(
                    event_pk=row["id"], title=row["title"],
                    start_iso=occ.astimezone(timezone.utc).isoformat(),
                    remind_at=(occ - timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat(),
                    minutes_before=minutes,
                )
        except (ValueError, TypeError, OverflowError):
            log.warning("Invalid recurrence for event %s", row["id"])

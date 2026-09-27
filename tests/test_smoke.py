"""Офлайн смоук-тесты: даты, RRULE, .ics, парсинг JSON-ответов AI.

Запуск: python tests/test_smoke.py   (сеть не нужна)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import EventDraft, get_tz, parse_ai_json  # noqa: E402
from bot.services.extractor import extract_event_local  # noqa: E402
from bot.services.ics import build_gcal_link, build_ics, safe_filename  # noqa: E402

TZ = "Europe/Moscow"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=get_tz(TZ))
OK, FAIL = "✅", "❌"
passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"{OK} {name}")
    else:
        failed += 1
        print(f"{FAIL} {name} {extra}")


# ---------- parse_ai_json ----------

check(
    "parse_ai_json: чистый JSON",
    parse_ai_json('{"is_event": true, "title": "Встреча"}') == {"is_event": True, "title": "Встреча"},
)
check(
    "parse_ai_json: с ```json-забором и болтовнёй",
    (parse_ai_json('Вот ответ:\n```json\n{"is_event": false}\n```\nЧто-то ещё') or {}).get("is_event") is False,
)
check(
    "parse_ai_json: битый JSON -> None",
    parse_ai_json("ни какого json здесь нет") is None,
)

# ---------- EventDraft.from_ai_json ----------

ai = {
    "is_event": True, "title": "Встреча с Иваном", "date": "2026-10-15", "time": "14:00",
    "end_time": None, "duration_minutes": 60, "all_day": False, "location": "офис на Ленина",
    "participants": "Иван", "description": "обсудить контракт", "recurrence": None,
    "confidence": 0.95, "missing": [],
}
d = EventDraft.from_ai_json(ai, tz=get_tz(TZ), now=NOW)
check("from_ai_json: старт 15.10 14:00", d and d.start.strftime("%d.%m %H:%M") == "15.10 14:00")
check("from_ai_json: конец = старт + 60 мин", d.end.strftime("%H:%M") == "15:00")
check("from_ai_json: preview содержит место", "офис на Ленина" in d.preview_text(TZ))

# «с 10 до 11:30»
d2 = EventDraft.from_ai_json(
    {**ai, "time": "10:00", "end_time": "11:30", "duration_minutes": None},
    tz=get_tz(TZ), now=NOW,
)
check("from_ai_json: end_time из «с 10 до 11:30»", d2.end.strftime("%H:%M") == "11:30")

# повторяющееся событие
d3 = EventDraft.from_ai_json(
    {**ai, "recurrence": {"freq": "WEEKLY", "byday": "MO"}},
    tz=get_tz(TZ), now=NOW,
)
check("rrule: FREQ=WEEKLY;BYDAY=MO", d3.rrule == "FREQ=WEEKLY;BYDAY=MO")
check("recurrence_human: «по понедельник»", "понедельни" in (d3.recurrence_human or ""))

# нет даты -> missing
d4 = EventDraft.from_ai_json(
    {"is_event": True, "title": "Позвонить маме", "date": None, "time": None},
    tz=get_tz(TZ), now=NOW,
)
check("from_ai_json: без даты -> needs_date", d4 is not None and d4.needs_date)
check("from_ai_json: «15 октября» в прошлом -> сдвиг на 2027", EventDraft.from_ai_json(
    {**ai, "date": "2026-10-15"}, tz=get_tz(TZ), now=datetime(2026, 11, 1, tzinfo=get_tz(TZ)),
).start.year == 2027)

# ---------- dateparser fallback (локальный парсер, ТЗ §8) ----------

cases = [
    ("Напомни завтра в 15:00 позвонить маме", "tomorrow_15"),
    ("созвон через час", "near_future"),
    ("встреча 15.10.2026 в 18:30", "absolute"),
]
for text, tag in cases:
    local = extract_event_local(text, TZ, NOW)
    check(f"dateparser: {tag} распознан", local is not None and local.start is not None,
          extra=f"-> {local}")

local = extract_event_local("Напомни завтра в 15:00 позвонить маме", TZ, NOW)
if local and local.start:
    check(
        "dateparser: завтра 15:00 == NOW+1д 15:00",
        local.start.strftime("%d.%m %H:%M") == (NOW + timedelta(days=1)).strftime("%d.%m") + " 15:00",
        extra=local.start.isoformat(),
    )

# регрессия: явная дата в тексте не должна перебиваться «сегодняшним» временем
LOCAL_NOW = datetime(2026, 9, 25, 23, 26, tzinfo=get_tz(TZ))
doc = extract_event_local(
    "встреча 29.09.2026 в 13:00 у терапевта. РЖД-медицина, г. Воронеж, Революции пр-кт, д.2",
    TZ, LOCAL_NOW,
)
check(
    "dateparser: явная дата 29.09.2026 13:00 (не сегодня!)",
    doc is not None and doc.start.strftime("%d.%m.%Y %H:%M") == "29.09.2026 13:00",
    extra=f"-> {doc.start if doc else None}",
)
check("dateparser: заголовок = первое предложение", doc is not None and "у терапевта" in doc.title,
      extra=f"-> {doc.title if doc else None}")

# ---------- ICS ----------

ics = build_ics(d, TZ).decode()
check("ics: BEGIN/END VCALENDAR", "BEGIN:VCALENDAR" in ics and "END:VCALENDAR" in ics)
check("ics: DTSTART с TZID", "DTSTART;TZID=Europe/Moscow:20261015T140000" in ics)
check("ics: VALARM за 10 минут", "TRIGGER:-PT10M" in ics)
check("ics: LOCATION экранирован", build_ics(
    EventDraft.from_ai_json({**ai, "location": "кафе Пушкин, центр"}, tz=get_tz(TZ), now=NOW),
    TZ,
).decode().count("кафе Пушкин\\, центр") == 1)
check("ics: safe_filename -> ASCII", safe_filename("Встреча/с: Иваном?").isascii())
check("ics: all-day", "DTSTART;VALUE=DATE:20261015" in build_ics(
    EventDraft.from_ai_json({**ai, "time": None, "all_day": True}, tz=get_tz(TZ), now=NOW), TZ,
).decode())
check("ics: VTIMEZONE для России", "BEGIN:VTIMEZONE" in ics and "TZID:Europe/Moscow" in ics
      and "TZOFFSETTO:+0300" in ics)
check("ics: все строки <= 75 октетов",
      all(len(l.encode()) <= 75 for l in build_ics(
          EventDraft.from_ai_json({**ai, "description": "д" * 300}, tz=get_tz(TZ), now=NOW),
          TZ,
      ).decode().split("\r\n")))
check("ics: VTIMEZONE с DST (Амстердам)", "BEGIN:DAYLIGHT" in build_ics(d, "Europe/Amsterdam").decode())
check("ics: пустое имя файла -> event", safe_filename("!!!") == "event")

# ---------- прямая ссылка Google Calendar ----------

link = build_gcal_link(d)
check("gcal-link: action=TEMPLATE", link is not None and "action=TEMPLATE" in link)
check("gcal-link: даты в UTC (14:00 MSK = 11:00Z)",
      "dates=20261015T110000Z%2F20261015T120000Z" in (link or "") or
      "dates=20261015T110000Z/20261015T120000Z" in (link or ""),
      extra=link)
check("gcal-link: location в ссылке", link and "location=" in link)

# ---------- get_tz fallback ----------

check("get_tz: мусор -> Europe/Moscow", str(get_tz("Nonsense/Zone")) == "Europe/Moscow")


# ---------- reminders (напоминания в Telegram) ----------

async def _reminder_flow() -> None:
    import tempfile

    from bot.config import Config
    from bot.db import Database
    from bot.reminders import reminder_minutes, schedule_for_event, sweep

    check(
        "remind: '10'->10, 'popup10'->10, 'off'->None",
        (reminder_minutes("10"), reminder_minutes("popup10"), reminder_minutes("off"))
        == (10, 10, None),
    )

    cfg = Config(allowed_user_id=1)
    db = Database(Path(tempfile.mkdtemp()) / "r.db")
    await db.connect()

    class FakeBot:
        sent: list = []

        async def send_message(self, chat_id, text, **kw):
            self.sent.append((chat_id, text))

    future = datetime.now(timezone.utc) + timedelta(hours=2)
    draft = EventDraft(title="Приём у терапевта", start=future)
    pk = await db.add_event(calendar_id="c", event_id="e", title=draft.title,
                            start_iso=future.isoformat(), end_iso=None)
    check("remind: запланировано за 10 мин", await schedule_for_event(db, pk, draft, 10))
    check("remind: в прошлом не планируется", not await schedule_for_event(
        db, pk, EventDraft(title="x", start=datetime.now(timezone.utc) - timedelta(minutes=5)), 10))
    check("remind: off не планируется", not await schedule_for_event(db, pk, draft, None))

    bot = FakeBot()
    check("remind: рано — тишина", await sweep(bot, db, cfg) == 0 and not bot.sent)
    await db.db.execute(
        "UPDATE reminders SET remind_at = ?",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
    )
    await db.db.commit()
    check("remind: созрело — отправлено 1", await sweep(bot, db, cfg) == 1 and len(bot.sent) == 1)
    check("remind: в тексте есть название", "терапевта" in bot.sent[0][1])
    check("remind: повторно не шлёт", await sweep(bot, db, cfg) == 0)
    await db.cancel_reminders_for_event(pk)
    await db.close()


import asyncio as _asyncio  # noqa: E402

_asyncio.run(_reminder_flow())

# ---------- v2: occurrences, интенты, перенос, БД ----------

from bot.handlers.manage import compose_move, parse_manage_intent  # noqa: E402

check("intent: «отмени приём у терапевта»", parse_manage_intent("Отмени приём у терапевта")
      == ("cancel", "приём у терапевта"))
check("intent: «перенеси встречу на 15:00»", parse_manage_intent("перенеси встречу на 15:00")
      == ("move", "встречу на 15:00"))
check("intent: обычный текст -> None", parse_manage_intent("встреча завтра в 15") is None)

old_s = datetime(2026, 9, 29, 13, 0, tzinfo=get_tz(TZ))
new_s, new_e = compose_move(old_s, old_s + timedelta(hours=1),
                            datetime(2026, 9, 25, 10, 0, tzinfo=get_tz(TZ)), "на 10:00")
check("compose_move: без даты — дата прежняя", new_s.strftime("%d.%m %H:%M") == "29.09 10:00"
      and new_e.strftime("%H:%M") == "11:00")
new_s2, _ = compose_move(old_s, None,
                         datetime(2026, 10, 1, 9, 0, tzinfo=get_tz(TZ)), "на 01.10 в 9:00")
check("compose_move: с явной датой", new_s2.strftime("%d.%m %H:%M") == "01.10 09:00")

rec = EventDraft(title="планёрка", start=datetime(2026, 9, 25, 10, 0, tzinfo=get_tz(TZ)),
                 recurrence={"freq": "WEEKLY", "byday": "MO"})
mons = rec.next_occurrences(3)
check("occurrences: понедельники x3",
      [d.strftime("%d.%m") for d in mons] == ["28.09", "05.10", "12.10"],
      extra=str([d.isoformat() for d in mons]))
daily = EventDraft(title="прививка", start=datetime(2026, 9, 25, 9, 0, tzinfo=get_tz(TZ)),
                   recurrence={"freq": "DAILY", "interval": 2}).next_occurrences(2)
check("occurrences: daily/2", [d.strftime("%d.%m") for d in daily] == ["27.09", "29.09"])


async def _v2_db_flow() -> None:
    import tempfile

    from bot.config import Config
    from bot.db import Database
    from bot.reminders import schedule_for_event

    cfg = Config(allowed_user_id=1)
    db = Database(Path(tempfile.mkdtemp()) / "v2.db")
    await db.connect()
    s = datetime(2026, 9, 28, 10, 0, tzinfo=get_tz(TZ))
    pk = await db.add_event(calendar_id="primary", event_id="g1", title="Планёрка команды",
                            start_iso=s.isoformat(),
                            end_iso=(s + timedelta(hours=1)).isoformat(),
                            rrule="FREQ=WEEKLY;BYDAY=MO")
    await db.update_event_times(pk, s.replace(hour=11).isoformat(),
                                s.replace(hour=12).isoformat())
    row = await db.get_event_by_pk(pk)
    check("v2: update_event_times", row["start_iso"][11:13] == "11")
    await db.mark_deleted(pk)
    check("v2: all_events скрывает удалённые", not await db.all_events())
    rec_draft = EventDraft(title="спортзал", start=s,
                           recurrence={"freq": "WEEKLY", "byday": "MO"})
    check("v2: напоминания на 8 повторов", await schedule_for_event(db, None, rec_draft, 30))
    st = await db.stats()
    check("v2: stats напоминания = 8", st["reminders_pending"] == 8, extra=str(st))
    await db.close()


_asyncio.run(_v2_db_flow())

print(f"\nИтог: {passed} ок, {failed} провалено")
sys.exit(1 if failed else 0)

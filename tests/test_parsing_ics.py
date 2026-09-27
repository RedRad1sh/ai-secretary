"""Регрессии парсинга, диапазонов, повторов и согласованности .ics (issue #4).

Фиксированное «сейчас»: 27.09.2026 19:00 Europe/Moscow (по чек-листу ROADMAP).
Покрыты все 12 пунктов чек-листа, дополнительно:
- monthly-RRULE на 31-е число (короткий месяц пропускается, а не сдвигается);
- диапазон через полночь;
- согласованность .ics: DTSTART/DTEND/TZID/RRULE, all-day, экранирование,
  фолдинг строк, имя файла, build_gcal_link (timed и all-day).

Офлайн: LLM не используется, только локальный парсер и модели.
Запуск: python tests/test_parsing_ics.py
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import EventDraft  # noqa: E402
from bot.services.extractor import extract_event_local, extract_events_local  # noqa: E402
from bot.services.ics import build_ics, build_gcal_link, safe_filename  # noqa: E402
from bot.handlers.manage import compose_move, parse_manage_intent  # noqa: E402

TZ = "Europe/Moscow"
NOW = datetime(2026, 9, 27, 19, 0, tzinfo=ZoneInfo(TZ))
passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ {name}")
    else:
        failed += 1
        print(f"❌ {name}  -> {extra}")


def dt(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ))


# ================= 12 пунктов чек-листа ROADMAP =================

d = extract_event_local("завтра в 10:00 встреча", TZ, NOW)
check("1. «завтра в 10:00 встреча» -> 28.09 10:00",
      d and d.start == dt(2026, 9, 28, 10, 0), str(d.start if d else None))

d = extract_event_local("послезавтра в 14:00 презентация", TZ, NOW)
check("2. «послезавтра в 14:00 презентация» -> 29.09 14:00",
      d and d.start == dt(2026, 9, 29, 14, 0), str(d.start if d else None))

d = extract_event_local("29.09.2026 в 13:00 врач", TZ, NOW)
check("3. «29.09.2026 в 13:00 врач» -> именно явная дата",
      d and d.start == dt(2026, 9, 29, 13, 0), str(d.start if d else None))

d = extract_event_local("завтра встреча с 10:00 до 11:30", TZ, NOW)
check("4. «с 10:00 до 11:30» -> один диапазон 90 мин",
      d and d.start == dt(2026, 9, 28, 10, 0) and d.end == dt(2026, 9, 28, 11, 30),
      f"{d.start} -> {d.end}" if d else "None")
ds = extract_events_local("завтра встреча с 10:00 до 11:30", TZ, NOW)
check("4б. диапазон не разбивается на два события", len(ds) == 1, f"n={len(ds)}")

d = extract_event_local("завтра смена с 23:00 до 01:00", TZ, NOW)
check("5. «с 23:00 до 01:00» -> окончание 29.09 01:00",
      d and d.start == dt(2026, 9, 28, 23, 0) and d.end == dt(2026, 9, 29, 1, 0),
      f"{d.start} -> {d.end}" if d else "None")

d = extract_event_local("завтра в 10:00 встреча на 90 минут", TZ, NOW)
check("6. «на 90 минут» -> до 11:30",
      d and d.start == dt(2026, 9, 28, 10, 0) and d.end == dt(2026, 9, 28, 11, 30),
      f"{d.start} -> {d.end}" if d else "None")

ds = extract_events_local("завтра в 10:00 планёрка, а в 15:00 врач", TZ, NOW)
check("7. «…10:00 планёрка, а в 15:00 врач» -> ровно две карточки 28.09",
      len(ds) == 2
      and ds[0].start == dt(2026, 9, 28, 10, 0)
      and ds[1].start == dt(2026, 9, 28, 15, 0),
      f"n={len(ds)} " + " ".join(str(x.start) for x in ds))

d = extract_event_local("каждый день в 09:00 зарядка", TZ, NOW)
check("8. «каждый день в 09:00» -> DAILY, первое вхождение 28.09 09:00",
      d and (d.recurrence or {}).get("freq") == "DAILY"
      and d.start == dt(2026, 9, 28, 9, 0),
      f"rec={d.recurrence if d else None} start={d.start if d else None}")

d = extract_event_local("по будням в 11:00 дейлик", TZ, NOW)
check("9. «по будням» -> WEEKLY MO,TU,WE,TH,FR",
      d and (d.recurrence or {}).get("freq") == "WEEKLY"
      and (d.recurrence or {}).get("byday") == "MO,TU,WE,TH,FR",
      str(d.recurrence if d else None))

d = extract_event_local("каждую пятницу в 18:30 тренировка", TZ, NOW)
check("10. «каждую пятницу в 18:30» -> WEEKLY/FR, 02.10 (ближайшая пятница)",
      d and (d.recurrence or {}).get("byday") == "FR"
      and d.start == dt(2026, 10, 2, 18, 30),
      f"rec={d.recurrence if d else None} start={d.start if d else None}")

# 11. «перенеси встречу на 15:00» -> дата и длительность прежние
old_s, old_e = dt(2026, 9, 28, 10, 0), dt(2026, 9, 28, 11, 0)
parsed = extract_event_local("встречу на 15:00", TZ, NOW)
new_s, new_e = compose_move(old_s, old_e, parsed.start, "встречу на 15:00")
check("11. «перенеси … на 15:00» -> дата 28.09 и 60 мин сохранены",
      new_s == dt(2026, 9, 28, 15, 0) and new_e == dt(2026, 9, 28, 16, 0),
      f"{new_s} -> {new_e}")

# 12. «отмени встречу» -> подтверждение перед удалением
# (сквозной сценарий с кнопкой — test_integration.py, сценарий 7;
#  здесь — интент: до подтверждения удаление не происходит)
check("12. интент «отмени встречу» распознан",
      parse_manage_intent("отмени встречу") == ("cancel", "встречу"),
      str(parse_manage_intent("отмени встречу")))


# ================= Повторы: 31-е число и RRULE =================

def _monthly_31() -> EventDraft:
    return EventDraft(title="Месячное", start=dt(2026, 1, 31, 10, 0),
                      recurrence={"freq": "MONTHLY"})

occ = _monthly_31().next_occurrences(3, after=dt(2026, 1, 31, 10, 0), inclusive=True)
check("31-е: вхождения 31.01 / 31.03 / 31.05 (февраль пропущен, без сдвига)",
      [o.strftime("%d.%m.%Y") for o in occ] == ["31.01.2026", "31.03.2026", "31.05.2026"],
      str([o.strftime("%d.%m.%Y") for o in occ]))

occ2 = _monthly_31().next_occurrences(1, after=dt(2026, 2, 1, 0, 0))
check("31-е: сразу после февраля -> 31.03, не 01.03 и не 28.02",
      occ2 and occ2[0] == dt(2026, 3, 31, 10, 0), str(occ2))

w = EventDraft(title="W", start=dt(2026, 9, 27, 12, 0),
               recurrence={"freq": "WEEKLY", "byday": "MO"})
occw = w.next_occurrences(2, after=NOW, inclusive=False)
check("WEEKLY/MO из вс 27.09 -> 28.09 и 05.10",
      [o.strftime("%d.%m") for o in occw] == ["28.09", "05.10"],
      str([o.strftime("%d.%m") for o in occw]))

i2 = EventDraft(title="I2", start=dt(2026, 9, 27, 12, 0),
                recurrence={"freq": "DAILY", "interval": 2})
occi = i2.next_occurrences(2, after=NOW, inclusive=False)
check("DAILY INTERVAL=2 -> 29.09 и 01.10",
      [o.strftime("%d.%m") for o in occi] == ["29.09", "01.10"],
      str([o.strftime("%d.%m") for o in occi]))


# ================= Согласованность .ics =================

d90 = extract_event_local("завтра встреча с 10:00 до 11:30", TZ, NOW)
ics = build_ics(d90, TZ).decode("utf-8")
check("ics: DTSTART/DTEND с TZID (90-мин диапазон)",
      "DTSTART;TZID=Europe/Moscow:20260928T100000" in ics
      and "DTEND;TZID=Europe/Moscow:20260928T113000" in ics, ics[:400])

drec = EventDraft(title="Повторка", start=dt(2026, 9, 28, 9, 0), end=dt(2026, 9, 28, 10, 0),
                  recurrence={"freq": "WEEKLY", "byday": "MO,TU,WE,TH,FR"})
icsr = build_ics(drec, TZ).decode("utf-8")
check("ics: RRULE-строка для повторов",
      "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR" in icsr, icsr[:400])

# Раунд-трип: DTSTART + RRULE из .ics воссоздают те же вхождения
m = re.search(r"DTSTART;TZID=\S+:(\d{8}T\d{6})", icsr)
from dateutil.rrule import rrulestr  # noqa: E402
parsed_rr = None
if m:
    ics_start = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
        tzinfo=ZoneInfo(TZ))
    for line in icsr.splitlines():
        if line.startswith("RRULE:"):
            parsed_rr = rrulestr(line.split(":", 1)[1], dtstart=ics_start)
occ_rt = list(parsed_rr.xafter(dt(2026, 9, 28, 9, 0), count=5, inc=True)) if parsed_rr else []
expected = drec.next_occurrences(5, after=dt(2026, 9, 28, 9, 0), inclusive=True)
check("ics: RRULE из файла даёт те же вхождения, что и draft",
      [o.date() for o in occ_rt] == [o.date() for o in expected],
      f"{[str(o) for o in occ_rt]} vs {[str(o) for o in expected]}")

dall = EventDraft(title="День", start=dt(2026, 10, 15, 0, 0), all_day=True)
icsa = build_ics(dall, TZ).decode("utf-8")
check("ics: all-day DTSTART;VALUE=DATE",
      "DTSTART;VALUE=DATE:20261015" in icsa, icsa[:400])

dsemi = EventDraft(title="Обед, с Иваном; второй блок", start=dt(2026, 10, 15, 13, 0))
icss = build_ics(dsemi, TZ).decode("utf-8")
check("ics: ; и , в SUMMARY экранированы",
      "SUMMARY:Обед\\, с Иваном\\; второй блок" in icss,
      [l for l in icss.splitlines() if l.startswith("SUMMARY")])

long_title = "Встреча по обсуждению квартального плана продаж и маркетинга"
icsl = build_ics(EventDraft(title=long_title, start=dt(2026, 10, 15, 13, 0)), TZ)
check("ics: все строки после фолдинга <= 75 октетов",
      all(len(l.encode("utf-8")) <= 75 for l in icsl.decode("utf-8").splitlines()),
      str(max((len(l.encode("utf-8")) for l in icsl.decode("utf-8").splitlines()), default=0)))
check("ics: фолдинг длинной строки (продолжение с пробела)",
      any(l.startswith(" ") for l in icsl.decode("utf-8").splitlines()))

check("filename: кириллица -> ASCII, <= 50 символов",
      safe_filename("Обед *с Иваном*/ (офис)").isascii()
      and len(safe_filename("В" * 80)) == 50,
      safe_filename("Обед *с Иваном*/ (офис)"))

# build_gcal_link: timed и all-day
link = build_gcal_link(d90)
check("gcal-link: timed — даты в UTC (10:00 MSK = 07:00Z)",
      link and "dates=20260928T070000Z%2F20260928T083000Z" in link, link or "None")
linka = build_gcal_link(dall)
check("gcal-link: all-day — dates=YYYYMMDD/YYYYMMDD",
      linka and "dates=20261015%2F20261015" in linka, linka or "None")
check("gcal-link: без даты -> None",
      build_gcal_link(EventDraft(title="X")) is None)

if failed:
    print(f"\nИтог: {passed} ок, {failed} провалено")
    raise SystemExit(1)
print(f"\nИтог: {passed} ок, 0 провалено")

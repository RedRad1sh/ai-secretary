"""Регрессии парсинга, диапазонов, повторов и согласованности .ics (issue #4).

Фиксированное «сейчас»: 27.09.2026 19:00 Europe/Moscow (по чек-листу ROADMAP).
Покрыты все 12 пунктов чек-листа, дополнительно:
- monthly-RRULE на 31-е число (короткий месяц пропускается, а не сдвигается);
- диапазон через полночь;
- согласованность .ics: DTSTART/DTEND/TZID/RRULE, all-day, экранирование,
  фолдинг строк, имя файла, build_gcal_link (timed и all-day);
- дефекты 29.09.2026: серия «каждую среду и пятницу» (DTSTART = ближайшее
  вхождение правила, а не «пятница») и события на несколько дней
  («с 5 по 10 октября» -> all-day с эксклюзивным DTEND), включая страховки,
  когда модель не вернула recurrence/end_date.

Офлайн: LLM не используется, только локальный парсер и модели.
Запуск: python tests/test_parsing_ics.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import EventDraft, RRULE_WEEKDAYS  # noqa: E402
from bot.services.extractor import (  # noqa: E402
    extract_event_local,
    extract_events,
    extract_events_local,
)
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
check("gcal-link: all-day — DTEND эксклюзивен (+1 день)",
      linka and "dates=20261015%2F20261016" in linka, linka or "None")
check("gcal-link: без даты -> None",
      build_gcal_link(EventDraft(title="X")) is None)


# ============ дефекты 29.09.2026: повторы и события на несколько дней ============
# 1) «каждую среду и пятницу» давала DTSTART=пятница: среда текущей недели
#    выпадала из серии, а часть календарей показывала только DTSTART.
drec2 = extract_event_local("Английский каждую среду и пятницу с 8:00 до 9:30", TZ, NOW)
check("deffekt-1: «каждую среду и пятницу с 8:00 до 9:30» -> BYDAY=WE,FR",
      drec2 is not None and (drec2.recurrence or {}).get("byday") == "WE,FR",
      f"rec={drec2.recurrence if drec2 else None}")
check("deffekt-1: первое вхождение — среда 30.09 08:00, а не пятница 02.10",
      drec2 is not None and drec2.start == dt(2026, 9, 30, 8, 0),
      str(drec2.start if drec2 else None))
check("deffekt-1: диапазон 8:00–9:30 сохранён",
      drec2 is not None and drec2.end == dt(2026, 9, 30, 9, 30),
      str(drec2.end if drec2 else None))
icsr2 = build_ics(drec2, TZ).decode("utf-8") if drec2 else ""
check("deffekt-1: .ics — DTSTART среда + RRULE WE,FR",
      "DTSTART;TZID=Europe/Moscow:20260930T080000" in icsr2
      and "RRULE:FREQ=WEEKLY;BYDAY=WE,FR" in icsr2, icsr2[:400])
_m = re.search(r"DTSTART;TZID=\S+:(\d{8}T\d{6})", icsr2)
_ics_start = (datetime.strptime(_m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(TZ))
              if _m else None)
_occ = ([o.strftime("%d.%m") for o in
         rrulestr("FREQ=WEEKLY;BYDAY=WE,FR", dtstart=_ics_start).xafter(
             dt(2026, 9, 30, 0, 0), count=4, inc=True)] if _ics_start else [])
check("deffekt-1: серия из .ics: ср 30.09, пт 02.10, ср 07.10, пт 09.10",
      _occ == ["30.09", "02.10", "07.10", "09.10"], str(_occ))
check("deffekt-1: DTSTART совпадает с BYDAY (RFC 5545)",
      drec2 is not None and RRULE_WEEKDAYS[drec2.start.weekday()] in
      (drec2.recurrence or {}).get("byday", ""), str(drec2.start if drec2 else None))

# 2) События на несколько дней: «с 5 по 10 октября» — раньше становилось
#    часовым событием 10.10.
dper = extract_event_local("отпуск с 5 по 10 октября", TZ, NOW)
check("deffekt-2: «отпуск с 5 по 10 октября» -> 05.10–10.10, весь день",
      dper is not None and dper.all_day
      and dper.start.date() == dt(2026, 10, 5).date()
      and dper.end.date() == dt(2026, 10, 10).date(),
      f"{dper.start if dper else None} -> {dper.end if dper else None}")
icsp = build_ics(dper, TZ).decode("utf-8") if dper else ""
check("deffekt-2: .ics — DTSTART 05.10, DTEND эксклюзивный 11.10",
      "DTSTART;VALUE=DATE:20261005" in icsp and "DTEND;VALUE=DATE:20261011" in icsp,
      icsp[:400])
check("deffekt-2: в предпросмотре видны дни и «весь день»",
      dper is not None and "05.10.2026–10.10.2026" in dper.preview_text(TZ)
      and "6 дней" in dper.preview_text(TZ),
      dper.preview_text(TZ) if dper else "None")
dper2 = extract_event_local("семинар 15-17 октября", TZ, NOW)
check("deffekt-2: «15-17 октября» -> 15.10–17.10",
      dper2 is not None and dper2.start.date() == dt(2026, 10, 15).date()
      and dper2.end.date() == dt(2026, 10, 17).date(),
      f"{dper2.start if dper2 else None} -> {dper2.end if dper2 else None}")
dper3 = extract_event_local("отпуск на 3 дня с 5 октября", TZ, NOW)
check("deffekt-2: «на 3 дня с 5 октября» -> 05.10–07.10",
      dper3 is not None and dper3.start.date() == dt(2026, 10, 5).date()
      and dper3.end.date() == dt(2026, 10, 7).date(),
      f"{dper3.start if dper3 else None} -> {dper3.end if dper3 else None}")
dper4 = extract_event_local("конференция с 5 по 10 октября с 10:00 до 18:00", TZ, NOW)
check("deffekt-2: многодневное с временем -> 05.10 10:00 – 10.10 18:00",
      dper4 is not None and not dper4.all_day
      and dper4.start == dt(2026, 10, 5, 10, 0) and dper4.end == dt(2026, 10, 10, 18, 0),
      f"{dper4.start if dper4 else None} -> {dper4.end if dper4 else None}")

# 2б) Повтор без «каждую»: дательный падеж («по средам и пятницам») и диапазон
#     времени без предлога «с» («8:00-9:30»).
dper5 = extract_event_local("английский по средам и пятницам 8:00-9:30", TZ, NOW)
check("deffekt-1: «по средам и пятницам 8:00-9:30» -> среда 30.09, 08:00–09:30",
      dper5 is not None and dper5.start == dt(2026, 9, 30, 8, 0)
      and dper5.end == dt(2026, 9, 30, 9, 30)
      and (dper5.recurrence or {}).get("byday") == "WE,FR",
      f"{dper5.start if dper5 else None} -> {dper5.end if dper5 else None}, "
      f"rec={dper5.recurrence if dper5 else None}")
dper6 = extract_event_local("занятия по вторникам и четвергам с 19:00 до 20:00", TZ, NOW)
check("deffekt-1: «по вторникам и четвергам» -> вторник 29.09, 19:00–20:00",
      dper6 is not None and dper6.start == dt(2026, 9, 29, 19, 0)
      and dper6.end == dt(2026, 9, 29, 20, 0)
      and (dper6.recurrence or {}).get("byday") == "TU,TH"
      and "BYDAY=TU,TH" in (dper6.rrule or ""),
      f"{dper6.start if dper6 else None} -> {dper6.end if dper6 else None}, "
      f"rrule={dper6.rrule if dper6 else None}")

# 3) AI-путь: end_date в JSON и выравнивание DTSTART повтора
dai = EventDraft.from_ai_json(
    {"is_event": True, "title": "Отпуск", "date": "2026-10-05", "end_date": "2026-10-10",
     "time": None, "end_time": None, "all_day": True, "recurrence": None,
     "confidence": 0.9, "missing": [], "assumptions": []},
    tz=ZoneInfo(TZ), now=NOW,
)
check("deffekt-2 (AI): end_date -> all-day 05.10–10.10",
      dai is not None and dai.all_day and dai.start.date() == dt(2026, 10, 5).date()
      and dai.end.date() == dt(2026, 10, 10).date(),
      f"{dai.start if dai else None} -> {dai.end if dai else None}")
dai2 = EventDraft.from_ai_json(
    {"is_event": True, "title": "Английский", "date": "2026-10-02", "time": "08:00",
     "end_time": "09:30", "all_day": False,
     "recurrence": {"freq": "WEEKLY", "byday": "WE,FR"},
     "confidence": 0.9, "missing": [], "assumptions": []},
    tz=ZoneInfo(TZ), now=NOW,
)
check("deffekt-1 (AI): DTSTART выровнен к среде 30.09",
      dai2 is not None and dai2.start == dt(2026, 9, 30, 8, 0)
      and dai2.end == dt(2026, 9, 30, 9, 30),
      f"{dai2.start if dai2 else None} -> {dai2.end if dai2 else None}")


# 4) Страховки AI-пути: модель «забыла» повтор или многодневность — достраиваем
#    по тексту (в боевом логе именно так терялись RRULE и второй день).
class _LazyLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def chat(self, system: str, user: str, **kw) -> str:
        return json.dumps(self.payload, ensure_ascii=False)


_lazy_rec = _LazyLLM({
    "is_event": True, "title": "Английский", "date": "2026-10-02", "time": "08:00",
    "end_time": "09:30", "all_day": False, "recurrence": None,
    "confidence": 0.9, "missing": [], "assumptions": [],
})
_ls = asyncio.run(extract_events(_lazy_rec, "Английский каждую среду и пятницу с 8:00 до 9:30",
                                 TZ, NOW))
check("deffekt-1 (страховка): модель без recurrence -> правило из текста, среда 30.09",
      len(_ls) == 1 and (_ls[0].recurrence or {}).get("byday") == "WE,FR"
      and _ls[0].start == dt(2026, 9, 30, 8, 0),
      f"rec={_ls[0].recurrence if _ls else None} start={_ls[0].start if _ls else None}")

_lazy_days = _LazyLLM({
    "is_event": True, "title": "Отпуск", "date": "2026-10-05", "time": None,
    "end_time": None, "all_day": False, "recurrence": None,
    "confidence": 0.9, "missing": [], "assumptions": [],
})
_ld = asyncio.run(extract_events(_lazy_days, "отпуск с 5 по 10 октября", TZ, NOW))
check("deffekt-2 (страховка): модель без end_date -> многодневное all-day 05.10–10.10",
      len(_ld) == 1 and _ld[0].all_day and _ld[0].start.date() == dt(2026, 10, 5).date()
      and _ld[0].end.date() == dt(2026, 10, 10).date(),
      f"{_ld[0].start if _ld else None} -> {_ld[0].end if _ld else None}")

_lazy_timed = _LazyLLM({
    "is_event": True, "title": "Конференция", "date": "2026-10-05", "time": "10:00",
    "end_time": "18:00", "all_day": False, "recurrence": None,
    "confidence": 0.9, "missing": [], "assumptions": [],
})
_lt = asyncio.run(extract_events(_lazy_timed, "конференция с 5 по 10 октября с 10:00 до 18:00",
                                 TZ, NOW))
check("deffekt-2 (страховка): время сохранено, конец 10.10 18:00",
      len(_lt) == 1 and not _lt[0].all_day and _lt[0].start == dt(2026, 10, 5, 10, 0)
      and _lt[0].end == dt(2026, 10, 10, 18, 0),
      f"{_lt[0].start if _lt else None} -> {_lt[0].end if _lt else None}")

if failed:
    print(f"\nИтог: {passed} ок, {failed} провалено")
    raise SystemExit(1)
print(f"\nИтог: {passed} ок, 0 провалено")

"""Fallback из ТЗ §8: генерация .ics-файла, если Google Calendar не настроен
или недоступен. Файл открывается в Google/Яндекс.Календаре, Outlook и т.д.

Совместимость с мобильным приложением Google Calendar:
- обязателен блок VTIMEZONE при использовании TZID (RFC 5545);
- строки длиннее 75 октетов «фолдятся» (переносятся с отступом);
- имя файла — ASCII (кириллица транслитерируется).

Дополнительно: build_gcal_link() — прямая ссылка «создать событие» в
Google Календаре (обходит .ics целиком).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from bot.models import EventDraft

_MAX_LINE_OCTETS = 75  # RFC 5545 §3.1


def _esc_ics(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Разбивает строку на части <= 75 октетов (продолжение — с пробела)."""
    out: list[str] = []
    cur, cur_len, first = "", 0, True
    for ch in line:
        w = len(ch.encode("utf-8"))
        limit = _MAX_LINE_OCTETS - 2 if first else _MAX_LINE_OCTETS - 3
        if cur and cur_len + w > limit:
            out.append(("" if first else " ") + cur)
            first, cur, cur_len = False, ch, w
        else:
            cur += ch
            cur_len += w
    out.append(("" if first else " ") + cur)
    return out


def _offset_str(tz: ZoneInfo, dt: datetime) -> str:
    off = int(dt.utcoffset().total_seconds())
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return f"{sign}{off // 3600:02d}{off % 3600 // 60:02d}"


def _vtimezone_lines(tz_name: str, when: datetime) -> list[str]:
    """VTIMEZONE для таймзоны события (фикс. пояс -> один STANDARD, с DST -> два)."""
    tz = ZoneInfo(tz_name)
    jan = datetime(when.year, 1, 15, 12, tzinfo=tz)
    jul = datetime(when.year, 7, 15, 12, tzinfo=tz)
    o1, o2 = _offset_str(tz, jan), _offset_str(tz, jul)
    name = tz_name.rsplit("/", 1)[-1]

    lines = ["BEGIN:VTIMEZONE", f"TZID:{tz_name}"]
    if o1 == o2:  # пояс без сезонного перевода часов (вся Россия)
        lines += [
            "BEGIN:STANDARD", f"TZOFFSETFROM:{o1}", f"TZOFFSETTO:{o1}",
            f"TZNAME:{name}", "DTSTART:19700101T000000", "END:STANDARD",
        ]
    else:  # европейская схема: январь — зимнее, июль — летнее
        std, dst = (o1, o2) if int(o1[-2:]) < int(o2[-2:]) else (o2, o1)
        lines += [
            "BEGIN:DAYLIGHT", f"TZOFFSETFROM:{std}", f"TZOFFSETTO:{dst}",
            f"TZNAME:{name}DST", "DTSTART:19700329T020000",
            "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
            "BEGIN:STANDARD", f"TZOFFSETFROM:{dst}", f"TZOFFSETTO:{std}",
            f"TZNAME:{name}", "DTSTART:19701025T030000",
            "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
        ]
    lines.append("END:VTIMEZONE")
    return lines


def _fmt_local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def build_ics(draft: EventDraft, tz_name: str = "Europe/Moscow") -> bytes:
    if draft.start is None:
        raise ValueError("Нельзя построить .ics без даты")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AI-Secretary//Telegram Bot//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        *_vtimezone_lines(tz_name, draft.start),
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@ai-secretary",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
    ]

    if draft.all_day:
        # draft.end — последний день события включительно; в RFC 5545/all-day
        # DTEND эксклюзивен, поэтому +1 день (иначе «отпуск с 5 по 10 октября»
        # импортируется как 5–9 октября, а однодневное событие — как нулевое).
        lines.append(f"DTSTART;VALUE=DATE:{draft.start.strftime('%Y%m%d')}")
        last_day = (draft.end or draft.start).date()
        lines.append(f"DTEND;VALUE=DATE:{(last_day + timedelta(days=1)).strftime('%Y%m%d')}")
    else:
        lines.append(f"DTSTART;TZID={tz_name}:{_fmt_local(draft.start)}")
        lines.append(f"DTEND;TZID={tz_name}:{_fmt_local(draft.end or draft.start)}")

    lines.append(f"SUMMARY:{_esc_ics(draft.title)}")
    if draft.location:
        lines.append(f"LOCATION:{_esc_ics(draft.location)}")
    desc_parts = []
    if draft.participants:
        desc_parts.append(f"Участники: {draft.participants}")
    if draft.description:
        desc_parts.append(draft.description)
    if desc_parts:
        lines.append(f"DESCRIPTION:{_esc_ics(chr(10).join(desc_parts))}")
    if draft.rrule:
        lines.append(f"RRULE:{draft.rrule}")

    lines += ["BEGIN:VALARM", "TRIGGER:-PT10M", "ACTION:DISPLAY",
              "DESCRIPTION:Напоминание", "END:VALARM",
              "END:VEVENT", "END:VCALENDAR"]

    folded = [piece for line in lines for piece in _fold(line)]
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")


def build_gcal_link(draft: EventDraft) -> str | None:
    """Прямая ссылка «создать событие» в Google Календаре (без .ics)."""
    if draft.start is None:
        return None
    params: dict[str, str] = {"text": draft.title, "action": "TEMPLATE"}
    if draft.all_day:
        s = draft.start.strftime("%Y%m%d")
        # Google принимает для all-day эксклюзивную дату окончания: +1 день
        e = ((draft.end or draft.start).date() + timedelta(days=1)).strftime("%Y%m%d")
        params["dates"] = f"{s}/{e}"
    else:
        end = draft.end or draft.start
        params["dates"] = "/".join(
            x.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            for x in (draft.start, end)
        )
    if draft.location:
        params["location"] = draft.location
    if draft.description:
        params["details"] = draft.description
    base = "https://calendar.google.com/calendar/render"
    return base + "?" + "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())


_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def safe_filename(title: str) -> str:
    """ASCII-безопасное имя файла (транслит + удаление опасных символов)."""
    translit = title.lower().translate(_TRANSLIT)
    keep = "".join(c if c.isalnum() or c in "-_ " else "_" for c in translit)
    keep = keep.strip(" -_")
    return (keep or "event")[:50]

"""EventDraft — нормализованный черновик события из AI-модуля.

Отвечает за валидацию и склейку полей AI-ответа в корректные даты
с учётом таймзоны (ТЗ §2.3, §2.4).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS_RU = {
    "MO": "понедельник", "TU": "вторник", "WE": "среда", "TH": "четверг",
    "FR": "пятница", "SA": "суббота", "SU": "воскресенье",
}

DEFAULT_TIME = time(10, 0)          # если время не указано — 10:00
DEFAULT_DURATION = timedelta(minutes=60)
MAX_FUTURE_DAYS = 365 * 3


WEEKDAYS_RU_FULL = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# порядковые коды дней недели для RRULE BYDAY (MO..SU), индекс = weekday()
RRULE_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def rrule_to_recurrence(rrule: str | None) -> dict | None:
    """Разбор RRULE обратно в recurrence-dict (обратная операция к EventDraft.rrule).

    'FREQ=WEEKLY;BYDAY=FR;INTERVAL=2' -> {"freq": "WEEKLY", "byday": "FR", "interval": 2}
    Нужен, чтобы при переносе повторяющегося события перечислить напоминания
    на вхождения, а не на одно (issue #6).
    """
    if not rrule:
        return None
    rec: dict = {}
    for part in rrule.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().upper()
        v = v.strip()
        if k == "FREQ":
            rec["freq"] = v
        elif k == "BYDAY":
            rec["byday"] = v
        elif k == "INTERVAL":
            try:
                rec["interval"] = int(v)
            except ValueError:
                pass
    return rec or None

@dataclass
class EventDraft:
    title: str
    start: datetime | None = None          # aware
    end: datetime | None = None            # aware
    all_day: bool = False
    location: str | None = None
    participants: str | None = None
    description: str | None = None
    recurrence: dict | None = None         # {"freq": "weekly", "byday": "MO"}
    confidence: float = 1.0
    missing: list[str] = field(default_factory=list)
    raw_text: str = ""
    extraction_note: str = ""

    # ---------- построение из ответа AI ----------

    @classmethod
    def from_ai_json(cls, data: dict, tz: ZoneInfo, now: datetime) -> "EventDraft | None":
        """Валидирует JSON от AI-модуля и собирает черновик (ТЗ §2.1 п.3)."""
        if not isinstance(data, dict) or not data.get("is_event"):
            return None

        title = str(data.get("title") or "Событие").strip()[:200]
        missing: list[str] = list(data.get("missing") or [])
        if not data.get("date"):
            if "date" not in missing:
                missing.append("date")

        start = _compose(data.get("date"), data.get("time"), tz, now)
        if start is None and "all_day" in data and data.get("all_day"):
            start = _compose(data.get("date"), None, tz, now)
            if start is not None:
                missing = [m for m in missing if m != "time"]

        end = None
        all_day = bool(data.get("all_day")) and data.get("time") is None
        if start is not None:
            if data.get("end_time"):
                end = _compose(data.get("date"), data.get("end_time"), tz, now)
                if end and end < start:
                    end += timedelta(days=1)
            if end is None and not all_day:
                dur = data.get("duration_minutes")
                duration = (
                    timedelta(minutes=int(float(dur))) if _is_num(dur) and float(dur) > 0 else DEFAULT_DURATION
                )
                end = start + duration

        rec = data.get("recurrence")
        recurrence = rec if isinstance(rec, dict) and rec.get("freq") else None

        conf = data.get("confidence")
        return cls(
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            location=_clean(data.get("location")),
            participants=_clean(data.get("participants")),
            description=_clean(data.get("description")),
            recurrence=recurrence,
            confidence=float(conf) if _is_num(conf) else 0.8,
            missing=missing,
            raw_text=str(data.get("raw_text") or ""),
        )

    # ---------- запросы к пользователю ----------

    @property
    def needs_date(self) -> bool:
        return self.start is None

    # ---------- повторяющиеся события: ближайшие даты ----------

    def next_occurrences(self, n: int = 8, *, after: datetime | None = None,
                         inclusive: bool = False) -> list[datetime]:
        """RFC RRULE semantics, shared with ICS (invalid month days are skipped)."""
        if self.start is None or not self.rrule:
            return []
        from dateutil.rrule import rrulestr
        try:
            rule = rrulestr(self.rrule, dtstart=self.start)
            return list(rule.xafter(after or self.start, count=n, inc=inclusive))
        except (ValueError, TypeError, OverflowError):
            return []

    # ---------- RRULE (повторяющиеся события, ТЗ §2.3) ----------

    @property
    def rrule(self) -> str | None:
        if not self.recurrence:
            return None
        freq = str(self.recurrence.get("freq", "weekly")).upper()
        parts = [f"FREQ={freq}"]
        byday = self.recurrence.get("byday")
        if byday:
            parts.append(f"BYDAY={byday}")
        interval = self.recurrence.get("interval")
        if _is_num(interval) and float(interval) > 1:
            parts.append(f"INTERVAL={int(float(interval))}")
        return ";".join(parts)

    @property
    def recurrence_human(self) -> str | None:
        if not self.recurrence:
            return None
        freq = str(self.recurrence.get("freq", "weekly")).lower()
        byday = self.recurrence.get("byday")
        names = " и ".join(
            WEEKDAYS_RU.get(d.strip().upper(), d) for d in str(byday).split(",")
        ) if byday else ""
        return {
            ("daily", ""): "каждый день",
            ("weekly", ""): "каждую неделю",
            ("monthly", ""): "каждый месяц",
        }.get((freq, names)) or (
            f"каждые {'недели' if freq == 'weekly' else 'дни' if freq == 'daily' else 'месяцы'}"
            + (f" по {names}" if names else "")
        )

    # ---------- представление для пользователя ----------

    def preview_text(self, tz_name: str) -> str:
        lines = [f"📌 <b>{_esc(self.title)}</b>"]
        if self.extraction_note:
            lines.append(f"⚠️ {_esc(self.extraction_note)}")
        if self.start:
            when = self.start.strftime("%d.%m.%Y")
            when += f" ({WEEKDAYS_RU_FULL[self.start.weekday()]})"
            if not self.all_day:
                when += ", " + self.start.strftime("%H:%M")
                if self.end:
                    when += f"–{self.end.strftime('%H:%M')}"
            else:
                when += ", весь день"
            lines.append(f"📅 {when} <i>({tz_name})</i>")
        else:
            lines.append("📅 <i>дата не определена</i>")
        if self.recurrence_human:
            lines.append(f"🔁 {_esc(self.recurrence_human)}")
        if self.location:
            lines.append(f"📍 {_esc(self.location)}")
        if self.participants:
            lines.append(f"👥 {_esc(self.participants)}")
        if self.description:
            lines.append(f"📝 {_esc(self.description)}")
        return "\n".join(lines)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _clean(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _compose(date_s: object, time_s: object, tz: ZoneInfo, now: datetime) -> datetime | None:
    """Склеивает 'YYYY-MM-DD' + 'HH:MM' в aware-datetime с проверками."""
    if not date_s or not isinstance(date_s, str):
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_s.strip())
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    hh, mm = 0, 0
    if time_s and isinstance(time_s, str):
        m2 = re.match(r"^(\d{1,2})[:.](\d{2})$", time_s.strip())
        if not m2:
            return None
        hh, mm = int(m2.group(1)), int(m2.group(2))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            return None
    try:
        dt = datetime(y, mo, d, hh, mm, tzinfo=tz)
    except ValueError:
        return None
    # «15 октября», сказанное позже этой даты, логичнее отнести к следующему году
    while dt < now - timedelta(days=1):
        try:
            dt = dt.replace(year=dt.year + 1)
        except ValueError:  # 29 февраля
            dt = dt.replace(year=dt.year + 1, day=28)
        if dt.year > now.year + 5:
            return None
    if dt < now - timedelta(days=30) or dt > now + timedelta(days=MAX_FUTURE_DAYS):
        return None
    return dt


def get_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return ZoneInfo("Europe/Moscow")


def parse_ai_json(text: str) -> dict | None:
    """Достаёт JSON из ответа модели (терпит ```json-заборы и лишний текст)."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        # последний шанс: починить хвостовые запятые
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", text[start : end + 1]))
        except json.JSONDecodeError:
            return None

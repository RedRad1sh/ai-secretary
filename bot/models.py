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

# дательный падеж множественного числа: «по средам и пятницам»
WEEKDAYS_RU_DATIVE = {
    "MO": "понедельникам", "TU": "вторникам", "WE": "средам", "TH": "четвергам",
    "FR": "пятницам", "SA": "субботам", "SU": "воскресеньям",
}

DEFAULT_TIME = time(10, 0)          # если время не указано — 10:00
DEFAULT_DURATION = timedelta(minutes=60)
MAX_FUTURE_DAYS = 365 * 3


WEEKDAYS_RU_FULL = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русская плюрализация: 1 событие / 2 события / 5 событий / 21 событие."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many

# порядковые коды дней недели для RRULE BYDAY (MO..SU), индекс = weekday()
RRULE_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def recurrence_rrule(recurrence: dict | None) -> str | None:
    """RRULE-строка из recurrence-dict (единая точка для .ics, GCal и напоминаний)."""
    if not recurrence:
        return None
    freq = str(recurrence.get("freq") or "WEEKLY").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        freq = "WEEKLY"
    parts = [f"FREQ={freq}"]
    codes = recurrence_byday(recurrence)
    if codes:
        parts.append("BYDAY=" + ",".join(codes))
    interval = recurrence.get("interval")
    if _is_num(interval) and float(interval) > 1:
        parts.append(f"INTERVAL={int(float(interval))}")
    return ";".join(parts)


def recurrence_byday(recurrence: dict | None) -> list[str]:
    """Список валидных кодов дней недели из recurrence (['WE', 'FR'])."""
    if not recurrence:
        return []
    raw = recurrence.get("byday")
    if not raw:
        return []
    return [
        c.strip().upper()
        for c in str(raw).replace(";", ",").split(",")
        if c.strip().upper() in RRULE_WEEKDAYS
    ]


def first_series_occurrence(start: datetime, recurrence: dict, now: datetime,
                            *, not_before: datetime | None = None) -> datetime:
    """Первое вхождение серии (DTSTART для RRULE), не раньше `now`.

    RFC 5545 требует, чтобы DTSTART совпадал с правилом повтора. Дефект:
    «Английский каждую среду и пятницу с 8:00 до 9:30» — парсер брал из текста
    «пятницу» как дату старта, и .ics получал DTSTART=пятница при BYDAY=WE,FR:
    среда текущей недели выпадала из серии, а часть календарей показывала только
    DTSTART (одно событие). Здесь старт сдвигается к ближайшему вхождению правила.

    not_before — явная дата начала серии из текста («каждую среду с 7 октября»).
    """
    if start is None or not recurrence:
        return start
    rrule = recurrence_rrule(recurrence)
    if not rrule:
        return start
    from dateutil.rrule import rrulestr  # импорт тяжёлый — лениво

    tz = start.tzinfo
    moment = now
    if not_before is not None:
        hint = not_before if not_before.tzinfo else not_before.replace(tzinfo=tz)
        moment = max(moment, hint) if moment.tzinfo else hint

    # Виртуальный dtstart на период раньше: dateutil отдаёт вхождения не раньше
    # dtstart, поэтому от исходной (возможно, поздней) даты ближайшая среда/вторник
    # не находились.
    freq = str(recurrence.get("freq") or "WEEKLY").upper()
    interval = recurrence.get("interval")
    step = int(float(interval)) if _is_num(interval) and float(interval) > 1 else 1
    if freq == "WEEKLY":
        virtual = start - timedelta(weeks=step)
    elif freq == "DAILY":
        virtual = start - timedelta(days=step)
    else:  # MONTHLY/YEARLY: фаза задаётся днём месяца/даты в dtstart
        virtual = start
    try:
        rule = rrulestr(rrule, dtstart=virtual)
        candidate = rule.after(moment - timedelta(seconds=1), inc=True)
    except (ValueError, TypeError, OverflowError):
        return start
    if candidate is None:
        return start
    try:
        candidate = candidate.astimezone(tz) if tz else candidate
    except (ValueError, OverflowError):
        return start
    codes = recurrence_byday(recurrence)
    if codes and RRULE_WEEKDAYS[candidate.weekday()] not in codes:
        return start  # страховка: правило и день недели разошлись — не ломаем дату
    return candidate


def align_series_start(draft: "EventDraft", now: datetime,
                       not_before: datetime | None = None) -> bool:
    """Сдвигает DTSTART повторяющегося события к первому вхождению правила.

    Длительность сохраняется: конец пересчитывается на ту же разницу.
    Возвращает True, если дата начала изменилась.
    """
    if draft.start is None or not draft.recurrence:
        return False
    new_start = first_series_occurrence(draft.start, draft.recurrence, now,
                                        not_before=not_before)
    if new_start == draft.start:
        return False
    shift = new_start - draft.start
    draft.start = new_start
    if draft.end is not None:
        draft.end = draft.end + shift
    return True


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
    # допущения, которые модель/код сделали ЗА пользователя (issue #13):
    # каждое маркируется в предпросмотре, тихих дефолтов нет
    assumptions: list[str] = field(default_factory=list)

    # ---------- построение из ответа AI ----------

    @classmethod
    def from_ai_json(cls, data: dict, tz: ZoneInfo, now: datetime,
                     not_before: datetime | None = None) -> "EventDraft | None":
        """Валидирует JSON от AI-модуля и собирает черновик (ТЗ §2.1 п.3).

        not_before — явная дата начала серии из текста («каждую среду с 7 октября»);
        нужна, чтобы DTSTART не «уезжал» к ближайшему вхождению раньше этой даты.
        """
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

        end_date_raw = data.get("end_date")
        has_end_date = bool(end_date_raw) and str(end_date_raw) != str(data.get("date") or "")
        has_times = data.get("time") is not None or data.get("end_time") is not None
        # «отпуск с 5 по 10 октября»: дат две, времени нет -> событие на весь день
        all_day = (bool(data.get("all_day")) or (has_end_date and not has_times)) and not has_times
        end = None
        assumptions = _assumptions_from(data.get("assumptions"))
        if start is not None:
            # «без тихих дефолтов» (issue #13): время не указано -> явный дефолт
            # с обязательной пометкой допущения
            time_defaulted = data.get("time") is None and not all_day
            if time_defaulted:
                start = start.replace(
                    hour=DEFAULT_TIME.hour, minute=DEFAULT_TIME.minute,
                    second=0, microsecond=0,
                )
            if has_end_date:
                # Конец многосуточного события: последний день (для all-day —
                # включительно; в .ics/Google он станет exclusive-датой +1 день).
                end = _compose(end_date_raw, data.get("end_time") or data.get("time"), tz, now)
                if end is not None and end < start:
                    start, end = end, start
            elif data.get("end_time"):
                end = _compose(data.get("date"), data.get("end_time"), tz, now)
                if end and end < start:
                    end += timedelta(days=1)
            duration_defaulted = False
            duration = None
            if end is None and not all_day:
                dur = data.get("duration_minutes")
                duration_defaulted = not (_is_num(dur) and float(dur) > 0)
                duration = (
                    timedelta(minutes=int(float(dur))) if not duration_defaulted else DEFAULT_DURATION
                )
                end = start + duration
            if time_defaulted:
                span = f"{start:%H:%M}–{end:%H:%M}" if end else f"{start:%H:%M}"
                assumptions.append(f"время не было указано — поставил дефолт {span}")
            if duration_defaulted and duration is not None:
                mins = int(duration.total_seconds() // 60)
                assumptions.append(
                    f"длительность не была указана — поставил дефолт "
                    f"{mins} {plural_ru(mins, 'минута', 'минуты', 'минут')}"
                )

        rec = data.get("recurrence")
        recurrence = rec if isinstance(rec, dict) and rec.get("freq") else None

        conf = data.get("confidence")
        draft = cls(
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
            assumptions=assumptions,
        )
        # DTSTART повторяющегося события обязан совпадать с RRULE (RFC 5545),
        # иначе среда/вторник текущей недели выпадают из серии (см. дефект #4).
        align_series_start(draft, now, not_before=not_before)
        return draft

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
        return recurrence_rrule(self.recurrence)

    @property
    def recurrence_human(self) -> str | None:
        if not self.recurrence:
            return None
        freq = str(self.recurrence.get("freq", "weekly")).lower()
        interval = self.recurrence.get("interval")
        step = int(float(interval)) if _is_num(interval) and float(interval) > 1 else 1
        base = {
            "daily": "каждый день" if step == 1
            else f"каждые {step} {plural_ru(step, 'день', 'дня', 'дней')}",
            "weekly": "каждую неделю" if step == 1
            else f"каждые {step} {plural_ru(step, 'неделю', 'недели', 'недель')}",
            "monthly": "каждый месяц" if step == 1
            else f"каждые {step} {plural_ru(step, 'месяц', 'месяца', 'месяцев')}",
        }.get(freq, "каждую неделю")
        codes = recurrence_byday(self.recurrence)
        if codes:
            # «каждую неделю по средам и пятницам» (дательный падеж)
            names = " и ".join(WEEKDAYS_RU_DATIVE.get(c, WEEKDAYS_RU.get(c, c)) for c in codes)
            return f"{base} по {names}"
        return base

    # ---------- представление для пользователя ----------

    def preview_text(self, tz_name: str) -> str:
        lines = [f"📌 <b>{_esc(self.title)}</b>"]
        if self.extraction_note:
            lines.append(f"⚠️ {_esc(self.extraction_note)}")
        if self.start:
            when = self.start.strftime("%d.%m.%Y")
            when += f" ({WEEKDAYS_RU_FULL[self.start.weekday()]})"
            if self.all_day:
                last = (self.end or self.start).date()
                if last > self.start.date():  # многодневное событие: «05.10–10.10»
                    days = (last - self.start.date()).days + 1
                    when = (
                        f"{self.start:%d.%m.%Y}–{last:%d.%m.%Y} "
                        f"({days} {plural_ru(days, 'день', 'дня', 'дней')}), весь день"
                    )
                else:
                    when += ", весь день"
            else:
                when += ", " + self.start.strftime("%H:%M")
                if self.end:
                    if self.end.date() != self.start.date():
                        # многодневное с временем: «10:00 – 10.10.2026 (сб) 18:00»
                        when += (
                            f" – {self.end:%d.%m.%Y} "
                            f"({WEEKDAYS_RU_FULL[self.end.weekday()]}) {self.end:%H:%M}"
                        )
                    else:
                        when += f"–{self.end.strftime('%H:%M')}"
            lines.append(f"📅 {when} <i>({tz_name})</i>")
        else:
            lines.append("📅 <i>дата не определена</i>")
        for a in self.assumptions:
            lines.append(f"⚠️ {_esc(a)}")
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


# ==================== предпросмотр серий событий (issue #13) ====================


def _series_key(d: EventDraft) -> tuple[str, str]:
    """Похожие события = одинаковые заголовок и описание («N одинаковых»)."""
    return (
        (d.title or "").strip().casefold(),
        (d.description or "").strip().casefold(),
    )


def _is_series_group(group: list[EventDraft]) -> bool:
    """Серия: ≥3 похожих событий с датами и без RRULE (иначе — обычные карточки)."""
    return (
        len(group) >= 3
        and all(d.start for d in group)
        and all(not d.recurrence for d in group)
    )


def _series_times(group: list[EventDraft]) -> str:
    labels: list[str] = []
    for d in group:
        if d.all_day or d.start is None:
            label = "весь день"
        elif d.end:
            label = f"{d.start:%H:%M}–{d.end:%H:%M}"
        else:
            label = f"{d.start:%H:%M}"
        if label not in labels:
            labels.append(label)
    return ", ".join(labels)


def series_preview_text(group: list[EventDraft], tz_name: str) -> str:
    """Компактный блок серии: «период | время | описание | N событий + флаги».

    Вместо N карточек — один блок; допущения (флаги) объединяются без повторов.
    """
    n = len(group)
    starts = sorted(d.start for d in group if d.start)
    first, last = starts[0], starts[-1]
    period = (
        first.strftime("%d.%m.%Y") if first.date() == last.date()
        else f"{first.strftime('%d.%m.%Y')}–{last.strftime('%d.%m.%Y')}"
    )
    desc = group[0].description or group[0].title
    flags: list[str] = []
    for d in group:
        for a in d.assumptions:
            if a not in flags:
                flags.append(a)
    lines = [f"<b>📦 {_esc(group[0].title)}</b>"]
    lines.append(
        f"📅 {period} | ⏰ {_series_times(group)} | 📝 {_esc(desc)} | "
        f"{n} {plural_ru(n, 'событие', 'события', 'событий')} <i>({tz_name})</i>"
    )
    lines += [f"⚠️ {_esc(a)}" for a in flags]
    return "\n".join(lines)


def multi_preview_text(drafts: list[EventDraft], tz_name: str) -> str:
    """Предпросмотр пакета: серии (≥3 похожих) — компактными блоками, остальное — карточками."""
    groups: dict[tuple[str, str], list[EventDraft]] = {}
    for d in drafts:
        groups.setdefault(_series_key(d), []).append(d)
    parts: list[str] = []
    idx = 1
    for group in groups.values():
        if _is_series_group(group):
            parts.append(series_preview_text(group, tz_name))
        else:
            for d in group:
                parts.append(f"<b>{idx}. {_esc(d.title)}</b>\n" + d.preview_text(tz_name))
                idx += 1
    return "\n\n---\n\n".join(parts)


def _clean(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _assumptions_from(value: object) -> list[str]:
    """Допущения из AI-JSON: список строк (или одна строка); не больше 10 штук."""
    if isinstance(value, str):
        items: list[object] = [value]
    elif isinstance(value, list):
        items = list(value)
    else:
        items = []
    out = []
    for v in items:
        s = str(v).strip()
        if s:
            out.append(s[:300])
    return out[:10]


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

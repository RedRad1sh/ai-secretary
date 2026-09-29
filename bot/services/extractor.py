"""AI-модуль извлечения событий (ТЗ §2.1 п.2–3, §2.3, §8).

Схема: GigaChat возвращает строгий JSON -> валидация в EventDraft ->
fallback на dateparser, если дата не распознана. Комбинация AI + dateparser
заявлена в ТЗ как mitigation риска «AI не распознаёт сложные конструкции».
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta

from bot.models import (
    EventDraft,
    align_series_start,
    first_series_occurrence,
    get_tz,
    parse_ai_json,
    recurrence_byday,
    RRULE_WEEKDAYS,
)
from bot.services.llm import LLMError

log = logging.getLogger(__name__)

PROMPT = """Ты — модуль извлечения событий для календаря. Из текста пользователя на русском языке \
извлеки информацию о встрече, деле или напоминании.

Сейчас: {now} ({tz_name}), {weekday}.

Верни СТРОГО JSON без пояснений и markdown. Ответ обязан начинаться с «{{» и заканчиваться «}}» — никакого текста до или после.

ЕСЛИ В ТЕКСТЕ ОДНО СОБЫТИЕ — верни один объект по схеме:
{{
  "is_event": true,
  "title": "str",
  "date": "YYYY-MM-DD" | null,
  "end_date": "YYYY-MM-DD" | null,
  "time": "HH:MM" | null,
  "end_time": "HH:MM" | null,
  "duration_minutes": int | null,
  "all_day": bool,
  "location": "str" | null,
  "participants": "str" | null,
  "description": "str" | null,
  "recurrence": {{"freq": "DAILY"|"WEEKLY"|"MONTHLY", "byday": "MO,TU,WE,TH,FR,SA,SU", "interval": int}} | null,
  "confidence": 0.0,
  "missing": ["date"|"time"|"title"],
  "assumptions": ["str"]
}}

ЕСЛИ В ТЕКСТЕ НЕСКОЛЬКО СОБЫТИЙ (например: «завтра в 11:00 дейлик, а в 10:00 подготовка») — верни:
{{
  "events": [
    {{"title": "str", "date": "YYYY-MM-DD", "end_date": null, "time": "HH:MM", "end_time": null, "duration_minutes": null, "all_day": false, "location": null, "participants": null, "description": null, "recurrence": null, "confidence": 0.9, "missing": [], "assumptions": []}},
    {{"title": "str", "date": "YYYY-MM-DD", "end_date": null, "time": "HH:MM", ...}}
  ]
}}

Правила:
- Если это не событие/дело/напоминание — верни {{"is_event": false}}.
- Для каждого события вычисли дату/время отдельно (завтра = {tomorrow}).
- Не выдумывай дату: если не можешь определить — null и добавь в missing.
- Событие на несколько дней («отпуск с 5 по 10 октября», «конференция 12–14 октября», «командировка с 1 по 3 ноября», «отель с 5.10 по 10.10», «на 3 дня с 5 октября»): date = первый день, end_date = последний день ВКЛЮЧИТЕЛЬНО. Если время не указано — all_day = true, time = null, end_time = null. Пример: «отпуск с 5 по 10 октября» -> date = "{year}-10-05", end_date = "{year}-10-10", all_day = true.
- Однодневное событие: end_date = null.
- Повтор («каждую среду и пятницу», «по будням»): date — ближайший подходящий день недели НЕ РАНЬШЕ сегодняшнего ({weekday}, {now}), время — указанное. Не переноси первый день серии на следующую неделю и не выбирай из списка дней только один последний.
- assumptions — список допущений, которые ты сделал ЗА пользователя (пустой, если всё взято из текста). См. «ПЛАНОВЫЕ ЗАПРОСЫ».

ПЛАНОВЫЕ ЗАПРОСЫ (серии похожих событий: «план/программа на неделю», «создай N событий», «распиши тренировки»):
- Количество и содержание событий бери СТРОГО из запроса пользователя: «12 событий» — ровно 12, «всю неделю» — все 7 дней. Не сокращай серию, не расширяй, не придумывай содержание программ (ты секретарь, а не тренер/диетолог).
- НЕ выдумывай время, длительность и раскладку (утро/вечер, какие дни): если пользователь не указал — верни time/end_time/duration_minutes = null и добавь "time" в missing; система подставит дефолт и пометит его сама.
- Если ты всё же подбираешь параметр за пользователя (время, длительность, дни недели, раскол утро/вечер, исключённые дни) — перечисли КАЖДОЕ такое допущение в поле assumptions у соответствующего события, например: «время не было указано — поставил дефолт 08:00–09:00», «раскладка утро/вечер — моё допущение», «взял 6 дней: Пн–Сб, воскресенье исключил по допущению». Никаких тихих дефолтов: секретарь говорит «я предположил», а не молчит.
- Пример одного: «встреча с Иваном завтра в 15:00 в кафе Пушкин» -> {{"is_event": true, "title": "Встреча с Иваном", "date": "{tomorrow}", "time": "15:00", "end_time": null, "duration_minutes": null, "all_day": false, "location": "кафе Пушкин", "participants": "Иван", "description": null, "recurrence": null, "confidence": 0.95, "missing": [], "assumptions": []}}"""


async def extract_events(
    llm,
    text: str,
    tz_name: str,
    now: datetime | None = None,
) -> list[EventDraft]:
    """Текст -> список EventDraft (пустой если не событие, 1+ если несколько)."""
    now = now or datetime.now(tz=get_tz(tz_name))
    if llm is None:
        local_list = extract_events_local(text, tz_name, now)
        return local_list or ([d] if (d := extract_event_local(text, tz_name, now)) else [])

    weekday = ("понедельник вторник среда четверг пятница суббота воскресенье").split()[now.weekday()]
    system = PROMPT.format(
        now=now.strftime("%Y-%m-%d %H:%M"),
        tz_name=tz_name,
        weekday=weekday,
        tomorrow=_iso_plus_days(now, 1),
        year=now.year,
    )
    try:
        answer = await llm.chat(system, text)
    except Exception:
        log.exception("LLM вызов упал (проверьте LLM_API_URL/LLM_MODEL/LLM_API_KEY) — пробуем локальный dateparser")
        local_list = extract_events_local(text, tz_name, now)
        if local_list:
            return local_list
        d = extract_event_local(text, tz_name, now)
        return [d] if d else []

    log.debug("AI ответ: %.500r", answer)
    data = parse_ai_json(answer)
    if data is None:
        log.warning("AI вернул не-JSON (см. выше «AI ответ») — пробуем локальный парсер")
        local_list = extract_events_local(text, tz_name, now)
        if local_list:
            return local_list
        d = extract_event_local(text, tz_name, now)
        return [d] if d else []

    # поддержка нового формата {"events": [...]}
    if isinstance(data, dict) and "events" in data and isinstance(data["events"], list):
        drafts: list[EventDraft] = []
        for item in data["events"]:
            if not isinstance(item, dict):
                continue
            # приводим к формату from_ai_json
            if "is_event" not in item:
                item = {"is_event": True, **item}
            d = EventDraft.from_ai_json(item, tz=get_tz(tz_name), now=now)
            if d is None:
                continue
            d.raw_text = text
            if not d.title or d.title.lower() in ("событие", "event"):
                d.title = (item.get("title") or text.strip().splitlines()[0])[:80]
            drafts.append(d)
        if drafts:
            log.info("AI извлёк %s событий: %s", len(drafts), ", ".join(f"«{x.title}»" for x in drafts))
            _apply_date_period(drafts, text, now, tz_name)
            _reconcile_recurrence(drafts, text, now, tz_name)
            _mark_assumed_time(drafts, text)
            return drafts
        # если events пустой — считаем не событием
        return []

    # старый формат одиночного объекта
    draft = EventDraft.from_ai_json(data, tz=get_tz(tz_name), now=now,
                                    not_before=_hint_for(tz_name, text, now))
    if draft is None:
        return []
    log.info("AI извлёк событие: «%s», уверенность %.2f", draft.title, draft.confidence)
    draft.raw_text = text
    if draft.needs_date:
        local = extract_event_local(text, tz_name, now)
        if local and local.start:
            draft.start, draft.end = local.start, local.end
            draft.missing = [m for m in draft.missing if m != "date"]
    if not draft.title or draft.title.lower() in ("событие", "event"):
        draft.title = (text.strip().splitlines() or ["Событие"])[0][:80]
    _apply_date_period([draft], text, now, tz_name)
    _reconcile_recurrence([draft], text, now, tz_name)
    _mark_assumed_time([draft], text)
    return [draft]


_CLOCK_RE = re.compile(r"\b(?P<prep>в|с)\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\b")


def _date_follows(text: str, m: re.Match) -> bool:
    """«с 7 октября», «с 5.10» — после числа идёт дата, а не время."""
    tail = text[m.end():]
    if re.match(r"\s*[./]\s*\d{1,2}", tail):
        return True
    return bool(re.match(rf"\s+{_MONTH_RE}", tail, re.I))


def _has_time_outside(text: str, span: tuple[int, int]) -> bool:
    """Есть ли в тексте время суток вне диапазона дат («с 5 по 10 октября» — нет)."""
    for m in _CLOCK_RE.finditer(text):
        if span[0] <= m.start() < span[1]:
            continue
        if m["prep"] == "с" and _date_follows(text, m):
            continue
        if _int_in(m["h"], 0, 23) is not None and _int_in(m["m"] or "0", 0, 59) is not None:
            return True
    for rx in (_TIME_RANGE, _BARE_TIME_RANGE):
        for m in rx.finditer(text):
            if m.end() <= span[0] or m.start() >= span[1]:
                return True
    return False


def _nearest_byday_candidate(recurrence: dict | None, text: str, now: datetime,
                             tz_name: str) -> datetime | None:
    """Ближайший подходящий день недели под правило (страховка офлайн-парсера)."""
    codes = recurrence_byday(recurrence)
    if not codes:
        return None
    clock = _pick_clock(text)
    hour, minute = (int(clock["h"]), int(clock["m"] or 0)) if clock else (0, 0)
    tz = get_tz(tz_name)
    for offset in range(0, 15):
        day = (now + timedelta(days=offset)).date()
        if RRULE_WEEKDAYS[day.weekday()] not in codes:
            continue
        candidate = datetime.combine(day, time(hour, minute), tzinfo=tz)
        if candidate >= now:
            return candidate
    return None


def _pick_clock(text: str) -> re.Match | None:
    """Время события: предпочитаем «в 15:00», пропускаем «с 7 октября»."""
    best, best_score = None, -1
    for m in _CLOCK_RE.finditer(text):
        if _int_in(m["h"], 0, 23) is None or _int_in(m["m"] or "0", 0, 59) is None:
            continue
        if m["prep"] == "с" and _date_follows(text, m):
            continue  # «с 7 октября» — дата, не время
        score = (2 if m["prep"] == "в" else 0) + (1 if m["m"] else 0)
        if score > best_score:
            best, best_score = m, score
    return best


def _hint_for(tz_name: str, text: str, now: datetime) -> datetime | None:
    """Явное начало серии из текста, с защитой от сбоев парсера шаблонов."""
    try:
        return series_start_hint(text, tz_name, now)
    except Exception:  # noqa: BLE001 — подсказка не должна ломать разбор
        log.debug("series_start_hint упал на тексте: %.120r", text)
        return None


def _apply_date_period(drafts: list[EventDraft], text: str, now: datetime,
                       tz_name: str) -> None:
    """Достраивает конец многодневного события, если модель увидела только первый
    день («отпуск с 5 по 10 октября» -> одно однодневное событие). Если во всём
    запросе нет времени, событие переводится в режим «весь день»."""
    if len(drafts) != 1:
        return
    draft = drafts[0]
    if draft.start is None:
        return
    if draft.end is not None and draft.end.date() > draft.start.date():
        return  # уже многодневное (end_date пришёл от модели)
    period = local_date_period(text, now)
    if period is None:
        return
    start_date, end_date, span = period
    if draft.start.date() not in (start_date, end_date):
        return  # модель назвала другую дату — не подменяем её
    tz = draft.start.tzinfo
    start = draft.start.replace(year=start_date.year, month=start_date.month, day=start_date.day)
    all_day = draft.all_day or not _has_time_outside(text, span)
    if all_day:
        draft.start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        draft.end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=tz)
        draft.all_day = True
        draft.missing = [m for m in draft.missing if m != "time"]
        # «время не указано» снимается: событие осознанно на весь день
        draft.assumptions = [
            a for a in draft.assumptions
            if "время не было указано" not in a and "длительность не была указана" not in a
        ]
    else:
        draft.start = start
        end_time = (draft.end or draft.start).timetz().replace(tzinfo=None)
        end = datetime.combine(end_date, end_time, tzinfo=tz)
        draft.end = end if end > start else start + timedelta(hours=1)


def _reconcile_recurrence(drafts: list[EventDraft], text: str, now: datetime,
                          tz_name: str) -> None:
    """Сверяет повтор из ответа AI с текстом («каждую среду и пятницу»).

    Если модель забыла про повтор, а в тексте он явный — включаем правило сами:
    иначе .ics уходит без RRULE, и в календаре остаётся одно событие.
    Если модель назвала не все дни недели — дополняем их из текста.
    """
    if len(drafts) != 1:
        return
    local = local_recurrence(text)
    if not local:
        return
    draft = drafts[0]
    if not draft.recurrence:
        draft.recurrence = dict(local)
    elif (
        str(draft.recurrence.get("freq", "")).upper() == "WEEKLY"
        and str(local.get("freq", "")).upper() == "WEEKLY"
    ):
        codes = set(recurrence_byday(draft.recurrence)) | set(recurrence_byday(local))
        if codes:
            draft.recurrence["byday"] = ",".join(c for c in RRULE_WEEKDAYS if c in codes)
    align_series_start(draft, now, not_before=_hint_for(tz_name, text, now))


async def extract_event(
    llm,
    text: str,
    tz_name: str,
    now: datetime | None = None,
) -> EventDraft | None:
    """Совместимость: текст -> один EventDraft (или None)."""
    drafts = await extract_events(llm, text, tz_name, now)
    return drafts[0] if drafts else None


def extract_event_local(text: str, tz_name: str, now: datetime | None = None) -> EventDraft | None:
    """Резервный офлайн-парсер (ТЗ §8: AI + dateparser для надёжности).

    Каскад стратегий для русского языка:
      1) dateparser по всей строке («завтра в 15:00», «через неделю в 10»);
      2) dateparser по кандидату-дате из regex («15.10.2026 в 18:30»);
      3) dateparser.search.search_dates — дата внутри предложения;
      4) rutimeparser — относительные конструкции («послезавтра», «в следующий
         вторник», «через час»), которые dateparser не понимает.
    Выбирается ближайшая будущая дата.
    """
    import dateparser  # импорт тяжёлый — лениво

    now = now or datetime.now(tz=get_tz(tz_name))

    # Событие на несколько дней («с 5 по 10 октября», «12–14 октября»): разбираем
    # диапазон дат явно, иначе dateparser делает из него часовое событие одного дня.
    period = local_date_period(text, now)
    if period:
        return _draft_from_period(text, *period, now=now, tz_name=tz_name)

    settings = {
        "RELATIVE_BASE": now.replace(tzinfo=None),
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": False,
    }

    candidates: list[datetime] = []
    if local_recurrence(text) and re.search(r"\bв\s+\d{1,2}(?::\d{2})?\b", text):
        candidates.append(now.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0))
    explicit_dates: set = set()

    time_range = _find_time_range(text)
    if time_range is not None and not _valid_time_range(time_range):
        return None
    parse_text = text
    if time_range:
        # приводим диапазон к «в HH:MM», чтобы датапарсер/часы увидели время начала
        parse_text = (
            text[:time_range.start()] + " в " + time_range["h1"] + ":"
            + (time_range["m1"] or "00") + text[time_range.end():]
        )

    # 1) Явные даты в тексте («29.09.2026», «15.10 в 18:30») — высший приоритет
    has_clock = bool(re.search(r"\b[вс]\s*\d{1,2}[:.]\d{2}\b", text.lower()))
    for m in re.finditer(
        r"\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?(?:\s*в\s*\d{1,2}[:.]\d{2})?", text
    ):
        naive = dateparser.parse(m.group(0), languages=["ru"], settings=settings)
        if naive:
            candidates.append(naive)
            explicit_dates.add(naive.date())
        # dateparser читает «dd.mm» как время (03:10), а «dd.mm в HH:MM» — как
        # None. Ручной разбор: dd.mm[.гггг] — дата, если год явный либо рядом
        # есть отдельное время («перенеси приём на 01.10 в 9:00»).
        date_part = m.group(0).split()[0]
        parts = re.split(r"[./]", date_part)
        if 2 <= len(parts) <= 3:
            try:
                day, month = int(parts[0]), int(parts[1])
                year = int(parts[2]) if len(parts) == 3 else now.year
                if 1 <= day <= 31 and 1 <= month <= 12 and (len(parts) == 3 or has_clock):
                    explicit = datetime(year, month, day)
                    # Неявный год: прошедшая дата — вероятно, следующий год
                    if len(parts) == 2 and explicit <= now.replace(tzinfo=None) - timedelta(days=1):
                        explicit = explicit.replace(year=year + 1)
                    candidates.append(explicit)
                    explicit_dates.add(explicit.date())
            except ValueError:
                pass

    naive = dateparser.parse(parse_text, languages=["ru"], settings=settings)
    if naive:
        candidates.append(naive)

    try:
        from dateparser.search import search_dates

        found = search_dates(parse_text, languages=["ru"], settings=settings) or []
        candidates += [dt for _, dt in found]
    except Exception:  # noqa: BLE001 — search_dates иногда падает на экзотике
        pass

    try:
        from rutimeparser import parse as ru_parse

        ru_dt = ru_parse(parse_text, now=now.replace(tzinfo=None))
        if ru_dt:
            candidates.append(ru_dt)
    except Exception:  # noqa: BLE001 — rutimeparser опционален
        pass

    # нормализуем кандидаты: rutimeparser может вернуть date, а не datetime
    normalized: list[datetime] = []
    for c in candidates:
        if isinstance(c, datetime):
            normalized.append(c)
        else:
            # date -> datetime at 00:00
            try:
                # assume date object has year/month/day
                normalized.append(datetime(c.year, c.month, c.day))
            except Exception:
                continue
    # фильтруем будущие (сравниваем наивные)
    base_naive = now.replace(tzinfo=None)
    future = []
    for c in normalized:
        # c наивный, base_naive наивный
        try:
            if c >= base_naive - timedelta(days=1):
                future.append(c.replace(tzinfo=get_tz(tz_name)))
        except TypeError:
            continue
    # Если в тексте есть явная дата — остаются только кандидаты с ней
    # (иначе «13:00» сегодня перебивает «29.09.2026 в 13:00»)
    if explicit_dates:
        filtered = [c for c in future if c.date() in explicit_dates]
        future = filtered or future
    if not future:
        # dateparser/rutimeparser не всегда понимают «каждую среду в 8:00»
        # (например, когда «сейчас» — воскресенье): считаем день недели сами.
        fallback = _nearest_byday_candidate(local_recurrence(text), parse_text, now, tz_name)
        if fallback is not None:
            future = [fallback]
    if not future:
        return None
    # При прочих равных предпочитаем кандидатов с временем (не полночь),
    # затем ближайший к «сейчас»
    start = min(future, key=lambda d: (not (d.hour or d.minute),
                                       abs((d - now).total_seconds())))

    # Explicit relative day outranks stray time-only candidates from search_dates.
    relative = re.search(r"\b(послезавтра|завтра|сегодня)\b", text.lower())
    if relative and not explicit_dates:
        day = now + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[relative[1]])
        start = start.replace(year=day.year, month=day.month, day=day.day)
    clock = _pick_clock(parse_text)
    if clock:
        start = start.replace(hour=int(clock["h"]), minute=int(clock["m"] or 0),
                              second=0, microsecond=0)
    has_time = bool(clock) or _has_explicit_time(text)
    recurrence = local_recurrence(text)
    if recurrence:
        # DTSTART серии = ближайшее вхождение правила (среда, а не пятница, для
        # «каждую среду и пятницу»); явное «начиная с <дата>» учитывается.
        start = first_series_occurrence(
            start, recurrence, now, not_before=_hint_for(tz_name, text, now)
        )
    end = start + timedelta(minutes=60) if has_time else None
    duration = re.search(r"\bна\s+(\d+)\s*(минут\w*|час\w*)", text.lower())
    if duration and has_time:
        end = start + timedelta(minutes=int(duration[1]) * (60 if duration[2].startswith("час") else 1))
    if time_range:
        start = start.replace(hour=int(time_range["h1"]), minute=int(time_range["m1"] or 0))
        end = start.replace(hour=int(time_range["h2"]), minute=int(time_range["m2"] or 0))
        if end <= start:
            end += timedelta(days=1)

    start = _fix_morning_evening(start, text)
    first_sentence = re.split(r"(?<!\d)[.!?](?!\d)|\n", text.strip())[0].strip()
    return EventDraft(
        title=(first_sentence or "Событие")[:80],
        start=start,
        end=end,
        recurrence=recurrence,
        extraction_note="Локальный разбор: проверьте даты, длительность и повтор. Сложные условия повторов могут быть не распознаны.",
        all_day=not has_time,
        confidence=0.5,
        missing=[] if has_time else ["time"],
        raw_text=text,
    )


def extract_events_local(text: str, tz_name: str, now: datetime | None = None) -> list:
    """Пытается вырезать несколько событий из одного сообщения офлайн."""
    # Split only explicit event boundaries, never a second clock in a range.
    parts = re.split(r"[\n;]+|,?\s+(?:а|и)\s+(?=в\s+\d)", text)
    drafts = []
    shared_day = re.search(r"\b(послезавтра|завтра|сегодня)\b", parts[0].lower())
    for chunk in parts:
        if not chunk.strip():
            continue
        if shared_day and not re.search(r"завтра|сегодня|\d{1,2}[./]\d{1,2}|понедель|вторник|сред|четверг|пятниц|суббот|воскрес", chunk.lower()):
            chunk = shared_day[1] + " " + chunk
        draft = extract_event_local(chunk, tz_name, now)
        if draft:
            drafts.append(draft)
    return drafts


_TIME_RANGE = re.compile(
    r"\bс\s+(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?:до|[-–—])\s*"
    r"(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\b",
    re.I,
)
# «8:00-9:30», «8:00 до 9:30» — без предлога «с» (частый формат расписаний)
_BARE_TIME_RANGE = re.compile(
    r"(?<![\d:])(?P<h1>\d{1,2}):(?P<m1>\d{2})\s*(?:до|[-–—])\s*"
    r"(?P<h2>\d{1,2}):(?P<m2>\d{2})(?![\d:])",
    re.I,
)


def _find_time_range(text: str) -> re.Match | None:
    """Диапазон времени в тексте: сначала «с 9:00 до 18:00», иначе «9:00-18:00»."""
    return _TIME_RANGE.search(text) or _BARE_TIME_RANGE.search(text)


def _valid_time_range(m: re.Match | None) -> bool:
    if m is None:
        return False
    return (
        _int_in(m["h1"], 0, 23) is not None
        and _int_in(m["h2"], 0, 23) is not None
        and _int_in(m["m1"] or "0", 0, 59) is not None
        and _int_in(m["m2"] or "0", 0, 59) is not None
    )


# ============== диапазоны дат («с 5 по 10 октября», «на 3 дня с 5.10») ==============
# Локальный парсер раньше не понимал события на несколько дней: «отпуск с 5 по
# 10 октября» превращался в часовое событие 10.10, «конференция 12 октября по
# 14 октября» — в событие 12.10 в 12:00. Здесь диапазон разбирается явно.

_MONTH_STEMS: tuple[tuple[str, int], ...] = (
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("ма", 5),
    ("июн", 6), ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10),
    ("ноябр", 11), ("декабр", 12),
)
# «ма[йяею]» — только само слово (май/мая/мае/маю), чтобы «маяк» не стал маем
_MONTH_RE = (
    r"(?:январ[а-яё]*|феврал[а-яё]*|март[а-яё]*|апрел[а-яё]*|ма[йяею]"
    r"|июн[а-яё]*|июл[а-яё]*|август[а-яё]*|сентябр[а-яё]*|октябр[а-яё]*"
    r"|ноябр[а-яё]*|декабр[а-яё]*)\b"
)

# «с 5 по 10 октября», «с 5.10 по 10.10», «12–14 октября», «с 30 сентября по 1 октября»
_DATE_RANGE_RE = re.compile(
    r"(?<![\d:а-яё])"
    r"(?:с\s+)?"
    r"(?P<d1>\d{1,2})(?:\s*[./]\s*(?P<m1>\d{1,2}))?(?:\s*[./]\s*(?P<y1>\d{2,4}))?"
    rf"(?:\s+(?P<mon1>{_MONTH_RE}))?"
    r"\s*(?:по|до|[-–—])\s*"
    r"(?P<d2>\d{1,2})(?:\s*[./]\s*(?P<m2>\d{1,2}))?(?:\s*[./]\s*(?P<y2>\d{2,4}))?"
    rf"(?:\s+(?P<mon2>{_MONTH_RE}))?",
    re.I,
)

# «на 3 дня с 5 октября», «на 2 суток с 5.10.2026»
_DAYS_SPAN_RE = re.compile(
    r"\bна\s+(?P<n>\d{1,3})\s*(?:дн\w*|сут\w*)\s+с\s+"
    r"(?P<d1>\d{1,2})(?:\s*[./]\s*(?P<m1>\d{1,2}))?(?:\s*[./]\s*(?P<y1>\d{2,4}))?"
    rf"(?:\s+(?P<mon1>{_MONTH_RE}))?",
    re.I,
)

# «каждую среду с 7 октября», «каждую пятницу начиная с 02.10» — явное начало серии
_SERIES_START_RE = re.compile(
    rf"(?:начин\w*|начн\w*)\s+с\s+(?P<a>\d{{1,2}}(?:\s*[./]\s*\d{{1,2}}){{1,2}}|\d{{1,2}}\s+{_MONTH_RE})"
    rf"|(?<![\d:а-яё])с\s+(?P<b>\d{{1,2}}[./]\d{{1,2}}[./]\d{{2,4}}|\d{{1,2}}\s+{_MONTH_RE})",
    re.I,
)


def _month_number(word: str | None) -> int | None:
    if not word:
        return None
    w = word.strip().lower()
    for stem, num in _MONTH_STEMS:
        if w.startswith(stem):
            return num
    return None


def _int_in(value: str | None, low: int, high: int) -> int | None:
    if value is None:
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    return num if low <= num <= high else None


def _year_of(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    if year < 100:
        year += 2000
    return year if 1900 <= year <= 2200 else None


def _explicit_date(day_s: str | None, month_s: str | None, year_s: str | None,
                   month_word: str | None, now: datetime) -> date | None:
    """День + (номер месяца | название месяца) -> date, прошедшее — на следующий год."""
    day = _int_in(day_s, 1, 31)
    month = _int_in(month_s, 1, 12) or _month_number(month_word)
    if day is None or month is None:
        return None
    year = _year_of(year_s) or now.year
    try:
        result = date(year, month, day)
    except ValueError:
        return None
    if not year_s and result < now.date() - timedelta(days=1):
        try:
            result = result.replace(year=result.year + 1)
        except ValueError:
            return None
    return result


def _range_dates(m: re.Match, now: datetime) -> tuple[date, date] | None:
    """Даты начала/конца из совпадения _DATE_RANGE_RE (без времени)."""
    g = m.groupdict()
    month1 = (
        _int_in(g.get("m1"), 1, 12)
        or _month_number(g.get("mon1"))
        or _month_number(g.get("mon2"))
        or _int_in(g.get("m2"), 1, 12)
    )
    month2 = (
        _int_in(g.get("m2"), 1, 12)
        or _month_number(g.get("mon2"))
        or _month_number(g.get("mon1"))
        or _int_in(g.get("m1"), 1, 12)
    )
    if month1 is None or month2 is None:
        return None  # ни месяца, ни дат с точками — это не диапазон дат
    day1, day2 = _int_in(g.get("d1"), 1, 31), _int_in(g.get("d2"), 1, 31)
    if day1 is None or day2 is None:
        return None
    year1 = _year_of(g.get("y1")) or _year_of(g.get("y2")) or now.year
    year2 = _year_of(g.get("y2")) or year1
    try:
        start, end = date(year1, month1, day1), date(year2, month2, day2)
    except ValueError:
        return None
    if end < start:  # «с 30 декабря по 3 января» — переход через год
        try:
            end = end.replace(year=end.year + 1)
        except ValueError:
            return None
    if end < now.date():  # прошедший период — следующий год (как в _compose)
        try:
            start = start.replace(year=start.year + 1)
            end = end.replace(year=end.year + 1)
        except ValueError:
            return None
    return start, end


def local_date_period(text: str, now: datetime) -> tuple[date, date, tuple[int, int]] | None:
    """Диапазон дат в тексте -> (первый день, последний день, span совпадения)."""
    m = _DAYS_SPAN_RE.search(text)
    if m:
        start = _explicit_date(m.group("d1"), m.group("m1"), m.group("y1"),
                               m.group("mon1"), now)
        days = _int_in(m.group("n"), 1, 60)
        if start is not None and days is not None:
            return start, start + timedelta(days=days - 1), m.span()
    m = _DATE_RANGE_RE.search(text)
    if m:
        pair = _range_dates(m, now)
        if pair:
            return pair[0], pair[1], m.span()
    return None


def series_start_hint(text: str, tz_name: str, now: datetime | None = None) -> datetime | None:
    """Явное начало серии из текста («каждую среду с 7 октября») или None."""
    now = now or datetime.now(tz=get_tz(tz_name))
    if not local_recurrence(text):
        return None
    m = _SERIES_START_RE.search(text)
    if not m:
        return None
    fragment = m.group("a") or m.group("b") or ""
    d = _parse_day_month_fragment(fragment, now)
    if d is None:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=get_tz(tz_name))


def _parse_day_month_fragment(fragment: str, now: datetime) -> date | None:
    f = fragment.strip()
    m = re.match(r"^(\d{1,2})\s*[./]\s*(\d{1,2})(?:\s*[./]\s*(\d{2,4}))?$", f)
    if m:
        return _explicit_date(m.group(1), m.group(2), m.group(3), None, now)
    m = re.match(rf"^(\d{{1,2}})\s+({_MONTH_RE})$", f, re.I)
    if m:
        return _explicit_date(m.group(1), None, None, m.group(2), now)
    return None


def _draft_from_period(text: str, start_date: date, end_date: date,
                       span: tuple[int, int], now: datetime, tz_name: str) -> EventDraft:
    """Черновик события на несколько дней (all-day, если время не указано)."""
    tz = get_tz(tz_name)
    time_range = None
    for rx in (_TIME_RANGE, _BARE_TIME_RANGE):
        for cand in rx.finditer(text):
            if cand.end() <= span[0] or cand.start() >= span[1]:
                time_range = cand
                break
        if time_range is not None:
            break
    valid = _valid_time_range(time_range)
    assumptions: list[str] = []
    if valid:
        start = datetime.combine(
            start_date, time(int(time_range["h1"]), int(time_range["m1"] or 0)), tzinfo=tz
        )
        end = datetime.combine(
            end_date, time(int(time_range["h2"]), int(time_range["m2"] or 0)), tzinfo=tz
        )
        all_day = False
    else:
        clock = None
        for cand in re.finditer(r"\b(?:в|к)\s+(\d{1,2})(?::(\d{2}))?\b", text):
            if cand.end() <= span[0] or cand.start() >= span[1]:
                if _int_in(cand[1], 0, 23) is not None and _int_in(cand[2] or "0", 0, 59) is not None:
                    clock = cand
                    break
        if clock is not None:
            start_time = time(int(clock[1]), int(clock[2] or 0))
            start = datetime.combine(start_date, start_time, tzinfo=tz)
            end = datetime.combine(end_date, start_time, tzinfo=tz) + timedelta(minutes=60)
            all_day = False
            assumptions.append(
                "время окончания не указано — поставил дефолт 60 минут после начала последнего дня"
            )
        else:
            start = datetime.combine(start_date, time(0, 0), tzinfo=tz)
            end = datetime.combine(end_date, time(0, 0), tzinfo=tz)
            all_day = True
    days = (end_date - start_date).days + 1
    first_sentence = re.split(r"(?<!\d)[.!?](?!\d)|\n", text.strip())[0].strip()
    return EventDraft(
        title=(first_sentence or "Событие")[:80],
        start=start,
        end=end,
        all_day=all_day,
        extraction_note=(
            "Локальный разбор: событие на "
            f"{days} {_plural_days(days)}; проверьте даты и время."
        ),
        confidence=0.5,
        missing=[],
        assumptions=assumptions,
        raw_text=text,
    )


def _plural_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


def local_recurrence(text: str) -> dict | None:
    text = text.lower()
    if re.search(r"каждый день|ежедневно", text):
        return {"freq": "DAILY"}
    if "по будням" in text:
        return {"freq": "WEEKLY", "byday": "MO,TU,WE,TH,FR"}
    if re.search(r"кажд\w*|еженедельно|\bпо\b", text):
        days = [("понедель", "MO"), ("вторник", "TU"), ("сред", "WE"),
                ("четверг", "TH"), ("пятниц", "FR"), ("суббот", "SA"), ("воскрес", "SU")]
        codes = [code for name, code in days if name in text]
        if codes:
            return {"freq": "WEEKLY", "byday": ",".join(codes)}
        if "каждую неделю" in text or "еженедельно" in text:
            return {"freq": "WEEKLY"}
        if "каждый месяц" in text:
            return {"freq": "MONTHLY"}
    return None


def _count_times(text: str) -> int:
    return len(re.findall(r"\b\d{1,2}[:.]\d{2}\b", text))


_TIME_WORDS = {"утра": 0, "дня": 12, "вечера": 12, "ночи": 0}


def _has_explicit_time(text: str) -> bool:
    return bool(
        re.search(r"\b\d{1,2}[:.]\d{2}\b", text)
        or re.search(r"\b\d{1,2}\s*(?:утра|дня|вечера|ночи|час(?:а|ов)?)\b", text)
    )


_TIME_MENTION = re.compile(
    r"\b\d{1,2}[:.]\d{2}\b"                              # 15:00, 9.30
    r"|\b\d{1,2}\s*(?:утра|дня|вечера|ночи|час(?:а|ов)?)\b"  # «в 3 дня», «5 часов»
    r"|\b(?:в|с|до|к)\s+\d{1,2}(?::\d{2})?\b",           # «в 15», «с 9 до 10»
    re.I,
)


def _user_specified_time(text: str) -> bool:
    """Есть ли во всём запросе хоть одно упоминание конкретного времени."""
    return bool(_TIME_MENTION.search(text or ""))


def _mark_assumed_time(drafts, text: str) -> None:
    """«Без тихих дефолтов» (issue #13): если во всём запросе не было ни одного
    времени — любое время в извлечённых событиях это допущение (модели или
    дефолт системы). Помечаем его явно, если модель не пометила сама."""
    if _user_specified_time(text):
        return
    for d in drafts:
        if d.start is None or d.all_day:
            continue
        if any("время не было указано" in a for a in d.assumptions):
            continue  # уже помечено (кодом или моделью)
        span = f"{d.start:%H:%M}–{d.end:%H:%M}" if d.end else f"{d.start:%H:%M}"
        d.assumptions.append(f"время не было указано — поставил дефолт {span}")


def _fix_morning_evening(start: datetime, text: str) -> datetime:
    """«в 9 утра» -> 09:00, «в 3 дня» -> 15:00, если парсер оставил полночь."""
    if start.hour or start.minute:
        return start
    m = re.search(r"\b(\d{1,2})\s*(утра|дня|вечера|ночи)\b", text)
    if not m:
        return start
    h = int(m.group(1)) % 12 + _TIME_WORDS[m.group(2)]
    return start.replace(hour=h)


def _iso_plus_days(now: datetime, days: int) -> str:
    return (now + timedelta(days=days)).strftime("%Y-%m-%d")

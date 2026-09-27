"""AI-модуль извлечения событий (ТЗ §2.1 п.2–3, §2.3, §8).

Схема: GigaChat возвращает строгий JSON -> валидация в EventDraft ->
fallback на dateparser, если дата не распознана. Комбинация AI + dateparser
заявлена в ТЗ как mitigation риска «AI не распознаёт сложные конструкции».
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from bot.models import EventDraft, get_tz, parse_ai_json
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
  "time": "HH:MM" | null,
  "end_time": "HH:MM" | null,
  "duration_minutes": int | null,
  "all_day": bool,
  "location": "str" | null,
  "participants": "str" | null,
  "description": "str" | null,
  "recurrence": {{"freq": "DAILY"|"WEEKLY"|"MONTHLY", "byday": "MO,TU,WE,TH,FR,SA,SU", "interval": int}} | null,
  "confidence": 0.0,
  "missing": ["date"|"time"|"title"]
}}

ЕСЛИ В ТЕКСТЕ НЕСКОЛЬКО СОБЫТИЙ (например: «завтра в 11:00 дейлик, а в 10:00 подготовка») — верни:
{{
  "events": [
    {{"title": "str", "date": "YYYY-MM-DD", "time": "HH:MM", "end_time": null, "duration_minutes": null, "all_day": false, "location": null, "participants": null, "description": null, "recurrence": null, "confidence": 0.9, "missing": []}},
    {{"title": "str", "date": "YYYY-MM-DD", "time": "HH:MM", ...}}
  ]
}}

Правила:
- Если это не событие/дело/напоминание — верни {{"is_event": false}}.
- Для каждого события вычисли дату/время отдельно (завтра = {tomorrow}).
- Не выдумывай дату: если не можешь определить — null и добавь в missing.
- Пример одного: «встреча с Иваном завтра в 15:00 в кафе Пушкин» -> {{"is_event": true, "title": "Встреча с Иваном", "date": "{tomorrow}", "time": "15:00", "end_time": null, "duration_minutes": null, "all_day": false, "location": "кафе Пушкин", "participants": "Иван", "description": null, "recurrence": null, "confidence": 0.95, "missing": []}}"""


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

    log.info("AI ответ: %.500r", answer)
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
            if d.needs_date:
                # пробуем уточнить дату локально по куску
                local = extract_event_local(item.get("title","") + " " + text, tz_name, now)
                if local and local.start:
                    d.start, d.end = local.start, local.end
                    d.missing = [m for m in d.missing if m != "date"]
            if not d.title or d.title.lower() in ("событие", "event"):
                d.title = (item.get("title") or text.strip().splitlines()[0])[:80]
            drafts.append(d)
        if drafts:
            log.info("AI извлёк %s событий: %s", len(drafts), ", ".join(f"«{x.title}»" for x in drafts))
            return drafts
        # если events пустой — считаем не событием
        return []

    # старый формат одиночного объекта
    draft = EventDraft.from_ai_json(data, tz=get_tz(tz_name), now=now)
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
    # эвристика: если в тексте явно два времени, но LLM вернул одно — пробуем локально дополнить
    if _count_times(text) >= 2:
        local_list = extract_events_local(text, tz_name, now)
        if len(local_list) >= 2:
            log.info("Эвристика: в тексте %s времени, локально нашли %s событий — используем локальные", _count_times(text), len(local_list))
            return local_list
    return [draft]


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
    settings = {
        "RELATIVE_BASE": now.replace(tzinfo=None),
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": False,
    }

    candidates: list[datetime] = []
    explicit_dates: set = set()

    # 1) Явные даты в тексте («29.09.2026», «15.10 в 18:30») — высший приоритет
    for m in re.finditer(
        r"\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?(?:\s*в\s*\d{1,2}[:.]\d{2})?", text
    ):
        naive = dateparser.parse(m.group(0), languages=["ru"], settings=settings)
        if naive:
            candidates.append(naive)
            explicit_dates.add(naive.date())

    naive = dateparser.parse(text, languages=["ru"], settings=settings)
    if naive:
        candidates.append(naive)

    try:
        from dateparser.search import search_dates

        found = search_dates(text, languages=["ru"], settings=settings) or []
        candidates += [dt for _, dt in found]
    except Exception:  # noqa: BLE001 — search_dates иногда падает на экзотике
        pass

    try:
        from rutimeparser import parse as ru_parse

        ru_dt = ru_parse(text, now=now.replace(tzinfo=None))
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
        return None
    # При прочих равных предпочитаем кандидатов с временем (не полночь),
    # затем ближайший к «сейчас»
    start = min(future, key=lambda d: (not (d.hour or d.minute),
                                       abs((d - now).total_seconds())))

    has_time = bool(start.hour or start.minute) or _has_explicit_time(text)
    start = _fix_morning_evening(start, text)
    first_sentence = re.split(r"(?<!\d)[.!?](?!\d)|\n", text.strip())[0].strip()
    return EventDraft(
        title=(first_sentence or "Событие")[:80],
        start=start,
        end=start + timedelta(minutes=60) if has_time else None,
        all_day=not has_time,
        confidence=0.5,
        missing=[] if has_time else ["time"],
        raw_text=text,
    )


def extract_events_local(text: str, tz_name: str, now: datetime | None = None) -> list:
    """Пытается вырезать несколько событий из одного сообщения офлайн."""
    # разбиваем по предложениям/союзам, сохраняя исходный текст для каждого куска
    parts = re.split(r"[\n;]+|\s+\.\s+|\s*\n\s*", text)
    # также режем по " На 10:00" " в 10:00" если есть два времени
    expanded = []
    for part in parts:
        # если в куске два времени типа "в 11:00 ... На 10:00" — режем по " На "
        sub = re.split(r"\s+(?:На\s+|на\s+|и\s+в\s+|\s+а\s+в\s+)", part)
        expanded.extend(sub)
    # fallback: если разбиение дало 1 часть но в тексте 2 времени — режем по времени
    if len(expanded) == 1 and _count_times(text) >= 2:
        # найдём второе время и разрежем
        times = list(re.finditer(r"\b\d{1,2}[:.]\d{2}\b", text))
        if len(times) >= 2:
            mid = times[1].start()
            # ищем ближайшую границу предложения до второго времени
            cut = text.rfind(".", 0, mid)
            if cut == -1:
                cut = text.rfind(" ", 0, mid-10)
            if cut != -1 and cut < mid:
                expanded = [text[:cut+1].strip(), text[cut+1:].strip()]

    drafts = []
    seen_starts = set()
    for chunk in expanded:
        chunk = chunk.strip()
        if not chunk or len(chunk) < 5:
            continue
        # эвристика: в куске должно быть время или слово "завтра/сегодня"
        if not (_has_explicit_time(chunk) or any(w in chunk.lower() for w in ["завтра","сегодня","послезавтра","вчера","понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"])):
            continue
        d = extract_event_local(chunk, tz_name, now)
        if d and d.start and d.start.isoformat() not in seen_starts:
            # уточняем title: берём первые слова куска
            first = re.split(r"(?<!\d)[.!?](?!\d)|\n", chunk)[0].strip()[:80]
            if first and d.title.lower() in ("событие","event") or len(d.title) < 5:
                d.title = first
            seen_starts.add(d.start.isoformat())
            drafts.append(d)
    # если нашли >=2 — сортируем по времени
    if len(drafts) >= 2:
        drafts.sort(key=lambda x: x.start)
        return drafts
    return drafts


def _count_times(text: str) -> int:
    return len(re.findall(r"\b\d{1,2}[:.]\d{2}\b", text))


_TIME_WORDS = {"утра": 0, "дня": 12, "вечера": 12, "ночи": 0}


def _has_explicit_time(text: str) -> bool:
    return bool(
        re.search(r"\b\d{1,2}[:.]\d{2}\b", text)
        or re.search(r"\b\d{1,2}\s*(?:утра|дня|вечера|ночи|час(?:а|ов)?)\b", text)
    )


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

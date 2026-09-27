"""Управление событиями текстом: «отмени приём», «перенеси встречу на 15:00».

Полностью офлайн: интент — по ключевым словам, поиск события — по БД,
новое время — локальным dateparser-каскадом. Никаких внешних API.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import Config
from bot.handlers.common import OwnerFilter, esc, get_tz_name
from bot.models import EventDraft, RRULE_WEEKDAYS, get_tz, rrule_to_recurrence
from bot.reminders import reminder_minutes, schedule_for_event
from bot.services.extractor import extract_event_local
from bot.services.gcal import GCalClient, GCalError

log = logging.getLogger(__name__)

router = Router(name="manage")
router.message.filter(OwnerFilter())
router.callback_query.filter(OwnerFilter())

_CANCEL_RE = re.compile(r"^(отмен\w*|удали\w*|сними)\s*(.*)$")
_MOVE_RE = re.compile(r"^(перенес\w*|сдвинь|поменяй время)\s+(.+)$")

_EXPLICIT_DATE_WORDS = (
    "завтра", "послезавтра", "сегодня", "через", "недел", "месяц",
    "понедельник", "вторник", "среду", "среда", "четверг", "четверга",
    "пятниц", "суббот", "воскресенье", "воскресенье",
)


def parse_manage_intent(text: str) -> tuple[str, str] | None:
    """«отмени приём» -> ("cancel", "приём"); «перенеси … на 15:00» -> ("move", …)."""
    t = (text or "").strip().lower()
    m = _CANCEL_RE.match(t)
    if m:
        return "cancel", (m.group(2) or "").strip()
    m = _MOVE_RE.match(t)
    if m:
        return "move", m.group(2).strip()
    return None


def _has_explicit_date(payload: str) -> bool:
    p = payload.lower()
    return bool(re.search(r"\d{1,2}[./]\d{1,2}", p)) or any(w in p for w in _EXPLICIT_DATE_WORDS)


def compose_move(old_start: datetime, old_end: datetime | None,
                 parsed_start: datetime, payload: str) -> tuple[datetime, datetime]:
    """Новое время: без явной даты в тексте — только время, дата прежняя."""
    if _has_explicit_date(payload):
        new_start = parsed_start
    else:
        new_start = old_start.replace(hour=parsed_start.hour, minute=parsed_start.minute)
    duration = (old_end - old_start) if old_end and old_end > old_start else timedelta(minutes=60)
    return new_start, new_start + duration


def _fmt(dt: datetime) -> str:
    return dt.strftime("%d.%m (%a) %H:%M").replace(
        "Mon", "пн").replace("Tue", "вт").replace("Wed", "ср").replace("Thu", "чт") \
        .replace("Fri", "пт").replace("Sat", "сб").replace("Sun", "вс")


class ManageStates(StatesGroup):
    confirm = State()


def _stems(text: str) -> set[str]:
    """Основы слов (первые 5 букв) — терпимы к падежам русского языка."""
    return {w[:5] for w in re.findall(r"[а-яё0-9]{4,}", text.lower()) if len(w) >= 4}


async def find_candidates(db, payload: str, now: datetime) -> list:
    """Активные события, подходящие под запрос (по пересечению основ слов)."""
    words = _stems(payload)
    out: list[tuple[int, object]] = []
    for r in await db.all_events():
        if not r["start_iso"]:
            continue
        try:
            start = datetime.fromisoformat(r["start_iso"])
        except ValueError:
            continue
        if start < now - timedelta(days=2):
            continue
        title = (r["title"] or "").lower()
        twords = _stems(title)
        score = sum(1 for s in words if s in title) + sum(1 for s in twords if s in payload)
        if score > 0 or not words:
            out.append((score, r))
    out.sort(key=lambda x: (-x[0], x[1]["start_iso"] or ""))
    return [r for _, r in out[:5]]


# ========================= ВХОД =========================

@router.message(StateFilter(None), F.text)
async def on_manage_text(message: Message, state: FSMContext, cfg: Config, db) -> None:
    intent = parse_manage_intent(message.text or "")
    if intent is None:
        raise SkipHandler()  # не наше — пусть обрабатывает обычный поток

    action, payload = intent
    tz_name = await get_tz_name(db, cfg)
    now = datetime.now(tz=get_tz(tz_name))

    cands = await find_candidates(db, payload, now)
    if not cands:
        await message.answer(
            "Не нашёл подходящих событий среди активных.\n"
            "Посмотрите список: /list — и повторите, например: «отмени приём у терапевта»."
        )
        return

    parsed_start = None
    if action == "move":
        local = extract_event_local(payload, tz_name, now)
        if local is None or local.start is None or local.all_day:
            await message.answer(
                "Укажите новое время, например:\n"
                "«перенеси приём на 15:00», «перенеси встречу на завтра в 10:30»,\n"
                "«перенеси приём на 01.10 в 9:00»."
            )
            return
        parsed_start = local.start

    if len(cands) > 1:
        rows = [[InlineKeyboardButton(
            text=f"«{r['title'][:40]}» — {_fmt(datetime.fromisoformat(r['start_iso']))}",
            callback_data=f"mg:pick:{r['id']}",
        )] for r in cands]
        # Без set_state кнопка mg:pick молча не срабатывает: у cb_pick
        # фильтр ManageStates.confirm (issue #6).
        await state.set_state(ManageStates.confirm)
        await state.update_data(
            action=action, payload=payload,
            new_start=parsed_start.isoformat() if parsed_start else None,
        )
        await message.answer(
            "Нашёл несколько событий — какое именно?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return

    await _ask_confirm(message, state, action, cands[0], payload, parsed_start)


async def _ask_confirm(message: Message, state: FSMContext, action: str, row,
                       payload: str, parsed_start: datetime | None) -> None:
    old_start = datetime.fromisoformat(row["start_iso"])
    old_end = (datetime.fromisoformat(row["end_iso"])
               if row["end_iso"] else None)

    title = esc(row["title"])  # заголовок — пользовательские данные
    if action == "cancel":
        text = f"❓ Отменить «<b>{title}</b>» ({_fmt(old_start)})?"
        new_start_iso = None
    else:
        new_start, new_end = compose_move(old_start, old_end, parsed_start, payload)
        text = (f"❓ Перенести «<b>{title}</b>»:\n"
                f"{_fmt(old_start)}  →  <b>{_fmt(new_start)}</b>?")
        new_start_iso = new_start.isoformat()

    await state.set_state(ManageStates.confirm)
    await state.update_data(action=action, pk=row["id"], new_start=new_start_iso)
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data="mg:yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="mg:no"),
    ]]))


@router.callback_query(ManageStates.confirm, F.data.startswith("mg:pick:"))
async def cb_pick(callback: CallbackQuery, state: FSMContext, db) -> None:
    pk = int(callback.data.split(":")[2])
    row = await db.get_event_by_pk(pk)
    data = await state.get_data()
    await callback.answer()
    if row is None or not data.get("action"):
        await callback.message.answer("Контекст потерялся, начните заново.")
        await state.clear()
        return
    parsed_start = (datetime.fromisoformat(data["new_start"])
                    if data.get("new_start") else None)
    await _ask_confirm(callback.message, state, data["action"], row,
                       data.get("payload", ""), parsed_start)


# ========================= ПОДТВЕРЖДЕНИЕ =========================

@router.callback_query(ManageStates.confirm, F.data == "mg:no")
async def cb_no(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Ок")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Ничего не менял.")


@router.callback_query(ManageStates.confirm, F.data == "mg:yes")
async def cb_yes(callback: CallbackQuery, state: FSMContext, cfg: Config, db,
                 gcal: GCalClient | None) -> None:
    data = await state.get_data()
    action, pk = data.get("action"), data.get("pk")
    await state.clear()
    await callback.answer()
    if action not in ("cancel", "move") or pk is None:
        await callback.message.answer("Контекст потерялся, начните заново.")
        return

    row = await db.get_event_by_pk(pk)
    if row is None or row["deleted"]:
        await callback.message.answer("Событие уже удалено.")
        return

    tz_name = await get_tz_name(db, cfg)

    if action == "cancel":
        if gcal is not None and row["calendar_id"] != "ics":
            try:
                await gcal.delete_event(row["calendar_id"], row["event_id"])
            except GCalError as e:
                await callback.message.answer(f"⚠️ Не удалось удалить из календаря: {e}")
                return
        await db.mark_deleted(pk)
        await db.cancel_reminders_for_event(pk)
        await db.log_request("manage", "cancelled", row["title"])
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"🗑 Отменил «{esc(row['title'])}» (и его напоминание)."
        )
        return

    # ---- перенос ----
    if not data.get("new_start"):
        await callback.message.answer("Не вижу нового времени — начните заново.")
        return
    new_start = datetime.fromisoformat(data["new_start"])
    old_start = datetime.fromisoformat(row["start_iso"])
    old_end = datetime.fromisoformat(row["end_iso"]) if row["end_iso"] else None
    duration = (old_end - old_start) if old_end and old_end > old_start else timedelta(minutes=60)
    new_end = new_start + duration

    # Повторяющееся событие: серия переякоривается к новой дате (freq сохраняется;
    # у weekly BYDAY обновляется под новый день недели). Иначе напоминания
    # закладывались бы только на одно вхождение (issue #6).
    recurrence = rrule_to_recurrence(row["rrule"])
    new_rrule: str | None = None
    if recurrence:
        if str(recurrence.get("freq", "")).upper() == "WEEKLY" and recurrence.get("byday"):
            recurrence = {**recurrence, "byday": RRULE_WEEKDAYS[new_start.weekday()]}
        moved = EventDraft(title=row["title"], start=new_start, recurrence=recurrence)
        new_rrule = moved.rrule
    else:
        moved = EventDraft(title=row["title"], start=new_start)

    if gcal is not None and row["calendar_id"] != "ics":
        try:
            await gcal.update_event_times(row["calendar_id"], row["event_id"],
                                          new_start, new_end, tz_name)
        except GCalError as e:
            await callback.message.answer(f"⚠️ Не удалось перенести в календаре: {e}")
            return

    await db.update_event_times(pk, new_start.isoformat(), new_end.isoformat(),
                                rrule=new_rrule, set_rrule=new_rrule is not None)
    await db.cancel_reminders_for_event(pk)
    minutes = reminder_minutes(await db.get_setting("reminders", "10"))
    await schedule_for_event(db, pk, moved, minutes)
    await db.log_request("manage", "moved", row["title"])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"✅ Перенёс «{esc(row['title'])}» на {_fmt(new_start)}."
    )

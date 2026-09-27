"""Команды: /start /help /list /undo /settings /today /tomorrow /stats (ТЗ §2.5)."""

from __future__ import annotations

import html
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import Config
from bot.handlers.common import OwnerFilter, get_calendar_id, get_tz_name
from bot.models import WEEKDAYS_RU_FULL, get_tz
from bot.services.gcal import GCalClient, GCalError

router = Router(name="commands")
router.message.filter(OwnerFilter())
router.callback_query.filter(OwnerFilter())

_START_TIME = time.time()

TZ_OPTIONS = [
    "Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg",
    "Asia/Novosibirsk", "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Vladivostok",
    "Europe/Amsterdam",
]


class SettingsStates(StatesGroup):
    waiting_tz = State()
    waiting_calendar = State()


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>Я ваш AI-секретарь</b>.\n\n"
        "Пришлите текстом, голосом или пересылкой сообщение о встрече/деле — "
        "я распознаю суть и предложу файл .ics для импорта в календарь.\n\n"
        "Примеры:\n"
        "• «встреча с Иваном завтра в 15:00 в офисе на Ленина, на час»\n"
        "• «созвон с командой каждую пятницу в 10:00»\n"
        "• 🎤 голосовое: «напомни послезавтра в два часа дня позвонить маме»\n\n"
        "<b>Команды:</b>\n"
        "/today — события сегодня (платный доступ)\n"
        "/tomorrow — события завтра (платный доступ)\n"
        "/list — последние 10 созданных событий\n"
        "/undo — удалить последнее событие\n"
        "/settings — календарь, часовой пояс, напоминания\n"
        "/stats — статистика (платный доступ)\n"
        "/cancel — сбросить диалог\n\n"
        "<b>Слова-команды</b> (платный доступ):\n"
        "«отмени приём у терапевта» — отмена события\n"
        "«перенеси встречу на 15:00» или «…на завтра в 10:30» — перенос\n\n"
        "Несколько событий, повторы и OCR фото — платный доступ. Оплата пока не подключена."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, сбросил. Пришлите новое описание события.")


@router.message(Command("list"))
async def cmd_list(message: Message, db) -> None:
    rows = await db.list_events(10)
    if not rows:
        await message.answer("Пока я не создал ни одного события. Пришлите описание встречи!")
        return
    lines = ["🗓 <b>Последние события:</b>\n"]
    for i, r in enumerate(rows, 1):
        when = (r["start_iso"] or "?").replace("T", " ")[:16]
        title = r["title"]
        if r["link"]:
            lines.append(f"{i}. <a href=\"{r['link']}\">{title}</a> — {when}")
        elif r["via_ics"]:
            lines.append(f"{i}. {title} — {when} <i>(.ics)</i>")
        else:
            lines.append(f"{i}. {title} — {when}")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("today"))
async def cmd_today(message: Message, db, cfg: Config) -> None:
    await _send_day(message, db, cfg, 0)


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message, db, cfg: Config) -> None:
    await _send_day(message, db, cfg, 1)


_RR_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _rrule_occurrence(rrule: str, start: datetime, day0: datetime) -> datetime | None:
    """Вхождение повторяющегося события в конкретный день (или None)."""
    from dateutil.rrule import rrulestr
    try:
        candidate = rrulestr(rrule, dtstart=start).after(day0, inc=True)
        return candidate if candidate and candidate.date() == day0.date() else None
    except (ValueError, TypeError):
        return None


async def _send_day(message: Message, db, cfg: Config, offset: int) -> None:
    tz = get_tz(await get_tz_name(db, cfg))
    now = datetime.now(tz=tz)
    day0 = (now + timedelta(days=offset)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day1 = day0 + timedelta(days=1)

    items: list[tuple[datetime, str]] = []
    for r in await db.all_events():
        if not r["start_iso"]:
            continue
        try:
            s = datetime.fromisoformat(r["start_iso"]).astimezone(tz)
        except ValueError:
            continue
        e = None
        if r["end_iso"]:
            try:
                e = datetime.fromisoformat(r["end_iso"]).astimezone(tz)
            except ValueError:
                e = None
        in_day = day0 <= s < day1
        if not in_day and r["rrule"]:
            occ = _rrule_occurrence(r["rrule"], s, day0)
            if occ:
                s, e, in_day = occ, None, True
        if not in_day:
            continue
        line = f"• <b>{s:%H:%M}</b>"
        if e and day0 <= e < day1 and not r["rrule"]:
            line += f"–{e:%H:%M}"
        line += f" — {html.escape(r['title'])}"
        if r["rrule"]:
            line += " 🔁"
        items.append((s, line))

    label = "Сегодня" if offset == 0 else "Завтра" if offset == 1 else day0.strftime("%d.%m")
    header = f"📅 <b>{label}, {day0:%d.%m} ({WEEKDAYS_RU_FULL[day0.weekday()]})</b>"
    if not items:
        await message.answer(header + "\n\nСобытий нет 🎉")
        return
    items.sort(key=lambda x: x[0])
    await message.answer(header + "\n\n" + "\n".join(l for _, l in items))


@router.message(Command("stats"))
async def cmd_stats(message: Message, db) -> None:
    st = await db.stats()
    # Размер БД ТЕКУЩЕГО пользователя (у владельца это bot.db, у остальных
    # users/<ID>.db) — cfg.db_path здесь нельзя: это база legacy-владельца.
    size = db.path.stat().st_size / 1024 if db.path.exists() else 0
    up = int(time.time() - _START_TIME)
    giga_fails = st["reqlog"].get("failed", 0)
    created = st["reqlog"].get("created", 0)
    await message.answer(
        "📊 <b>Статистика</b>\n"
        f"Событий в истории: <b>{st['events']}</b> "
        f"(через .ics: {st['events_ics']}, удалено: {st['events_deleted']})\n"
        f"Напоминаний: отправлено <b>{st['reminders_sent']}</b>, "
        f"ждут <b>{st['reminders_pending']}</b>\n"
        f"Создано кнопкой: {created} • Сбоев AI: {giga_fails}\n"
        f"База: {size:.0f} КБ • Аптайм сессии: {up // 3600} ч {(up % 3600) // 60} мин"
    )


@router.message(Command("undo"))
async def cmd_undo(message: Message, db, gcal: GCalClient | None) -> None:
    row = await db.get_last_event()
    if row is None:
        await message.answer("Нечего отменять — я пока ничего не создавал.")
        return
    if row["via_ics"]:
        await db.mark_deleted(row["id"])
        await message.answer(
            f"«{row['title']}» убрано из истории. Файл .ics уже был отправлен — "
            "если вы успели его импортировать, удалите событие в календаре вручную."
        )
        return
    if gcal is None:
        await message.answer("⚠️ Google Calendar не настроен — удалять нечего.")
        return
    try:
        await gcal.delete_event(row["calendar_id"], row["event_id"])
    except GCalError as e:
        await message.answer(f"Не удалось удалить: {e}. Попробуйте позже.")
        return
    await db.mark_deleted(row["id"])
    await db.cancel_reminders_for_event(row["id"])
    await db.log_request("command", "undone", row["title"])
    await message.answer(f"🗑 Удалил «{row['title']}» из календаря (и его напоминание).")


@router.message(Command("settings"))
async def cmd_settings(message: Message, db, cfg: Config) -> None:
    tz = await get_tz_name(db, cfg)
    calendar_id = await get_calendar_id(db, cfg)
    reminders = await db.get_setting("reminders", "10")
    await message.answer(
        "⚙️ <b>Настройки</b>\n"
        f"Часовой пояс: <b>{tz}</b>\n"
        f"Календарь: <b>{calendar_id}</b>\n"
        f"Напоминания: <b>{_remind_label(reminders)}</b>\n\n"
        "Что изменить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🕐 Часовой пояс", callback_data="st:tz"),
            InlineKeyboardButton(text="📚 Календарь", callback_data="st:cal"),
            InlineKeyboardButton(text="🔔 Напоминания", callback_data="st:remind"),
        ]]),
    )


@router.callback_query(F.data == "st:tz")
async def cb_tz_menu(callback: CallbackQuery) -> None:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"st:tzset:{name}")]
        for name in TZ_OPTIONS
    ]
    rows.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="st:tzcustom")])
    await callback.message.edit_text(
        "Выберите часовой пояс:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("st:tzset:"))
async def cb_tz_set(callback: CallbackQuery, db) -> None:
    tz = callback.data.split(":", 2)[2]
    await db.set_setting("timezone", tz)
    await callback.message.edit_text(f"✅ Часовой пояс: <b>{tz}</b>")
    await callback.answer("Сохранено")


@router.callback_query(F.data == "st:tzcustom")
async def cb_tz_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_tz)
    await callback.message.edit_text(
        "Пришлите имя таймзоны, например <code>Europe/Moscow</code>:"
    )
    await callback.answer()


@router.message(SettingsStates.waiting_tz, F.text)
async def msg_tz(message: Message, db, state: FSMContext) -> None:
    name = (message.text or "").strip()
    try:
        ZoneInfo(name)
    except Exception:
        await message.answer("Не знаю такую таймзону. Пример: <code>Europe/Moscow</code>")
        return
    await db.set_setting("timezone", name)
    await state.clear()
    await message.answer(f"✅ Часовой пояс: <b>{name}</b>")


@router.callback_query(F.data == "st:cal")
async def cb_cal(callback: CallbackQuery, db, state: FSMContext) -> None:
    current = await get_calendar_id(db, None) or "primary"
    await state.set_state(SettingsStates.waiting_calendar)
    await callback.message.edit_text(
        f"Текущий календарь: <b>{current}</b>.\n\n"
        "ID календаря — это e-mail вида <code>you@gmail.com</code> (основной) или ID "
        "второго календаря (настройки календаря → «Интеграция» → «ID календаря»). "
        "Пришлите ID или <code>primary</code>:"
    )
    await callback.answer()


@router.message(SettingsStates.waiting_calendar, F.text)
async def msg_cal(message: Message, db, state: FSMContext) -> None:
    value = (message.text or "").strip() or "primary"
    await db.set_setting("calendar_id", value)
    await state.clear()
    await message.answer(f"✅ События будут создаваться в <b>{value}</b>")


@router.callback_query(F.data == "st:remind")
async def cb_remind(callback: CallbackQuery, db) -> None:
    current = await db.get_setting("reminders", "10")
    options = [("10", "За 10 минут"), ("30", "За 30 минут"),
               ("60", "За час"), ("off", "Без напоминаний")]
    rows = [[
        InlineKeyboardButton(
            text=("✅ " if current == v else "") + label,
            callback_data=f"st:remset:{v}",
        )
    ] for v, label in options]
    await callback.message.edit_text(
        f"🔔 Напоминания в Telegram: <b>{_remind_label(current)}</b>\n"
        "Бот сам напишет вам заранее. Выберите:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


def _remind_label(v: str) -> str:
    return "выкл" if v == "off" else f"за {v} минут"


@router.callback_query(F.data.startswith("st:remset:"))
async def cb_remind_set(callback: CallbackQuery, db) -> None:
    value = callback.data.split(":", 2)[2]
    await db.set_setting("reminders", value)
    await callback.message.edit_text(f"🔔 Напоминания: <b>{_remind_label(value)}</b>")
    await callback.answer("Сохранено")

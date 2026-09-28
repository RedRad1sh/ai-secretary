"""Основной поток: текст / голос / пересланные -> AI -> предпросмотр -> событие.

Реализует ТЗ §2.1 (User Flow), §2.2 (типы ввода), §2.5 (обратная связь).
"""

from __future__ import annotations

import logging
import uuid

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import Config
from bot.handlers.common import OwnerFilter, esc, get_calendar_id, get_tz_name
from bot.models import EventDraft, multi_preview_text, plural_ru
from bot.reminders import reminder_minutes, schedule_for_event
from bot.services.extractor import extract_event, extract_event_local, extract_events, extract_events_local
from bot.services.gcal import GCalClient, GCalError
from bot.services.llm import LLMError
from bot.services.ics import build_gcal_link, build_ics, safe_filename

log = logging.getLogger(__name__)

router = Router(name="parse_flow")
router.message.filter(OwnerFilter())
router.callback_query.filter(OwnerFilter())


class ParseStates(StatesGroup):
    waiting_clarification = State()  # ТЗ §2.5: бот просит уточнить дату
    waiting_edit = State()           # ТЗ §2.1: кнопка «Изменить»


CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Создать", callback_data="ev:create"),
    InlineKeyboardButton(text="✏️ Изменить", callback_data="ev:edit"),
    InlineKeyboardButton(text="❌ Отмена", callback_data="ev:cancel"),
]])


def _multi_kb(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Создать все ({n})", callback_data="ev:create_all"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data="ev:edit"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="ev:cancel"),
    ]])


# ========================= ВХОДНЫЕ ТОЧКИ =========================

@router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext, cfg: Config, db,
                  llm) -> None:
    """Текстовые И пересланные сообщения: у пересланного текст уже в message.text;
    у пересланных медиа текст лежит в caption (ТЗ §2.2)."""
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("Пришлите описание события текстом 🙏")
        return
    source = "forward" if message.forward_origin else "text"
    await _process(message, state, cfg, db, llm, text, source)


@router.message(F.voice)
async def on_voice(message: Message, state: FSMContext, cfg: Config, db,
                   llm, bot) -> None:
    """Голосовое сообщение: транскрипция через GigaChat Whisper -> тот же пайплайн."""
    if llm is None:
        await message.answer("Распознавание голоса не настроено. Пришлите текст.")
        return
    try:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:  # noqa: BLE001 — косметика
        pass
    try:
        f = await bot.get_file(message.voice.file_id)
        buf = await bot.download_file(f.file_path)
        transcript = await llm.transcribe(buf.getvalue())
    except LLMError as e:
        log.exception("ASR (audio/transcriptions) failed")
        await message.answer(f"🤷 Не получилось распознать голос: {e}")
        await db.log_request("voice", "failed", str(e))
        return
    if not transcript:
        await message.answer("Голос не распознан. Попробуйте ещё раз или напишите текстом.")
        return
    note = await message.answer(f"🎤 <i>«{esc(transcript)}»</i>")
    await _process(note, state, cfg, db, llm, transcript, "voice")


@router.message(F.photo)
async def on_photo(message: Message, state: FSMContext, cfg: Config, db, llm, bot) -> None:
    from bot.access import PAID_NOTICE
    from bot.services.ocr import recognize_image
    if not cfg.is_paid(message.from_user.id):
        await message.answer(PAID_NOTICE)
        return
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > 5 * 1024 * 1024:
        await message.answer("Изображение больше 5 МБ. Пришлите уменьшенное фото.")
        return
    try:
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        text = await recognize_image(buf.getvalue())
    except (ValueError, TimeoutError) as e:
        await message.answer("OCR недоступен или не смог прочитать фото. Пришлите текст.")
        log.warning("OCR failed: %s", e)
        return
    if not text:
        await message.answer("Текст на фото не найден. Пришлите более чёткое изображение.")
        return
    await _process(message, state, cfg, db, llm, text + "\n" + (message.caption or ""), "photo")


@router.message(F.video | F.document)
async def on_media(message: Message) -> None:
    await message.answer("Пришлите изображение как фото, либо описание события текстом.")


# ========================= ЯДРО ПОТОКА =========================

async def _process(message: Message, state: FSMContext, cfg: Config, db,
                   llm, text: str, source: str) -> None:
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:  # noqa: BLE001 — «печатает…» косметика, не должна ломать поток
        pass
    if not cfg.is_paid(state.key.user_id):
        import re
        from bot.access import PAID_NOTICE
        if re.search(r"кажд\w*|ежедневно|еженедельно|по будням|[;\n].*\d|,?\s+(?:а|и)\s+в\s+\d", text.lower()):
            await message.answer(PAID_NOTICE + " Можно создать одно неповторяющееся событие.")
            return
    tz_name = await get_tz_name(db, cfg)
    try:
        drafts = await extract_events(llm, text, tz_name)
    except LLMError as e:
        await message.answer(
            f"⚠️ AI-модуль недоступен: {e}\nПроверьте LLM_API_URL / LLM_MODEL / LLM_API_KEY (см. логи выше — там сырой ответ провайдера)."
        )
        await db.log_request(source, "failed", str(e))
        return

    if not drafts:
        await message.answer(
            "🤔 Не похоже на встречу/дело. Пришлите описание события — "
            "например: «встреча с Иваном завтра в 15:00»."
        )
        await db.log_request(source, "no_event", text[:120])
        return

    if not cfg.is_paid(state.key.user_id) and (len(drafts) > 1 or any(d.recurrence for d in drafts)):
        from bot.access import PAID_NOTICE
        await state.clear()
        await message.answer(PAID_NOTICE + " Можно создать одно неповторяющееся событие.")
        return

    if len(drafts) == 1:
        await _show_preview(message, state, drafts[0], tz_name)
        await db.log_request(source, "preview", drafts[0].title)
    else:
        await _show_multi_preview(message, state, drafts, tz_name)
        await db.log_request(source, "preview_multi", f"{len(drafts)}: " + ", ".join(d.title for d in drafts)[:200])


async def _show_preview(message: Message, state: FSMContext, draft: EventDraft,
                        tz_name: str) -> None:
    """ТЗ §2.1 п.4: карточка предпросмотра с кнопками."""
    data = await state.get_data()
    stored: EventDraft | None = data.get("draft")
    # при фазе уточнения мержим уже известные поля с новыми
    if stored and draft.start is None and stored.start is not None:
        draft = stored

    if draft.needs_date:
        await state.set_state(ParseStates.waiting_clarification)
        await state.update_data(draft=draft, tz_name=tz_name, drafts=None)
        await message.answer(
            f"Понял: <b>{draft.title}</b>.\n"
            "❓ Не удалось определить дату. Напишите, пожалуйста, когда это должно "
            "произойти? (например: «завтра в 15:00», «в следующий вторник», «15.10.2026»)"
        )
        return

    await state.set_state(None)
    await state.update_data(draft=draft, drafts=None, tz_name=tz_name)
    await message.answer(
        draft.preview_text(tz_name) + "\n\n<i>Создать событие?</i>", reply_markup=CONFIRM_KB
    )


async def _show_multi_preview(message: Message, state: FSMContext, drafts: list[EventDraft],
                              tz_name: str) -> None:
    """Предпросмотр нескольких событий из одного сообщения (этап 4.1)."""
    need_date = [d for d in drafts if d.needs_date]
    if need_date:
        # если хотя бы у одного нет даты — просим уточнить первое такое
        await state.set_state(ParseStates.waiting_clarification)
        await state.update_data(drafts=drafts, tz_name=tz_name, draft=None)
        titles = ", ".join(f"«{d.title}»" for d in need_date)
        await message.answer(
            f"Нашёл {len(drafts)} {plural_ru(len(drafts), 'событие', 'события', 'событий')}, "
            f"но у {titles} не понял дату.\n"
            "❓ Напишите дату для них (например: «завтра» — применится ко всем без даты, для разных дат нажмите ✏️ и пришлите полное исправленное описание)."
        )
        return
    await state.set_state(None)
    await state.update_data(drafts=drafts, draft=None, tz_name=tz_name)
    # серии (≥3 похожих) показываем компактным блоком, остальное — карточками (issue #13)
    txt = multi_preview_text(drafts, tz_name)
    await message.answer(
        f"Нашёл {len(drafts)} {plural_ru(len(drafts), 'событие', 'события', 'событий')}:\n\n"
        + txt + "\n\n<i>Создать все?</i>",
        reply_markup=_multi_kb(len(drafts)),
    )


# ========================= УТОЧНЕНИЕ / ИЗМЕНЕНИЕ =========================

@router.message(ParseStates.waiting_clarification, F.text)
async def on_clarify(message: Message, state: FSMContext, cfg: Config, db) -> None:
    data = await state.get_data()
    draft: EventDraft = data["draft"]
    tz_name: str = data.get("tz_name", cfg.default_tz)

    local = extract_event_local(message.text or "", tz_name)
    if local is None or local.start is None:
        await message.answer(
            "Не понял дату 🙏 Попробуйте формат: <code>15.10.2026 14:00</code>, "
            "«завтра в 15», «через неделю», «в пятницу в 18:30»."
        )
        await db.log_request("clarify", "retry", message.text[:120])
        return
    targets = data.get("drafts") or [draft]
    for item in targets:
        if item and item.needs_date:
            item.start = local.start
            item.end = local.end
            item.all_day = item.all_day and local.all_day
            item.missing = [m for m in item.missing if m not in ("date", "time")]
    if data.get("drafts"):
        await _show_multi_preview(message, state, targets, tz_name)
    else:
        await _show_preview(message, state, draft, tz_name)


@router.message(ParseStates.waiting_edit, F.text)
async def on_edit(message: Message, state: FSMContext, cfg: Config, db,
                  llm) -> None:
    """Кнопка «Изменить»: пользователь присылает исправленный текст — парсим заново."""
    await _process(message, state, cfg, db, llm, message.text or "", "edit")


@router.message(ParseStates.waiting_clarification)
@router.message(ParseStates.waiting_edit)
async def on_state_other(message: Message) -> None:
    await message.answer("Жду от вас текст 🙏 (или /cancel для сброса)")


# ========================= НАПОМИНАНИЯ =========================

async def _schedule_reminder(db, event_pk: int | None, draft: EventDraft) -> None:
    """Планирует Telegram-напоминание согласно настройкам (/settings → 🔔).

    event_pk обязателен: без него cancel_reminders_for_event(pk) при
    отмене/переносе/undo не найдёт строки, и напоминание уйдёт на
    удалённое событие (issue #6).
    """
    minutes = reminder_minutes(await db.get_setting("reminders", "10"))
    await schedule_for_event(db, event_pk, draft, minutes)


# ========================= INLINE-КНОПКИ =========================

@router.callback_query(F.data == "ev:create_all")
async def cb_create_all(callback: CallbackQuery, state: FSMContext, cfg: Config, db,
                        gcal: GCalClient | None) -> None:
    """Создание нескольких событий из одного сообщения."""
    data = await state.get_data()
    drafts: list[EventDraft] | None = data.get("drafts")
    tz_name: str = data.get("tz_name", cfg.default_tz)
    await callback.answer()
    if not drafts:
        await callback.message.answer("Черновики потерялись 😕 Пришлите описание заново.")
        await state.clear()
        return
    await _create_many(callback, state, cfg, db, gcal, drafts, tz_name)


@router.callback_query(F.data == "ev:create")
async def cb_create(callback: CallbackQuery, state: FSMContext, cfg: Config, db,
                    gcal: GCalClient | None) -> None:
    """ТЗ §2.1 п.5–6: создание события и подтверждение со ссылкой."""
    data = await state.get_data()
    # поддержка мульти: если в state лежат drafts, берём первый (совместимость)
    drafts = data.get("drafts")
    if drafts and isinstance(drafts, list) and len(drafts) >= 1 and not data.get("draft"):
        # если нажали старую кнопку на мульти — создаём всё
        return await cb_create_all(callback, state, cfg, db, gcal)
    draft: EventDraft | None = data.get("draft")
    tz_name: str = data.get("tz_name", cfg.default_tz)
    await callback.answer()
    if draft is None or draft.start is None:
        await callback.message.answer("Черновик потерялся 😕 Пришлите описание события заново.")
        await state.clear()
        return

    calendar_id = await get_calendar_id(db, cfg)

    if gcal is None:
        await _send_ics(callback, state, db, draft, tz_name,
                        "📎 Google Calendar пока не настроен — держите <b>.ics</b>-файл: он "
                        "откроется в любом календаре.\n"
                        "<i>Как включить автосоздание: docs/SETUP_GOOGLE.md</i>")
        return

    try:
        event = await gcal.create_event(draft, calendar_id, tz_name)
    except GCalError as e:
        log.warning("GCal create failed: %s", e)
        await _send_ics(callback, state, db, draft, tz_name,
                        f"⚠️ Google Calendar недоступен ({e}).\nДержите файл — импортируйте вручную:")
        return

    pk = await db.add_event(
        calendar_id=calendar_id, event_id=event["id"], title=draft.title,
        start_iso=draft.start.isoformat() if draft.start else None,
        end_iso=draft.end.isoformat() if draft.end else None,
        rrule=draft.rrule, link=event.get("htmlLink"),
    )
    await _schedule_reminder(db, pk, draft)
    await db.log_request("button", "created", draft.title)
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: S110 — сообщение могло быть отредактировано ранее
        pass
    await callback.message.answer(
        f"✅ Создано: <b>{esc(draft.title)}</b>\n"
        f"🔗 <a href=\"{esc(event.get('htmlLink', ''))}\">Открыть в календаре</a>",
        disable_web_page_preview=True,
    )


async def _create_many(callback: CallbackQuery, state: FSMContext, cfg: Config, db,
                       gcal: GCalClient | None, drafts: list[EventDraft], tz_name: str) -> None:
    calendar_id = await get_calendar_id(db, cfg)
    # если gcal не настроен — шлём пачкой .ics
    if gcal is None:
        for d in drafts:
            content = build_ics(d, tz_name)
            pk = await db.add_event(
                calendar_id="ics", event_id=f"ics-{uuid.uuid4().hex[:8]}", title=d.title,
                start_iso=d.start.isoformat() if d.start else None,
                end_iso=d.end.isoformat() if d.end else None,
                rrule=d.rrule, via_ics=True,
            )
            await _schedule_reminder(db, pk, d)
        await db.log_request("button", "ics_fallback_multi", f"{len(drafts)}")
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        # шлём каждый .ics отдельным документом (пачкой)
        for d in drafts:
            content = build_ics(d, tz_name)
            link = build_gcal_link(d)
            kb = None
            if link:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📅 Открыть в Google Календаре", url=link)]])
            await callback.message.answer_document(
                BufferedInputFile(content, filename=f"{safe_filename(d.title)}.ics"),
                caption=f"📎 <b>{esc(d.title)}</b> — .ics файл",
                reply_markup=kb,
            )
        await callback.message.answer(f"✅ Создано {len(drafts)} событий (режим .ics).")
        return

    # gcal настроен — создаём по очереди
    created = []
    for d in drafts:
        try:
            event = await gcal.create_event(d, calendar_id, tz_name)
            pk = await db.add_event(
                calendar_id=calendar_id, event_id=event["id"], title=d.title,
                start_iso=d.start.isoformat() if d.start else None,
                end_iso=d.end.isoformat() if d.end else None,
                rrule=d.rrule, link=event.get("htmlLink"),
            )
            await _schedule_reminder(db, pk, d)
            created.append((d.title, event.get("htmlLink")))
        except GCalError as e:
            log.warning("GCal create failed for %s: %s", d.title, e)
            # фолбек в .ics для этого события
            content = build_ics(d, tz_name)
            pk = await db.add_event(
                calendar_id="ics", event_id=f"ics-{uuid.uuid4().hex[:8]}", title=d.title,
                start_iso=d.start.isoformat() if d.start else None,
                end_iso=d.end.isoformat() if d.end else None,
                rrule=d.rrule, via_ics=True,
            )
            await _schedule_reminder(db, pk, d)
            created.append((d.title + " (.ics)", None))
    await db.log_request("button", "created_multi", f"{len(created)}")
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    lines = []
    for title, link in created:
        if link:
            lines.append(f"✅ <b>{esc(title)}</b> — <a href=\"{esc(link)}\">открыть</a>")
        else:
            lines.append(f"✅ <b>{esc(title)}</b> (файл .ics выше)")
    await callback.message.answer("\n".join(lines), disable_web_page_preview=True)


async def _send_ics(callback: CallbackQuery, state: FSMContext, db, draft: EventDraft,
                    tz_name: str, preamble: str) -> None:
    """Fallback из ТЗ §8: .ics-файл + прямая ссылка на Google Календарь."""
    content = build_ics(draft, tz_name)
    pk = await db.add_event(
        calendar_id="ics", event_id=f"ics-{uuid.uuid4().hex[:8]}", title=draft.title,
        start_iso=draft.start.isoformat() if draft.start else None,
        end_iso=draft.end.isoformat() if draft.end else None,
        rrule=draft.rrule, via_ics=True,
    )
    await _schedule_reminder(db, pk, draft)
    await db.log_request("button", "ics_fallback", draft.title)
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: S110
        pass

    keyboard = None
    link = build_gcal_link(draft)
    if link:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📅 Открыть в Google Календаре", url=link)
        ]])
    await callback.message.answer_document(
        BufferedInputFile(content, filename=f"{safe_filename(draft.title)}.ics"),
        caption=preamble + "\n\n💡 Или нажмите кнопку ниже — событие добавится одной кнопкой.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "ev:edit")
async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ParseStates.waiting_edit)
    await callback.answer()
    await callback.message.answer(
        "✏️ Пришлите исправленное описание события целиком (или /cancel):"
    )


@router.callback_query(F.data == "ev:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    await db.log_request("button", "cancelled")
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: S110
        pass

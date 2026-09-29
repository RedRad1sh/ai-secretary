"""Приёмка этапа 4: OCR-конвейер, платный шлюз, сквозные сценарии (issue #6).

Кодовая часть issue #6:
A. OCR-конвейер: фото -> recognize_image -> _process -> предпросмотр ->
   подтверждение -> .ics. Заглушка OCR + лимиты (>5 МБ, таймаут 20 с,
   PDF/видео — отказ, пустой OCR — вежливый отказ). Если в системе есть
   бинарник tesseract (в Docker — tesseract-ocr rus+eng), часть A8/A9
   прогоняет НАСТОЯЩЕЕ распознавание: сгенерированное фото с рус+англ
   текстом -> recognize_image -> карточка -> .ics.
B. Платный шлюз OCR: неплатному фото отвечает PAID_NOTICE, OCR не вызывается.
C. Сквозные сценарии этапа 4 (LLM-заглушки, через build_dispatcher):
   пакет событий -> «Создать все» -> пачка .ics; повторяющееся событие ->
   RRULE в .ics и напоминания на вхождения; /today//tomorrow с повтором;
   перенос текстом (длительность сохраняется, напоминание перепланируется);
   отмена текстом (событие deleted, напоминания отменены — включая
   legacy-напоминания с event_pk=NULL, созданные до фикса issue #6).

Запуск: python tests/test_stage4_acceptance.py   (сеть не нужна;
tesseract/Pillow необязательны — реальные OCR-проверки тогда пропускаются)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ALLOWED_USER_ID", "100")
os.environ.setdefault("ALLOWED_USER_IDS", "100,200,300")
os.environ.setdefault("PAID_USER_IDS", "100,200")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ai-secretary-s4-"))

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import File, Message, Update  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402
from bot.services import ocr as ocr_mod  # noqa: E402

OWN, FREE = 100, 300
TODAY = datetime.now(ZoneInfo("Europe/Moscow"))
TODAY_ISO = TODAY.strftime("%Y-%m-%d")
TOMORROW = TODAY + timedelta(days=1)
TOMORROW_ISO = TOMORROW.strftime("%Y-%m-%d")
BIG_FUTURE = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()

passed = failed = skipped = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ {name}")
    else:
        failed += 1
        print(f"❌ {name}  -> {extra}")


def skip(name: str, why: str) -> None:
    global skipped
    skipped += 1
    print(f"⏭️  {name}  (пропуск: {why})")


def _friday_from_tomorrow() -> str:
    d = TOMORROW
    return (d + timedelta(days=(4 - d.weekday()) % 7)).strftime("%Y-%m-%d")


MSK = ZoneInfo("Europe/Moscow")


def _msk(start_iso: str) -> datetime:
    """start_iso из БД (UTC) -> московское время для сравнения часа/дня."""
    return datetime.fromisoformat(start_iso).astimezone(MSK)


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, system: str, user: str, **kw) -> str:
        self.calls.append(user)
        if "планёрка" in user and "врач" in user:
            return json.dumps({"events": [
                {"is_event": True, "title": "Планёрка", "date": TOMORROW_ISO,
                 "time": "09:00", "duration_minutes": 60, "all_day": False,
                 "recurrence": None, "confidence": 0.95, "missing": []},
                {"is_event": True, "title": "Врач", "date": TOMORROW_ISO,
                 "time": "15:00", "duration_minutes": 60, "all_day": False,
                 "recurrence": None, "confidence": 0.95, "missing": []},
            ]}, ensure_ascii=False)
        if "пятниц" in user.lower():
            return json.dumps({"is_event": True, "title": "Созвон команды",
                               "date": _friday_from_tomorrow(), "time": "18:30",
                               "duration_minutes": 60, "all_day": False,
                               "recurrence": {"freq": "WEEKLY", "byday": "FR"},
                               "confidence": 0.95, "missing": []}, ensure_ascii=False)
        if "каждый день" in user.lower():
            return json.dumps({"is_event": True, "title": "Ежедневный звонок",
                               "date": TODAY_ISO, "time": "09:00",
                               "duration_minutes": 30, "all_day": False,
                               "recurrence": {"freq": "DAILY"},
                               "confidence": 0.95, "missing": []}, ensure_ascii=False)
        if "Петром" in user:
            return json.dumps({"is_event": True, "title": "Встреча с Петром",
                               "date": TOMORROW_ISO, "time": "14:00",
                               "end_time": "15:30", "all_day": False,
                               "recurrence": None, "confidence": 0.95,
                               "missing": []}, ensure_ascii=False)
        if "бухгалтерией" in user.lower():
            return json.dumps({"is_event": True, "title": "Обед с бухгалтерией",
                               "date": TOMORROW_ISO, "time": "16:00",
                               "duration_minutes": 60, "all_day": False,
                               "recurrence": None, "confidence": 0.95,
                               "missing": []}, ensure_ascii=False)
        if "Встреча с Иваном" in user:  # результат настоящего OCR (A9)
            return json.dumps({"is_event": True, "title": "Встреча с Иваном",
                               "date": TOMORROW_ISO, "time": "15:00",
                               "duration_minutes": 60, "all_day": False,
                               "recurrence": None, "confidence": 0.95,
                               "missing": []}, ensure_ascii=False)
        return json.dumps({"is_event": True, "title": "Событие",
                           "date": TOMORROW_ISO, "time": "12:00",
                           "duration_minutes": 60, "all_day": False,
                           "recurrence": None, "confidence": 0.9,
                           "missing": []}, ensure_ascii=False)

    async def close(self) -> None:
        pass


class RecSession(BaseSession):
    def __init__(self, photo_bytes: bytes = b"") -> None:
        super().__init__()
        self.calls = []
        self.photo_bytes = photo_bytes

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ == "GetFile":
            return File(file_id="f1", file_unique_id="u1", file_path="dummy")
        if type(method).__name__ == "SendMessage":
            return Message.model_validate({
                "message_id": 900000 + len(self.calls), "date": 1758800000,
                "chat": {"id": getattr(method, "chat_id", OWN), "type": "private"},
                "text": getattr(method, "text", "") or "",
            }, context={"bot": bot})
        return getattr(method, "__return_value__", True)

    async def stream_content(self, *a, **k):
        yield self.photo_bytes


N = 0


def _base_msg(uid: int) -> dict:
    return {"message_id": N + 1000, "date": 1758800000,
            "chat": {"id": uid, "type": "private"},
            "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"}}


def _msg(uid: int, text: str) -> Update:
    global N
    N += 1
    m = _base_msg(uid)
    m["text"] = text
    return Update.model_validate({"update_id": N, "message": m})


def _photo(uid: int, file_size: int = 100_000, caption: str = "") -> Update:
    global N
    N += 1
    m = _base_msg(uid)
    m["photo"] = [{"file_id": "ph1", "file_unique_id": "phu1",
                   "width": 800, "height": 600, "file_size": file_size}]
    if caption:
        m["caption"] = caption
    return Update.model_validate({"update_id": N, "message": m})


def _document(uid: int) -> Update:
    global N
    N += 1
    m = _base_msg(uid)
    m["document"] = {"file_id": "d1", "file_unique_id": "du1",
                     "file_name": "afisha.pdf", "mime_type": "application/pdf",
                     "file_size": 1000}
    return Update.model_validate({"update_id": N, "message": m})


def _cbq(uid: int, data: str) -> Update:
    global N
    N += 1
    return Update.model_validate({"update_id": N, "callback_query": {
        "id": f"cb-{N}", "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"},
        "chat_instance": "x", "data": data,
        "message": {"message_id": N + 5000, "date": 1758800000,
                    "chat": {"id": uid, "type": "private"},
                    "from": {"id": 777000, "is_bot": True, "first_name": "Bot"}}}})


def _png_sample() -> bytes | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    from io import BytesIO
    img = Image.new("RGB", (1000, 300), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 44)
    d.text((40, 40), "Встреча с Иваном завтра в 15:00", fill="black", font=font)
    d.text((40, 140), "Meeting with Ivan tomorrow 15:00", fill="black", font=font)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def pending(db) -> list:
    return await db.due_reminders(BIG_FUTURE)


async def main() -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    await db.connect()
    bot = Bot(cfg.bot_token, session=RecSession())
    llm = FakeLLM()
    dp = build_dispatcher(cfg, db, llm, None)
    original_recognize = ocr_mod.recognize_image
    ocr_calls = {"n": 0}

    async def feed(update: Update):
        n0 = len(bot.session.calls)
        await asyncio.wait_for(dp.feed_update(bot, update), timeout=30)
        return bot.session.calls[n0:]

    def texts(calls) -> list[str]:
        return [c.text or "" for c in calls if c.__class__.__name__ == "SendMessage"]

    def docs(calls) -> list:
        return [c for c in calls if c.__class__.__name__ == "SendDocument"]

    def preview_kb(calls):
        for c in reversed(calls):
            if c.__class__.__name__ == "SendMessage" and c.reply_markup:
                for row in c.reply_markup.inline_keyboard:
                    for b in row:
                        if b.callback_data in ("ev:create", "ev:create_all",
                                               "mg:yes"):
                            return b.callback_data
        return None

    try:
        # ================= A. OCR-конвейер (офлайн, заглушка) =================
        async def stub_ocr(image: bytes) -> str:
            ocr_calls["n"] += 1
            return "Встреча: планёрка"

        ocr_mod.recognize_image = stub_ocr
        calls = await feed(_photo(OWN, caption="с Иваном завтра в 15:00"))
        last_llm = llm.calls[-1] if llm.calls else ""
        check("A1a фото: OCR-текст + caption ушли в парсер",
              "Встреча: планёрка" in last_llm
              and "с Иваном завтра в 15:00" in last_llm, last_llm[:160])
        check("A1b фото: карточка предпросмотра с кнопкой Создать",
              preview_kb(calls) == "ev:create", str(texts(calls)))
        calls = await feed(_cbq(OWN, "ev:create"))
        check("A1c фото: подтверждение -> .ics отправлен",
              len(docs(calls)) == 1 and any(
                  "Google Calendar пока не настроен" in (d.caption or "")
                  for d in docs(calls)), str([d.document.filename for d in docs(calls)]))

        ocr_mod.recognize_image = lambda image: _empty_ocr(image)
        async def _empty_ocr(image: bytes) -> str:
            ocr_calls["n"] += 1
            return ""
        calls = await feed(_photo(OWN, caption="афиша"))
        check("A2 пустой OCR: вежливый отказ, без предпросмотра",
              any("не найден" in t for t in texts(calls))
              and preview_kb(calls) is None, str(texts(calls)))

        n_before = ocr_calls["n"]
        calls = await feed(_photo(OWN, file_size=6 * 1024 * 1024, caption="x"))
        check("A3 фото >5 МБ: отказ, OCR не вызывался",
              any("больше 5 МБ" in t for t in texts(calls))
              and ocr_calls["n"] == n_before, str(texts(calls)))

        calls = await feed(_document(OWN))
        check("A4 PDF/видео: отказ (не распознаются)",
              any("изображение как фото" in t.lower() for t in texts(calls)),
              str(texts(calls)))

        async def broken_ocr(image: bytes) -> str:
            raise ValueError("boom")
        ocr_mod.recognize_image = broken_ocr
        calls = await feed(_photo(OWN, caption="x"))
        check("A5 сбой OCR: вежливый отказ",
              any("OCR недоступен" in t for t in texts(calls)), str(texts(calls)))
        ocr_mod.recognize_image = original_recognize

        # Таймаут 20 с в recognize_image: медленный фейковый tesseract +
        # сжатие wait_for до 1 с (механизм проверяется, 20 с не ждём).
        slow_dir = Path(tempfile.mkdtemp(prefix="slow-tess-"))
        (slow_dir / "tesseract").write_text("#!/bin/sh\nsleep 30\n")
        (slow_dir / "tesseract").chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{slow_dir}{os.pathsep}{old_path}"
        real_wait = asyncio.wait_for

        async def quick_wait(aw, timeout=None, **kw):
            return await real_wait(aw, min(timeout, 1), **kw)

        asyncio.wait_for = quick_wait
        try:
            try:
                await ocr_mod.recognize_image(b"\x89PNG fake")
                timed_out = False
            except TimeoutError:
                timed_out = True
            finally:
                asyncio.wait_for = real_wait
        finally:
            os.environ["PATH"] = old_path
            shutil.rmtree(slow_dir, ignore_errors=True)
        check("A6 recognize_image: таймаут процесса tesseract (20 с) -> TimeoutError",
              timed_out)

        # Нет tesseract в PATH -> понятная ошибка
        bare_dir = Path(tempfile.mkdtemp(prefix="no-tess-"))
        os.environ["PATH"] = str(bare_dir)
        try:
            try:
                await ocr_mod.recognize_image(b"\x89PNG fake")
                missing = False
            except ValueError as e:
                missing = "OCR не установлен" in str(e)
        finally:
            os.environ["PATH"] = old_path
            shutil.rmtree(bare_dir, ignore_errors=True)
        check("A7 tesseract не установлен: ValueError с подсказкой", missing)

        # ================= A8/A9: НАСТОЯЩЕЕ распознавание (tesseract) =======
        have_tess = shutil.which("tesseract") is not None
        if not have_tess:
            skip("A8/A9 реальный OCR", "бинарник tesseract не найден в PATH")
        else:
            png = _png_sample()
            if png is None:
                skip("A8/A9 реальный OCR", "Pillow не установлен")
            else:
                recognized = await ocr_mod.recognize_image(png)
                check("A8 реальный OCR: рус+англ распознаны",
                      "Встреча с Иваном" in recognized
                      and "Meeting with Ivan" in recognized,
                      recognized[:200])
                # Сквозно через тот же диспетчер: фото -> реальный tesseract ->
                # предпросмотр -> .ics (сессия бота отдаёт реальные байты фото)
                old_session = bot.session
                bot.session = RecSession(photo_bytes=png)
                try:
                    calls = await feed(_photo(OWN, caption=""))
                finally:
                    bot.session = old_session
                last_llm = llm.calls[-1] if llm.calls else ""
                check("A9a фото->реальный OCR->парсер: текст подхватился",
                      "Встреча с Иваном" in last_llm, last_llm[:200])
                check("A9b фото->реальный OCR->карточка предпросмотра",
                      preview_kb(calls) == "ev:create", str(texts(calls)))
                calls = await feed(_cbq(OWN, "ev:create"))
                check("A9c фото->реальный OCR->.ics отправлен",
                      len(docs(calls)) == 1, str([d.document.filename for d in docs(calls)]))

        # ================= B. Платный шлюз OCR =================
        ocr_mod.recognize_image = stub_ocr
        n_before = ocr_calls["n"]
        calls = await feed(_photo(FREE, caption="афиша"))
        from bot.access import PAID_NOTICE
        check("B1 неплатному фото: PAID_NOTICE, OCR не вызывался",
              any(PAID_NOTICE in t for t in texts(calls))
              and ocr_calls["n"] == n_before
              and preview_kb(calls) is None, str(texts(calls)))
        ocr_mod.recognize_image = original_recognize

        # ================= C. Сквозные сценарии этапа 4 =================
        # C1: несколько событий из одного сообщения -> пакет .ics
        calls = await feed(_msg(OWN, "09:00 планёрка, а в 15:00 врач"))
        check("C1a два события: общий предпросмотр пакета",
              preview_kb(calls) == "ev:create_all"
              and any("Нашёл 2 события" in t for t in texts(calls)),
              str(texts(calls)))
        calls = await feed(_cbq(OWN, "ev:create_all"))
        evs = [e["title"] for e in await db.list_events()]
        check("C1b «Создать все»: пачка из 2 .ics + оба события в БД",
              len(docs(calls)) == 2
              and any("Создано 2 событий" in t for t in texts(calls))
              and "Планёрка" in evs and "Врач" in evs,
              f"{[d.document.filename for d in docs(calls)]} {evs}")

        # C2: повторяющееся событие -> RRULE в .ics + напоминания на вхождения
        calls = await feed(_msg(OWN, "каждую пятницу в 18:30 созвон"))
        check("C2a повтор: карточка предпросмотра",
              preview_kb(calls) == "ev:create", str(texts(calls)))
        calls = await feed(_cbq(OWN, "ev:create"))
        ics_text = docs(calls)[0].document.data.decode("utf-8") if docs(calls) else ""
        evs = await db.all_events()
        fr = [r for r in evs if r["title"] == "Созвон команды"]
        check("C2b .ics повторяющегося события: RRULE FREQ=WEEKLY;BYDAY=FR",
              "RRULE:FREQ=WEEKLY" in ics_text and "BYDAY=FR" in ics_text
              and fr and fr[0]["rrule"], ics_text[:300])
        pk_fr = fr[0]["id"] if fr else None
        rem_fr = [r for r in await pending(db) if r["event_pk"] == pk_fr]
        check("C2c напоминания запланированы на вхождения (event_pk сохранён)",
              len(rem_fr) >= 1, f"pk={pk_fr} n={len(rem_fr)}")

        # C3: /today и /tomorrow с повторяющимся событием. Первое вхождение
        # серии — ближайшее ≥ «сейчас» (критерий issue #4: «каждый день» — DAILY,
        # первое вхождение ≥ «сейчас»). «Каждый день в 09:00», созданный после
        # 09:00, начинается завтра — тогда он виден в /tomorrow, а не в /today.
        calls = await feed(_msg(OWN, "каждый день в 09:00 звонок"))
        await feed(_cbq(OWN, "ev:create"))
        daily = [r for r in await db.all_events() if r["title"] == "Ежедневный звонок"]
        target = "/today"
        if daily:
            target = "/today" if _msk(daily[0]["start_iso"]).date() == TODAY.date() else "/tomorrow"
        calls = await feed(_msg(OWN, target))
        check(f"C3a {target}: вхождение ежедневного события в свой день",
              any("Ежедневный звонок" in t and "🔁" in t for t in texts(calls)),
              f"{target} {texts(calls)}")
        calls = await feed(_msg(OWN, "/tomorrow"))
        check("C3b /tomorrow: следующее вхождение ежедневного события",
              any("Ежедневный звонок" in t for t in texts(calls)),
              str(texts(calls)))

        # C4: перенос текстом: время меняется, длительность сохраняется,
        # напоминание перепланируется (старое отменяется)
        calls = await feed(_msg(OWN, "встреча с Петром завтра с 14:00 до 15:30"))
        await feed(_cbq(OWN, "ev:create"))
        evs = await db.all_events()
        row = [r for r in evs if r["title"] == "Встреча с Петром"][0]
        pk_p = row["id"]
        old_start = row["start_iso"]
        calls = await feed(_msg(OWN, "перенеси встречу с Петром на 10:00"))
        # среди событий могут быть другие «встречи» — если бот спросил
        # «какое именно?», выбираем нужную по кнопке mg:pick
        kb = next((c.reply_markup for c in reversed(calls)
                   if c.__class__.__name__ == "SendMessage" and c.reply_markup),
                  None)
        pick = None
        if kb:
            for row_ in kb.inline_keyboard:
                for b in row_:
                    if (b.callback_data or "").startswith("mg:pick:") \
                            and "Петром" in (b.text or ""):
                        pick = b.callback_data
        if pick:
            calls = await feed(_cbq(OWN, pick))
        check("C4a перенос: карточка подтверждения с новым временем",
              preview_kb(calls) == "mg:yes"
              and any("Перенести" in t for t in texts(calls)), str(texts(calls)))
        calls = await feed(_cbq(OWN, "mg:yes"))
        evs = await db.all_events()
        row = [r for r in evs if r["id"] == pk_p][0]
        new_start = datetime.fromisoformat(row["start_iso"])
        new_end = datetime.fromisoformat(row["end_iso"])
        expect_start = (TOMORROW.replace(hour=10, minute=0, second=0,
                                         microsecond=0))
        check("C4b перенос: время обновлено, длительность 90 мин сохранена",
              new_start == expect_start
              and new_end == expect_start + timedelta(minutes=90),
              f"{row['start_iso']} .. {row['end_iso']}")
        rem = await pending(db)
        new_dt = datetime.fromisoformat(row["start_iso"])
        old_dt = datetime.fromisoformat(old_start)
        check("C4c перенос: старое напоминание отменено, новое запланировано",
              any(r["event_pk"] == pk_p
                  and datetime.fromisoformat(r["start_iso"]) == new_dt
                  for r in rem)
              and not any(datetime.fromisoformat(r["start_iso"]) == old_dt
                          and r["event_pk"] in (pk_p, None) for r in rem),
              str([(r["event_pk"], r["start_iso"], r["sent"]) for r in rem]))

        # C4d: перенос ПОВТОРЯЮЩЕГОСЯ события (только время): RRULE жив,
        # напоминания перепланированы на ВСЕ вхождения, а не на одно
        evs = await db.all_events()
        row_fr = [r for r in evs if r["id"] == pk_fr][0]
        old_fr_start = row_fr["start_iso"]
        calls = await feed(_msg(OWN, "перенеси созвон команды на 19:00"))
        kb = next((c.reply_markup for c in reversed(calls)
                   if c.__class__.__name__ == "SendMessage" and c.reply_markup),
                  None)
        pick = None
        if kb:
            for row_ in kb.inline_keyboard:
                for b in row_:
                    if (b.callback_data or "").startswith("mg:pick:") \
                            and "Пятничный" in (b.text or ""):
                        pick = b.callback_data
        if pick:
            calls = await feed(_cbq(OWN, pick))
        check("C4d1 перенос повтора: подтверждение",
              preview_kb(calls) == "mg:yes", str(texts(calls)))
        await feed(_cbq(OWN, "mg:yes"))
        evs = await db.all_events()
        row_fr = [r for r in evs if r["id"] == pk_fr][0]
        fr_new_start = datetime.fromisoformat(row_fr["start_iso"])
        fr_old_start = datetime.fromisoformat(old_fr_start)
        rem_fr_now = [r for r in await pending(db) if r["event_pk"] == pk_fr]
        check("C4d2 перенос повтора: время обновлено, RRULE сохранён",
              fr_new_start.date() == fr_old_start.date()
              and (fr_new_start.hour, fr_new_start.minute) == (19, 0)
              and row_fr["rrule"] == "FREQ=WEEKLY;BYDAY=FR",
              f"{row_fr['start_iso']} rrule={row_fr['rrule']}")
        # 8 напоминаний = 8 разных пятниц; у всех должно быть новое время 19:00
        # (МСК), и ни одного старого (18:30) не должно остаться
        all_new_time = len(rem_fr_now) >= 8 and all(
            _msk(r["start_iso"]).strftime("%H:%M") == "19:00" for r in rem_fr_now)
        no_old_left = not any(
            _msk(r["start_iso"]).strftime("%H:%M") == "18:30"
            and r["event_pk"] in (pk_fr, None) for r in await pending(db))
        check("C4d3 перенос повтора: напоминания на все вхождения, старые сняты",
              all_new_time and no_old_left,
              str([(r["event_pk"], r["start_iso"]) for r in rem_fr_now]))

        # C4e: перенос повтора на другой день недели — серия следует за датой
        sat = TOMORROW + timedelta(days=(5 - TOMORROW.weekday()) % 7)
        calls = await feed(_msg(
            OWN, f"перенеси созвон команды на {sat.strftime('%d.%m')} в 18:30"))
        kb = next((c.reply_markup for c in reversed(calls)
                   if c.__class__.__name__ == "SendMessage" and c.reply_markup),
                  None)
        pick = None
        if kb:
            for row_ in kb.inline_keyboard:
                for b in row_:
                    if (b.callback_data or "").startswith("mg:pick:") \
                            and "Пятничный" in (b.text or ""):
                        pick = b.callback_data
        if pick:
            calls = await feed(_cbq(OWN, pick))
        check("C4e1 перенос повтора на другую дату: подтверждение",
              preview_kb(calls) == "mg:yes", str(texts(calls)))
        await feed(_cbq(OWN, "mg:yes"))
        evs = await db.all_events()
        row_fr = [r for r in evs if r["id"] == pk_fr][0]
        sat_dt = datetime(sat.year, sat.month, sat.day, 18, 30,
                          tzinfo=ZoneInfo("Europe/Moscow"))
        rem_fr_now = [r for r in await pending(db) if r["event_pk"] == pk_fr]
        # серия перескочила на субботу: вхождение события = sat, RRULE BYDAY=SA,
        # все напоминания — по субботам в 18:30
        rem_saturdays = len(rem_fr_now) >= 1 and all(
            _msk(r["start_iso"]).weekday() == 5
            and _msk(r["start_iso"]).strftime("%H:%M") == "18:30"
            for r in rem_fr_now)
        check("C4e2 перенос повтора на субботу: событие, RRULE и напоминания "
              "следуют за новой датой",
              datetime.fromisoformat(row_fr["start_iso"]) == sat_dt
              and row_fr["rrule"] == "FREQ=WEEKLY;BYDAY=SA"
              and rem_saturdays,
              f"start={row_fr['start_iso']} rrule={row_fr['rrule']} "
              f"rem={[r['start_iso'] for r in rem_fr_now][:3]}")

        # C5: отмена текстом: событие deleted, напоминания отменены
        # (включая legacy-строки с event_pk=NULL от событий, созданных до фикса)
        calls = await feed(_msg(OWN, "обед с бухгалтерией завтра в 16:00"))
        await feed(_cbq(OWN, "ev:create"))
        evs = await db.all_events()
        row = [r for r in evs if r["title"] == "Обед с бухгалтерией"][0]
        pk_l = row["id"]
        # legacy-строка в том же формате, что и в проде (UTC, как пишет
        # schedule_for_event), только event_pk=None — как до фикса
        legacy_start = datetime.fromisoformat(row["start_iso"]) \
            .astimezone(timezone.utc)
        await db.add_reminder(
            event_pk=None, title="Обед с бухгалтерией",
            start_iso=legacy_start.isoformat(),
            remind_at=(legacy_start - timedelta(minutes=10)).isoformat(),
            minutes_before=10,
        )
        calls = await feed(_msg(OWN, "отмени обед с бухгалтерией"))
        check("C5a отмена: карточка подтверждения",
              preview_kb(calls) == "mg:yes"
              and any("Отменить" in t for t in texts(calls)), str(texts(calls)))
        calls = await feed(_cbq(OWN, "mg:yes"))
        evs = [e["title"] for e in await db.all_events()]
        rem = await pending(db)
        ev_start_dt = datetime.fromisoformat(row["start_iso"])
        legacy_left = [r for r in rem
                       if r["event_pk"] is None
                       and datetime.fromisoformat(r["start_iso"]) == ev_start_dt]
        pk_left = [r for r in rem if r["event_pk"] == pk_l]
        check("C5b отмена: «🗑 Отменил», событие из истории исчезло",
              any("Отменил" in t for t in texts(calls))
              and "Обед с бухгалтерией" not in evs, str(texts(calls)))
        check("C5c отмена: напоминания отменены (включая legacy event_pk=NULL)",
              not legacy_left and not pk_left,
              str([(r["event_pk"], r["start_iso"]) for r in legacy_left + pk_left]))

        # ================= результат =================
        print(f"\nИтог: {passed} ок, {failed} провалено, {skipped} пропущено")
    finally:
        ocr_mod.recognize_image = original_recognize
        await bot.session.close()
        await db.close()
        await llm.close()

    if failed:
        raise SystemExit(1)


async def feed2(bot2, update):  # заглушка для читаемости (не используется)
    return []


if __name__ == "__main__":
    asyncio.run(main())

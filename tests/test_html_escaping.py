"""Экранирование пользовательских строк в HTML-ответах (issue #8).

Заголовок события (из текста/LLM/OCR) и другие пользовательские строки
(транскрипт ASR, данные callback) не должны попадать в Telegram-HTML в сыром
виде: несбалансированные теги ломают parse (400 Bad Request), а валидные —
дают сам-XSS. Проверка через реальный диспетчер (build_dispatcher).

Запуск: python tests/test_html_escaping.py   (сеть не нужна)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ALLOWED_USER_ID", "100")
os.environ.setdefault("ALLOWED_USER_IDS", "100")
os.environ.setdefault("PAID_USER_IDS", "100")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ai-secretary-esc-"))

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import File, Message, Update  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

UID = 100
RAW_TITLE = "обед <b>команда</b> & офис"
ESC_TITLE = "обед &lt;b&gt;команда&lt;/b&gt; &amp; офис"
RAW_TRANSCRIPT = "напомнить <маме> & <папе> позвонить"
ESC_TRANSCRIPT = "напомнить &lt;маме&gt; &amp; &lt;папе&gt; позвонить"

# «Сегодня» по таймзоне бота (Europe/Moscow), чтобы /today показывал событие.
TODAY = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")
TIME_TODAY = "12:00"


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, system: str, user: str, **kw) -> str:
        self.calls.append(user)
        if "двух встреч" in user:
            return json.dumps({"events": [
                {"title": RAW_TITLE, "date": TODAY, "time": "10:00",
                 "duration_minutes": 30, "all_day": False, "recurrence": None,
                 "confidence": 0.9, "missing": []},
                {"title": "чистая вторая", "date": TODAY, "time": "15:00",
                 "duration_minutes": 30, "all_day": False, "recurrence": None,
                 "confidence": 0.9, "missing": []},
            ]}, ensure_ascii=False)
        return json.dumps({
            "is_event": True, "title": RAW_TITLE, "date": TODAY, "time": TIME_TODAY,
            "end_time": None, "duration_minutes": 60, "all_day": False,
            "location": None, "participants": None, "description": None,
            "recurrence": None, "confidence": 0.95, "missing": [],
        }, ensure_ascii=False)

    async def transcribe(self, data: bytes, **kw) -> str:
        return RAW_TRANSCRIPT

    async def close(self) -> None:
        pass


class RecSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ == "GetFile":
            return File(file_id="f1", file_unique_id="u1", file_path="dummy")
        if type(method).__name__ == "SendMessage":
            # Реальный Message нужен голосовому потоку (note = await msg.answer())
            return Message.model_validate({
                "message_id": 900000 + len(self.calls), "date": 1758800000,
                "chat": {"id": getattr(method, "chat_id", UID), "type": "private"},
                "text": getattr(method, "text", "") or "",
            }, context={"bot": bot})
        return getattr(method, "__return_value__", True)

    async def stream_content(self, *a, **k):
        yield b""


N = 0


def _msg(uid: int, text: str = "", voice: bool = False) -> Update:
    global N
    N += 1
    m: dict = {"message_id": N + 1000, "date": 1758800000,
               "chat": {"id": uid, "type": "private"},
               "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"}}
    if voice:
        m["voice"] = {"file_id": "v1", "file_unique_id": "vu1", "duration": 5,
                      "mime_type": "audio/ogg", "file_size": 1000}
    else:
        m["text"] = text
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


async def main() -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    await db.connect()
    bot = Bot(cfg.bot_token, session=RecSession())
    llm = FakeLLM()
    dp = build_dispatcher(cfg, db, llm, None)
    results: list[tuple[str, bool, str]] = []

    try:
        async def feed(update: Update):
            n0 = len(bot.session.calls)
            await asyncio.wait_for(dp.feed_update(bot, update), timeout=15)
            return bot.session.calls[n0:]

        def msgs(calls) -> list[str]:
            return [c.text or "" for c in calls if c.__class__.__name__ == "SendMessage"]

        def doc_captions(calls) -> list[str]:
            return [c.caption or "" for c in calls
                    if c.__class__.__name__ == "SendDocument"]

        def check(name: str, cond: bool, extra: str = "") -> None:
            results.append((name, bool(cond), extra))
            print(f"{'✅' if cond else '❌'} {name}"
                  + (f"  -> {extra}" if not cond else ""))

        def no_raw(*texts: str) -> bool:
            return all(RAW_TITLE not in t and RAW_TRANSCRIPT not in t
                       for t in texts)

        # 1. Предпросмотр (эталон — preview_text экранирует сам)
        res = await feed(_msg(UID, "тест"))
        t = msgs(res)
        check("1.1 предпросмотр: заголовок экранирован",
              t and ESC_TITLE in t[0], str(t))
        check("1.2 предпросмотр: сырые теги отсутствуют", no_raw(*t))

        # 2. Создание -> .ics: подпись и имя файла документа
        res = await feed(_cbq(UID, "ev:create"))
        cap = doc_captions(res)
        fname = next((c.document.filename for c in res
                      if c.__class__.__name__ == "SendDocument"), "")
        check("2.1 caption .ics: сырые теги отсутствуют", no_raw(*cap), str(cap))
        check("2.2 имя файла .ics: разметка не в имени",
              "<" not in fname and ">" not in fname, fname)

        # 3. /list
        res = msgs(await feed(_msg(UID, "/list")))
        check("3.1 /list: заголовок экранирован", res and ESC_TITLE in res[0], str(res))
        check("3.2 /list: сырые теги отсутствуют", no_raw(*res))

        # 4. /today
        res = msgs(await feed(_msg(UID, "/today")))
        check("4.1 /today: заголовок экранирован",
              res and ESC_TITLE in res[0], str(res))
        check("4.2 /today: сырые теги отсутствуют", no_raw(*res))

        # 5. Отмена текстом: подтверждение и результат
        res = msgs(await feed(_msg(UID, "отмени обед")))
        check("5.1 подтверждение отмены: заголовок экранирован",
              res and ESC_TITLE in res[0] and "Отменить" in res[0], str(res))
        check("5.2 подтверждение отмены: сырые теги отсутствуют", no_raw(*res))
        res = msgs(await feed(_cbq(UID, "mg:yes")))
        check("5.3 результат отмены: заголовок экранирован",
              res and ESC_TITLE in res[-1], str(res))
        check("5.4 результат отмены: сырые теги отсутствуют", no_raw(*res))

        # 6. Перенос текстом: подтверждение и результат
        await feed(_msg(UID, "тест"))
        await feed(_cbq(UID, "ev:create"))
        res = msgs(await feed(_msg(UID, "перенеси обед завтра в 10:00")))
        check("6.1 подтверждение переноса: заголовок экранирован",
              res and ESC_TITLE in res[0] and "Перенести" in res[0], str(res))
        check("6.2 подтверждение переноса: сырые теги отсутствуют", no_raw(*res))
        res = msgs(await feed(_cbq(UID, "mg:yes")))
        check("6.3 результат переноса: заголовок экранирован",
              res and ESC_TITLE in res[-1], str(res))
        check("6.4 результат переноса: сырые теги отсутствуют", no_raw(*res))

        # 7. /undo
        res = msgs(await feed(_msg(UID, "/undo")))
        check("7.1 /undo: заголовок экранирован", res and ESC_TITLE in res[0], str(res))
        check("7.2 /undo: сырые теги отсутствуют", no_raw(*res))

        # 8. Мульти-предпросмотр: заголовки в блоках
        res = msgs(await feed(_msg(UID, "у меня двух встреч завтра")))
        check("8.1 мульти-предпросмотр: заголовок экранирован",
              res and ESC_TITLE in res[0], str(res))
        check("8.2 мульти-предпросмотр: сырые теги отсутствуют", no_raw(*res))

        # 9. Голос: транскрипт в предпросмотре
        res = msgs(await feed(_msg(UID, voice=True)))
        check("9.1 транскрипт: экранирован", res and ESC_TRANSCRIPT in res[0], str(res))
        check("9.2 транскрипт: сырые теги отсутствуют", no_raw(*res))

        # 10. Поддельный callback: tz с разметкой
        res = msgs(await feed(_cbq(UID, "st:tzset:Europe/Moscow<b>x</b>")))
        tz_saved = await db.get_setting("timezone", "")
        check("10.1 st:tzset с разметкой: не сохранён в настройках",
              tz_saved == "", tz_saved)
        check("10.2 st:tzset с разметкой: сырые теги не в ответе",
              all("<b>x</b>" not in t for t in res), str(res))

        # 11. Поддельный callback: интервал напоминаний с разметкой
        res = msgs(await feed(_cbq(UID, "st:remset:10<b>y</b>")))
        rem_saved = await db.get_setting("reminders", "")
        check("11.1 st:remset с разметкой: не сохранён в настройках",
              rem_saved == "", rem_saved)
        check("11.2 st:remset с разметкой: сырые теги не в ответе",
              all("<b>y</b>" not in t for t in res), str(res))
    finally:
        await bot.session.close()
        await db.close()

    failed = [r for r in results if not r[1]]
    print(f"\nИтог: {len(results) - len(failed)}/{len(results)} ок"
          + (f", провалено: {[r[0] for r in failed]}" if failed else ""))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

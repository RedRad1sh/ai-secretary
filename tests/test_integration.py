"""Интеграционный тест: фейковый Telegram-апдейт прогоняется через весь
диспетчер (GigaChat заглушен, Google Calendar не настроен -> .ics fallback).

Запуск: python tests/test_integration.py   (сеть не нужна)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ALLOWED_USER_ID", "555000111")
os.environ.setdefault("PAID_USER_IDS", "555000111")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ai-secretary-test-"))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import Update  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

CANNED = json.dumps({
    "is_event": True, "title": "Встреча с Иваном", "date": "2026-10-15", "time": "14:00",
    "end_time": None, "duration_minutes": 60, "all_day": False,
    "location": "офис на Ленина", "participants": "Иван", "description": None,
    "recurrence": None, "confidence": 0.95, "missing": [],
}, ensure_ascii=False)


class FakeGiga:
    async def chat(self, system: str, user: str, **kw) -> str:  # noqa: D102
        return CANNED


class RecordingSession(BaseSession):
    """Ловит все исходящие вызовы Telegram API вместо реальной сети."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return getattr(method, "__return_value__", True)

    async def stream_content(self, *a, **k):
        yield b""


def _msg(update_id: int, user_id: int, text: str) -> Update:
    return Update.model_validate({"update_id": update_id, "message": {
        "message_id": update_id + 9, "date": 1758800000,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "U"},
        "text": text,
    }})


def _cb(update_id: int, user_id: int, data: str) -> Update:
    return Update.model_validate({"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "from": {"id": user_id, "is_bot": False, "first_name": "U"},
        "chat_instance": "x", "data": data,
        "message": {"message_id": update_id + 9, "date": 1758800000,
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": 777000, "is_bot": True, "first_name": "Bot"}},
    }})


async def main() -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    try:
        await db.connect()
        bot = Bot(cfg.bot_token, session=RecordingSession())
        # сборка тем же кодом, что и в проде: ловит рассинхрон импортов
        dp = build_dispatcher(cfg, db, FakeGiga(), None)

        uid = 555000111

        await dp.feed_update(bot, _msg(1, uid, "встреча с Иваном 15 октября в 14:00 в офисе на Ленина"))
        preview = next(c for c in bot.session.calls if c.__class__.__name__ == "SendMessage")
        assert "Встреча с Иваном" in preview.text and "офис на Ленина" in preview.text
        assert preview.reply_markup is not None, "нет inline-кнопок"
        print("1) предпросмотр с кнопками — OK")

        await dp.feed_update(bot, _cb(2, uid, "ev:create"))
        assert "SendDocument" in [c.__class__.__name__ for c in bot.session.calls], "нет .ics"
        print("2) .ics-fallback при ненастроенном Google — OK")

        n = len(bot.session.calls)
        await dp.feed_update(bot, _msg(3, 999, "встреча завтра"))
        assert len(bot.session.calls) == n, "чужой не проигнорирован"
        print("3) белый список Telegram ID — OK")

        await dp.feed_update(bot, _msg(4, uid, "/list"))
        last = bot.session.calls[-1]
        assert last.__class__.__name__ == "SendMessage" and "Встреча" in last.text
        print("4) /list — OK")

        await dp.feed_update(bot, _msg(5, uid, "/undo"))
        assert "убрано из истории" in bot.session.calls[-1].text
        assert not await db.list_events()
        print("5) /undo — OK")

        # v2: /today на пустой день
        await dp.feed_update(bot, _msg(6, uid, "/today"))
        last = bot.session.calls[-1]
        assert last.__class__.__name__ == "SendMessage" and "Событий нет" in last.text
        print("6) /today (пусто) — OK")

        # v2: создание, затем отмена текстом + подтверждение кнопкой
        await dp.feed_update(bot, _msg(7, uid, "созвон с командой завтра в 11:00"))
        await dp.feed_update(bot, _cb(8, uid, "ev:create"))
        assert await db.list_events(), "событие не создалось"

        await dp.feed_update(bot, _msg(9, uid, "отмени встречу"))
        confirms = [c for c in bot.session.calls
                    if c.__class__.__name__ == "SendMessage" and "Отменить" in (c.text or "")]
        assert confirms, "не предложено подтверждение отмены"
        await dp.feed_update(bot, _cb(10, uid, "mg:yes"))
        assert not await db.list_events(), "событие не отменено"
        print("7) «отмени встречу» + кнопка — OK")

        await dp.feed_update(bot, _msg(11, uid, "/stats"))
        last = bot.session.calls[-1]
        assert last.__class__.__name__ == "SendMessage" and "Статистика" in last.text
        print("8) /stats — OK")

        # serverless-адаптер обязан собираться тем же build_dispatcher:
        # статическая проверка консистентности (динамику уже проверили выше)
        yh_src = (Path(__file__).resolve().parent.parent
                  / "serverless" / "yandex_handler.py").read_text(encoding="utf-8")
        assert "from bot.main import build_dispatcher" in yh_src
        assert "_dp = build_dispatcher(" in yh_src
        assert "include_router" not in yh_src  # ручной сборки быть не должно
        main_src = (Path(__file__).resolve().parent.parent / "bot" / "main.py").read_text(
            encoding="utf-8")
        assert "from bot.handlers import commands, manage, parse_flow" in main_src
        print("9) serverless использует build_dispatcher (консистентно) — OK")

        print("\nALL INTEGRATION CHECKS PASSED")
        await bot.session.close()
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Точка входа: long polling (ТЗ §5). Запуск: python -m bot.main"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import get_config
from bot.db import Database
from bot.handlers import commands, manage, parse_flow
from bot import reminders
from bot.services.gcal import GCalClient
from bot.services.llm import create_llm_client


def setup_logging(cfg) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    # не дублировать хендлеры при повторном вызове (тесты / reload)
    if root.handlers:
        root.handlers.clear()
    root.setLevel(cfg.log_level)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.handlers.RotatingFileHandler(
        cfg.data_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # сторонние библиотеки шумят на INFO (каждый HTTP-запрос) — глушим до WARNING,
    # кроме ошибок; DEBUG включайте через LOG_LEVEL=DEBUG
    if cfg.log_level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("aiogram.event").setLevel(logging.INFO)


def build_dispatcher(cfg, db, llm, gcal) -> "Dispatcher":
    """Собирает Dispatcher со всеми роутерами и зависимостями.

    Единая точка сборки: используется в main(), serverless-адаптере и тестах —
    рассинхрон импортов/роутеров ловится тестами, а не на проде.
    """
    dp = Dispatcher(storage=MemoryStorage())

    # зависимости, доступные хендлерам как именованные аргументы
    dp["cfg"] = cfg
    dp["db"] = db
    dp["llm"] = llm
    dp["gcal"] = gcal

    dp.include_router(commands.router)
    dp.include_router(manage.router)
    dp.include_router(parse_flow.router)
    return dp


async def main() -> None:
    cfg = get_config()
    setup_logging(cfg)
    log = logging.getLogger("bot")

    db = Database(cfg.db_path)
    await db.connect()

    llm = create_llm_client(cfg)
    gcal = GCalClient(cfg.google_client_id, cfg.google_client_secret,
                      cfg.google_refresh_token) if cfg.google_ready else None

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(cfg, db, llm, gcal)

    @dp.errors()
    async def on_error(event):  # aiogram 3: event is ErrorEvent
        # ErrorEvent has .exception and .update
        exc = getattr(event, "exception", None) or getattr(event, "error", None) or event
        log.exception("Unhandled error: %s", exc, exc_info=exc if isinstance(exc, BaseException) else None)
        try:
            # пробуем ответить в тот же чат
            upd = getattr(event, "update", None)
            msg = None
            if upd is not None:
                msg = getattr(upd, "message", None) or getattr(upd, "callback_query", None)
                if msg is not None and hasattr(msg, "message"):
                    msg = msg.message  # callback_query.message
            if msg is not None and hasattr(msg, "answer"):
                await msg.answer("😅 Внутренняя ошибка. Попробуйте ещё раз.")
            elif hasattr(event, "message") and getattr(event, "message", None):
                await event.message.answer("😅 Внутренняя ошибка. Попробуйте ещё раз.")
        except Exception:  # noqa: S110
            pass
        return True

    log.info("Бот запущен. AI: %s. Google Calendar: %s",
             type(llm).__name__ if llm else "не настроен (офлайн-парсер)",
             "подключён" if gcal else "НЕ настроен (режим .ics)")
    if cfg.allowed_user_id is None:
        log.warning("ALLOWED_USER_ID не задан — бот будет отвечать всем! Задайте его в .env")

    async def reminder_loop() -> None:
        """Каждые 30 секунд проверяет созревшие напоминания и шлёт их в Telegram."""
        while True:
            try:
                await reminders.sweep(bot, db, cfg)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                log.exception("reminder sweep failed")
            await asyncio.sleep(30)

    sweep_task = asyncio.create_task(reminder_loop())
    log.info("Планировщик напоминаний запущен (каждые 30 с)")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        sweep_task.cancel()
        if llm is not None:
            await llm.close()
        await db.close()
        await bot.session.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

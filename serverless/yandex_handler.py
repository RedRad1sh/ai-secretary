"""Адаптер Yandex Cloud Functions — бесплатный хостинг (0 ₽/мес).

Бесплатные лимиты YC: 1 000 000 вызовов функций и 10 GB×час в месяц — личному
боту хватает с запасом. Схема: Telegram webhook -> публичный URL функции -> эта
функция -> aiogram Dispatcher.feed_update -> GigaChat + Google Calendar.

Развёртывание: см. docs/DEPLOY_YC.md

Опционально: если задать переменную окружения WEBHOOK_SECRET и установить
вебхук с тем же секретом (setWebhook?secret_token=...), функция будет
отвечать 403 на запросы не от Telegram.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

log = logging.getLogger(__name__)

_bot = None
_dp = None
_loop = None
_cfg = None
_db = None


def _init() -> None:
    global _bot, _dp, _loop, _cfg, _db
    if _bot is not None:
        return

    import logging as _logging

    _logging.basicConfig(level=_logging.INFO)  # чтобы INFO-логи были видны в консоли YC

    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.config import load_config
    from bot import reminders
    from bot.db import Database
    from bot.main import build_dispatcher
    from bot.services.llm import create_llm_client

    # В функции вместо .env читаем переменные окружения напрямую
    os.environ.setdefault("DATA_DIR", "/tmp/data")
    cfg = load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    _cfg = cfg

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    db = Database(cfg.db_path)
    _loop.run_until_complete(db.connect())
    _db = db

    _bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _dp = build_dispatcher(
        cfg, db, create_llm_client(cfg),
        None,  # OAuth отложен; .ics для всех пользователей
    )
    log.info(
        "Бот инициализирован. ALLOWED_USER_ID=%s, LLM=%s (model=%s), Google=%s",
        cfg.allowed_user_id,
        cfg.llm_api_url or ("нативный GigaChat" if cfg.giga_api_key else "OFF (офлайн-парсер)"),
        cfg.llm_model if cfg.llm_api_url else cfg.giga_model,
        "отложен (.ics)",
    )


_cfg = None


def _extract_from_id(data: dict) -> int | None:
    """ID отправителя из сырого апдейта (для диагностического лога)."""
    for key in ("message", "edited_message", "callback_query"):
        node = data.get(key)
        if isinstance(node, dict):
            frm = node.get("from") or (node.get("chat") or {})
            return frm.get("id")
    return None


def _parse_body(event) -> dict:
    """Достаёт JSON апдейта из тела запроса (учитывая base64-вариант YC)."""
    body = event.get("body")
    if not body:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode("utf-8")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _secret_ok(event) -> bool:
    """Если WEBHOOK_SECRET задан — пускаем только запросы с ним (от Telegram)."""
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        return True
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    return headers.get("x-telegram-bot-api-secret-token") == secret


async def _sweep_reminders() -> None:
    """Отправляет созревшие напоминания (ошибки не прерывают остальные)."""
    try:
        from bot import reminders

        n = await reminders.sweep_all(_bot, _db, _cfg)
        if n:
            log.info("Отправлено напоминаний: %d", n)
    except Exception:  # noqa: BLE001
        log.exception("reminder sweep failed")


def handler(event, context):
    """Точка входа Yandex Cloud Functions.

    Принимает: (1) HTTP-запрос Telegram (webhook), (2) вызов Timer-триггера YC
    (напоминания), (3) прочие запросы без тела — проверка живости.
    """
    _init()

    try:
        has_body = bool((event or {}).get("body"))

        # Timer-триггер YC присылает event с "messages" и без "body"
        if not has_body and (event or {}).get("messages"):
            _loop.run_until_complete(_sweep_reminders())
            return {"statusCode": 200, "body": "reminders swept"}

        if has_body and not _secret_ok(event):
            return {"statusCode": 403, "body": "forbidden"}

        data = _parse_body(event)
        if "update_id" not in data:
            # Открытие ссылки в браузере / ручной вызов без тела Telegram —
            # это не ошибка, просто отвечаем бодро и не пачкаем логи.
            log.info("Запрос без апдейта Telegram (проверка живости) — OK")
            return {"statusCode": 200, "body": "Bot is alive"}

        from aiogram.types import Update

        log.info(
            "Апдейт Telegram: update_id=%s from=%s ожидаемый_ID=%s",
            data.get("update_id"), _extract_from_id(data),
            _cfg.allowed_user_id if _cfg else "?",
        )
        _loop.run_until_complete(
            _dp.feed_update(_bot, Update.model_validate(data))
        )
        # попутный прогон напоминаний при любой активности
        _loop.run_until_complete(_sweep_reminders())
        log.info("Апдейт %s обработан", data.get("update_id"))
        return {"statusCode": 200, "body": "OK"}
    except Exception as e:  # noqa: BLE001
        log.exception("handler failed: %s", e)
        return {"statusCode": 200, "body": "ERR"}  # Telegram сделает retry

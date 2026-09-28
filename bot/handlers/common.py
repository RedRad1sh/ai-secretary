"""Общие фильтры и зависимости для хендлеров."""

from __future__ import annotations

import html

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from bot.config import Config


def esc(value: object) -> str:
    """Экранирует строку для Telegram HTML (parse_mode=HTML).

    Пользовательские строки (заголовки событий из текста/LLM/OCR, транскрипты,
    данные callback) не должны попадать в HTML в сыром виде: несбалансированные
    теги ломают парсинг (400 Bad Request), валидные — дают сам-XSS.
    """
    return html.escape(str(value if value is not None else ""))


class OwnerFilter(BaseFilter):
    """ТЗ §3.4: бот реагирует только на сообщения авторизованного пользователя."""

    async def __call__(self, event: TelegramObject, cfg: Config) -> bool:
        user = event.from_user
        if user is None:
            return False
        return cfg.is_allowed(user.id)


async def get_tz_name(db, cfg: Config | None) -> str:
    """Таймзона пользователя: из настроек в БД или из конфига."""
    default = cfg.default_tz if cfg else "Europe/Moscow"
    return await db.get_setting("timezone", default)


async def get_calendar_id(db, cfg: Config | None) -> str:
    default = cfg.default_calendar_id if cfg else "primary"
    return await db.get_setting("calendar_id", default)

"""Общие фильтры и зависимости для хендлеров."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from bot.config import Config


class OwnerFilter(BaseFilter):
    """ТЗ §3.4: бот реагирует только на сообщения авторизованного пользователя."""

    async def __call__(self, event: TelegramObject, cfg: Config) -> bool:
        user = event.from_user
        if user is None:
            return False
        return cfg.allowed_user_id is None or user.id == cfg.allowed_user_id


async def get_tz_name(db, cfg: Config | None) -> str:
    """Таймзона пользователя: из настроек в БД или из конфига."""
    default = cfg.default_tz if cfg else "Europe/Moscow"
    return await db.get_setting("timezone", default)


async def get_calendar_id(db, cfg: Config | None) -> str:
    default = cfg.default_calendar_id if cfg else "primary"
    return await db.get_setting("calendar_id", default)

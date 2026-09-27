"""Access policy and per-user SQLite isolation, shared by polling and webhook."""
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery
from bot.db import Database

PAID_NOTICE = "🔒 Эта функция доступна платному списку. Оплата пока не подключена; доступ выдаёт владелец."


def user_db_path(cfg, user_id):
    # Existing single-user database remains owned ONLY by the legacy owner.
    if user_id == cfg.allowed_user_id:
        return cfg.db_path
    return cfg.db_path.parent / "users" / f"{user_id}.db"


class UserAccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        cfg = data["cfg"]
        user = event.from_user
        if not user or not cfg.is_allowed(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return
        message = event.message if isinstance(event, CallbackQuery) else event
        if not message or message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Откройте личный чат с ботом", show_alert=True)
            return
        if not cfg.is_paid(user.id):
            from bot.handlers.manage import parse_manage_intent
            text = getattr(event, "text", "") or ""
            command = text.split(maxsplit=1)[0].split("@")[0] if text else ""
            blocked = command in {"/today", "/tomorrow", "/stats"} or bool(parse_manage_intent(text))
            blocked |= bool(getattr(event, "photo", None))
            if isinstance(event, CallbackQuery):
                blocked |= (event.data or "").startswith("mg:")
                state = data.get("state")
                stored = await state.get_data() if state else {}
                drafts = stored.get("drafts") or [stored.get("draft")]
                if (event.data or "").startswith("ev:create"):
                    blocked |= len(drafts) > 1 or any(d and d.recurrence for d in drafts)
            if blocked:
                await event.answer(PAID_NOTICE, **({"show_alert": True} if isinstance(event, CallbackQuery) else {}))
                return
        # No shared owner OAuth credentials for other users.
        data["gcal"] = None
        if user.id == cfg.allowed_user_id:
            return await handler(event, data)
        db = Database(user_db_path(cfg, user.id))
        await db.connect()
        try:
            data["db"] = db
            return await handler(event, data)
        finally:
            await db.close()

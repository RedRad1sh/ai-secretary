#!/usr/bin/env python3
"""Установка/снятие webhook Telegram (для serverless/VPS-режима).

Установить:   python scripts/set_webhook.py https://<url-функции>
Проверить:    python scripts/set_webhook.py --info
Снять (нужно для long polling): python scripts/set_webhook.py --remove
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402

from bot.config import get_config  # noqa: E402


async def main() -> None:
    cfg = get_config()
    bot = Bot(cfg.bot_token)
    arg = sys.argv[1] if len(sys.argv) > 1 else "--info"

    if arg == "--remove":
        print(await bot.delete_webhook(drop_pending_updates=False))
    elif arg == "--info":
        print(await bot.get_webhook_info())
    else:
        print(await bot.set_webhook(arg, allowed_updates=["message", "callback_query"]))
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

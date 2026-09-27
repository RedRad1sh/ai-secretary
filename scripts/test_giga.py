#!/usr/bin/env python3
"""Быстрая проверка GigaChat без бота: авторизация + пробный запрос.

Запуск из корня проекта:  python scripts/test_giga.py
Читает .env (GIGACHAT_API_KEY, GIGACHAT_SCOPE, GIGACHAT_MODEL, GIGACHAT_CA_CERT).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config  # noqa: E402
from bot.services.giga import GigaChatClient, GigaChatError  # noqa: E402


async def main() -> None:
    cfg = load_config()
    if not cfg.giga_api_key:
        print("❌ GIGACHAT_API_KEY не задан в .env")
        sys.exit(1)

    key = cfg.giga_api_key.strip()
    print(f"scope={cfg.giga_scope}, model={cfg.giga_model}, verify_tls={cfg.giga_verify_tls}")
    print(f"ключ: длина={len(key)}, начало={key[:8]}..., конец=...{key[-4:]}")
    if " " in key or "\t" in key:
        print("⚠️ В ключе есть пробелы — проверьте, что скопировали одну строку без переносов")
    if key.lower().startswith("basic"):
        print("⚠️ Ключ содержит слово «Basic» — в .env должно быть только само значение "
              "(без слова Basic, без кавычек)")
    if not key.rstrip("="):
        print("⚠️ Ключ не похож на base64 (нет завершающих «=»?) — сверьте в кабинете")

    giga = GigaChatClient(cfg)
    try:
        await giga._authorize()
        print("✅ Авторизация OK (access-токен получен)")
        answer = await giga.chat("Ты ассистент. Отвечай кратко.", "Скажи «работаю»")
        print(f"✅ chat/completions OK, ответ модели: {answer!r}")
        print("\nВсё работает — GigaChat подключён корректно.")
    except GigaChatError as e:
        print(f"❌ {e}")
        sys.exit(1)
    finally:
        await giga.close()


if __name__ == "__main__":
    asyncio.run(main())

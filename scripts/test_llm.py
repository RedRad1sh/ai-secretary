#!/usr/bin/env python3
"""Проверка AI-провайдера без бота.

Работает для любого OpenAI-совместимого API (LLM_API_URL + LLM_API_KEY +
LLM_MODEL) и для нативного GigaChat. Запуск: python scripts/test_llm.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config  # noqa: E402
from bot.services.llm import LLMError, create_llm_client  # noqa: E402


async def main() -> None:
    cfg = load_config()
    if cfg.llm_api_url:
        print(f"Режим: OpenAI-совместимый\n  url:   {cfg.llm_api_url}\n"
              f"  model: {cfg.llm_model}\n  key:   "
              f"{cfg.llm_api_key[:6] + '...' if cfg.llm_api_key else '(нет — как для Ollama)'}")
    elif cfg.giga_api_key:
        print("Режим: нативный GigaChat (OAuth)")
    else:
        print("❌ Не задан ни LLM_API_URL, ни GIGACHAT_API_KEY (см. .env.example)")
        sys.exit(1)

    llm = create_llm_client(cfg)
    try:
        answer = await llm.chat("Ты ассистент. Отвечай кратко.", "Скажи «работаю»")
        print(f"✅ chat/completions OK, ответ: {answer!r}")
    except LLMError as e:
        print(f"❌ {e}")
        sys.exit(1)
    finally:
        await llm.close() if llm is not None else None

    print("\nВсё работает — AI подключён.")


if __name__ == "__main__":
    asyncio.run(main())

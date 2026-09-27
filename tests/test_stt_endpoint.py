"""Маршрутизация STT-эндпоинта (issue #12): STT_API_* vs фолбэк на LLM_*.

Контекст: боевой бот держит LLM на бесплатных моделях OpenRouter, а аудио у
этого провайдера только платное → голос упирался в 402 «requires at least
$0.50 in balance for audio». Фикс: переменные STT_API_URL / STT_API_KEY /
STT_MODEL (напр. на бесплатный Groq whisper-large-v3-turbo), фолбэк на
значения LLM_* по каждой позиции, если STT_* пустые.

Офлайн: httpx.MockTransport (fake-транспорт, без сети).
Запуск: python tests/test_stt_endpoint.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.services.llm import LLMError, OpenAICompatClient  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ {name}")
    else:
        failed += 1
        print(f"❌ {name} {extra}")


def make_cfg(**over) -> Config:
    d = dict(
        llm_api_url="https://llm.example/v1",
        llm_api_key="llm-key",
        llm_model="some/model:free",
        llm_voice_model="whisper-1",
        llm_stt_enabled=True,
    )
    d.update(over)
    return Config(**d)


class Recorder:
    """Fake-транспорт: фиксирует запросы, отвечает подготовленным HTTP-статусом."""

    def __init__(self, status: int = 200, payload: dict | None = None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.payload = payload if payload is not None else {"text": "привет, мир"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)


def client(cfg: Config, rec: Recorder) -> OpenAICompatClient:
    return OpenAICompatClient(cfg, transport=httpx.MockTransport(rec.handler))


async def run_transcribe(cfg: Config, rec: Recorder) -> str:
    c = client(cfg, rec)
    try:
        return await c.transcribe(b"fake-ogg-bytes")
    finally:
        await c.close()


async def run_chat(cfg: Config, rec: Recorder) -> str:
    rec.payload = {"choices": [{"message": {"content": '{"ok": true}'}}]}
    c = client(cfg, rec)
    try:
        return await c.chat("system", "user")
    finally:
        await c.close()


async def main() -> None:
    # T1: все STT_* заданы → запрос идёт на STT-базу, с STT-ключом и STT-моделью
    rec = Recorder()
    cfg = make_cfg(stt_api_url="https://stt.example/v1",
                   stt_api_key="stt-key",
                   stt_model="whisper-large-v3-turbo")
    text = await run_transcribe(cfg, rec)
    r = rec.requests[0]
    check("T1a STT_* заданы: URL = STT_API_URL/audio/transcriptions",
          str(r.url) == "https://stt.example/v1/audio/transcriptions", str(r.url))
    check("T1b STT_* заданы: Authorization = STT_API_KEY",
          r.headers.get("authorization") == "Bearer stt-key",
          str(r.headers.get("authorization")))
    check("T1c STT_* заданы: model = STT_MODEL",
          b'name="model"' in r.content and b"whisper-large-v3-turbo" in r.content)
    check("T1d ответ 200 → текст транскрипта", text == "привет, мир", text)

    # T2: STT_* не заданы → прежнее поведение (LLM-база, LLM-ключ, LLM_VOICE_MODEL)
    rec = Recorder()
    text = await run_transcribe(make_cfg(), rec)
    r = rec.requests[0]
    check("T2a фолбэк: URL = LLM_API_URL/audio/transcriptions",
          str(r.url) == "https://llm.example/v1/audio/transcriptions", str(r.url))
    check("T2b фолбэк: Authorization = LLM_API_KEY",
          r.headers.get("authorization") == "Bearer llm-key")
    check("T2c фолбэк: model = LLM_VOICE_MODEL",
          b'name="model"' in r.content and b"whisper-1" in r.content)
    check("T2d фолбэк: текст транскрипта", text == "привет, мир")

    # T3: задан только STT_API_URL → ключ и модель с фолбэком
    rec = Recorder()
    await run_transcribe(make_cfg(stt_api_url="https://stt.example/v1"), rec)
    r = rec.requests[0]
    check("T3a частичный: URL = STT_API_URL",
          str(r.url) == "https://stt.example/v1/audio/transcriptions", str(r.url))
    check("T3b частичный: ключ = LLM_API_KEY (фолбэк)",
          r.headers.get("authorization") == "Bearer llm-key")
    check("T3c частичный: модель = LLM_VOICE_MODEL (фолбэк)",
          b"whisper-1" in r.content)

    # T4: заданы STT_API_KEY + STT_MODEL, без URL → база с фолбэком
    rec = Recorder()
    await run_transcribe(make_cfg(stt_api_key="stt-key", stt_model="whisper-large-v3"), rec)
    r = rec.requests[0]
    check("T4a частичный: URL = LLM_API_URL (фолбэк)",
          str(r.url) == "https://llm.example/v1/audio/transcriptions", str(r.url))
    check("T4b частичный: ключ = STT_API_KEY",
          r.headers.get("authorization") == "Bearer stt-key")
    check("T4c частичный: модель = STT_MODEL",
          b"whisper-large-v3" in r.content and b"whisper-1" not in r.content)

    # T5: 402 (баланс, OpenRouter audio) → понятная подсказка про пополнение/STT_*
    rec = Recorder(status=402, payload={"error": {"message": "requires at least $0.50 in balance for audio"}})
    try:
        await run_transcribe(make_cfg(), rec)
        check("T5 402 → LLMError", False, "исключение не выброшено")
    except LLMError as e:
        msg = str(e)
        check("T5a 402 → LLMError с кодом", "402" in msg, msg)
        check("T5b 402 → подсказка про STT_API_URL / Groq",
              "STT_API_URL" in msg and "Groq" in msg, msg)

    # T6: LLM_STT_ENABLED=false → отказ, запросов в сеть нет (независимо от STT_*)
    rec = Recorder()
    try:
        await run_transcribe(make_cfg(stt_api_url="https://stt.example/v1",
                                      llm_stt_enabled=False), rec)
        check("T6 LLM_STT_ENABLED=false → LLMError", False, "исключение не выброшено")
    except LLMError as e:
        check("T6a LLM_STT_ENABLED=false → LLMError «отключено»", "отключено" in str(e), str(e))
    check("T6b LLM_STT_ENABLED=false → запрос не уходил", len(rec.requests) == 0)

    # T7: регрессия — chat() при заданных STT_* ходит на LLM-базу (STT не влияет)
    rec = Recorder()
    content = await run_chat(make_cfg(stt_api_url="https://stt.example/v1",
                                      stt_api_key="stt-key",
                                      stt_model="whisper-large-v3-turbo"), rec)
    r = rec.requests[0]
    check("T7a chat: URL = LLM_API_URL/chat/completions",
          str(r.url) == "https://llm.example/v1/chat/completions", str(r.url))
    check("T7b chat: ключ = LLM_API_KEY",
          r.headers.get("authorization") == "Bearer llm-key")
    check("T7c chat: контент ответа", content == '{"ok": true}', content)


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\nИтог: {passed} ок, {failed} провалено")
    sys.exit(1 if failed else 0)

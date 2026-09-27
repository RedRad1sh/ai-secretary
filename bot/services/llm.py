"""Универсальный AI-клиент.

Основной режим — OpenAI-совместимый контракт (ТЗ-универсальность):
    LLM_API_URL  — базовый URL, например https://api.proxyapi.ru/openai/v1
    LLM_API_KEY  — Bearer-ключ (для локальных серверов может быть пустым)
    LLM_MODEL    — имя модели, например gpt-4o-mini / deepseek-chat / qwen2.5:7b

Подходит для: ProxyAPI.ru, VseGPT.ru (оба — РФ, оплата рублями), OpenRouter,
OpenAI, DeepSeek, Groq, Together, YandexGPT (compat-режим), Ollama/LM Studio/vLLM.

Альтернатива — нативный GigaChat (OAuth Сбера): если LLM_API_URL не задан,
но есть GIGACHAT_API_KEY, фабрика вернёт GigaChatClient из bot.services.giga.

Если не задано ничего — фабрика вернёт None: бот работает только офлайн-парсером.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any

import httpx

from bot.config import Config

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Единая ошибка AI-провайдера (и OpenAI-совместимого, и GigaChat)."""


class _RateLimiter:
    """Токен-бакет: не более max_calls за period секунд. 18/60 = безопасно под OpenRouter 20 RPM."""

    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.max_calls <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._times and self._times[0] <= now - self.period:
                    self._times.popleft()
                if len(self._times) < self.max_calls:
                    self._times.append(now)
                    return
                sleep_for = self._times[0] + self.period - now + 0.05
            if sleep_for > 0:
                log.info("RateLimiter: лимит %s req/%.0fs, пауза %.1fs", self.max_calls, self.period, sleep_for)
                await asyncio.sleep(sleep_for)


class OpenAICompatClient:
    """Клиент OpenAI-контракта: /chat/completions + /audio/transcriptions.

    Фишки для free-tier OpenRouter:
    - rate-limiter 18 RPM (ниже 20)
    - ретраи на 429/5xx с exponential backoff + Retry-After
    - фолбек по списку моделей (LLM_FALLBACK_MODELS)
    - авто-откат json_object если модель вернула пустой content
    """

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.base = cfg.llm_api_url.rstrip("/")
        self._no_json_mode = False  # провайдер отказал в response_format -> не слать больше
        self._limiter = _RateLimiter(cfg.llm_rate_limit_rpm, 60.0)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )

    @property
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.llm_api_key:
            h["Authorization"] = f"Bearer {self.cfg.llm_api_key}"
        return h

    async def close(self) -> None:
        await self._http.aclose()

    def _model_chain(self) -> list[str]:
        primary = (self.cfg.llm_model or "").strip()
        raw = (self.cfg.llm_fallback_models or "").strip()
        fallbacks = [m.strip() for m in raw.split(",") if m.strip()] if raw else []
        # поддержка LLM_MODEL="a,b,c" как сокращённая запись фолбеков
        if "," in primary:
            parts = [p.strip() for p in primary.split(",") if p.strip()]
            primary = parts[0]
            fallbacks = parts[1:] + fallbacks
        chain = []
        for m in [primary] + fallbacks:
            if m and m not in chain:
                chain.append(m)
        return chain or ["openrouter/free"]

    # ---------- chat/completions ----------

    async def chat(self, system: str, user: str, *,
                   temperature: float = 0.1, max_tokens: int = 1024) -> str:
        chain = self._model_chain()
        last_err: Exception | None = None

        for model in chain:
            for attempt in range(max(1, self.cfg.llm_max_retries)):
                await self._limiter.acquire()
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if self.cfg.llm_json_mode and not self._no_json_mode:
                    payload["response_format"] = {"type": "json_object"}

                try:
                    resp = await self._post("/chat/completions", payload)
                except LLMError as e:
                    last_err = e
                    if attempt < self.cfg.llm_max_retries - 1:
                        backoff = (1.5 ** attempt) + random.uniform(0, 0.5)
                        log.warning("LLM сеть %s (%s) — ретрай %s/%s через %.1fs", model, e, attempt+1, self.cfg.llm_max_retries, backoff)
                        await asyncio.sleep(backoff)
                        continue
                    break

                # 429 / 5xx — ретраи с бэкоффом
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    retry_after = _retry_after(resp)
                    backoff = retry_after if retry_after is not None else (1.5 ** attempt + random.uniform(0, 0.5))
                    is_last_attempt = attempt == self.cfg.llm_max_retries - 1
                    log.warning("LLM %s: %s (попытка %s/%s) — жду %.1fs %s",
                                model, f"{resp.status_code} {resp.text[:150]}",
                                attempt+1, self.cfg.llm_max_retries, backoff,
                                f"→ фолбек {chain[chain.index(model)+1]}" if is_last_attempt and model != chain[-1] else "")
                    last_err = LLMError(f"{resp.status_code} {resp.text[:200]} ({_hint(resp.status_code)})")
                    if not is_last_attempt:
                        await asyncio.sleep(backoff)
                        continue
                    break

                # 400 с response_format — выключаем json и повторяем
                if resp.status_code == 400 and "response_format" in resp.text and "response_format" in payload:
                    log.warning("Провайдер %s не поддержал response_format — повтор без json-режима", model)
                    self._no_json_mode = True
                    payload.pop("response_format", None)
                    try:
                        resp = await self._post("/chat/completions", payload)
                    except LLMError as e:
                        last_err = e
                        break
                    if resp.status_code != 200:
                        last_err = LLMError(f"chat/completions: {resp.status_code} {resp.text[:200]} ({_hint(resp.status_code)})")
                        if resp.status_code in (429, 500, 502, 503, 504):
                            continue
                        break

                if resp.status_code != 200:
                    last_err = LLMError(f"chat/completions: {resp.status_code} {resp.text[:200]} ({_hint(resp.status_code)})")
                    break

                def _parse_content(r) -> tuple[dict, dict, str | None]:
                    try:
                        d = r.json()
                    except ValueError as e:
                        raise LLMError(f"ответ не JSON: {r.text[:200]}") from e
                    try:
                        ch = d["choices"][0]
                        cnt = (ch.get("message") or {}).get("content")
                    except (KeyError, IndexError, TypeError) as e:
                        raise LLMError(f"неожиданный формат ответа: {str(d)[:300]}") from e
                    return d, ch, cnt

                try:
                    raw, choice, content = _parse_content(resp)
                except LLMError as e:
                    last_err = e
                    break

                # пустой content + json_mode → ретрай без json_mode (poolside и т.д.)
                if not content and "response_format" in payload:
                    log.warning("Модель %s вернула пустой content c response_format=json_object — повтор без json-режима", model)
                    self._no_json_mode = True
                    payload.pop("response_format", None)
                    await self._limiter.acquire()
                    try:
                        resp2 = await self._post("/chat/completions", payload)
                    except LLMError as e:
                        last_err = e
                        break
                    if resp2.status_code != 200:
                        last_err = LLMError(f"chat/completions (retry): {resp2.status_code} {resp2.text[:200]} ({_hint(resp2.status_code)})")
                        break
                    try:
                        raw, choice, content = _parse_content(resp2)
                    except LLMError as e:
                        last_err = e
                        break

                # если модель оборвала из-за лимита токенов — ретрай с увеличенным бюджетом
                if choice.get("finish_reason") == "length":
                    # reasoning-модели съели бюджет на размышления
                    if max_tokens < 2048 and attempt < self.cfg.llm_max_retries - 1:
                        new_tokens = min(2048, max_tokens * 2)
                        log.warning("LLM %s оборвала ответ (finish_reason=length, max_tokens=%s) — ретрай с %s", model, max_tokens, new_tokens)
                        max_tokens = new_tokens
                        # не считаем это за фолбек модели, просто следующая попытка той же модели
                        await asyncio.sleep(0.5 + attempt * 0.5)
                        continue
                    else:
                        log.warning("LLM %s length-обрыв даже с %s токенов, пробую фолбек", model, max_tokens)
                        last_err = LLMError(f"модель {model} оборвала ответ (finish_reason=length, max_tokens={max_tokens}). Сырой: {str(raw)[:400]}")
                        break

                if content:
                    usage = raw.get("usage") or {}
                    log.debug("LLM ok: model=%s (запрошена %s) prompt=%s completion=%s",
                              raw.get("model"), model, usage.get("prompt_tokens"), usage.get("completion_tokens"))
                    if raw.get("model") and raw.get("model") != model:
                        log.info("LLM роутер %s → %s", model, raw.get("model"))
                    return content

                err = raw.get("error") or choice.get("error")
                last_err = LLMError(
                    f"модель {model} вернула пустой content (finish_reason={choice.get('finish_reason')}, error={str(err)[:150]}). Сырой: {str(raw)[:400]}"
                )
                log.warning("LLM %s пустой ответ — пробую фолбек: %s", model, last_err)
                break  # к следующей модели

            if model != chain[-1]:
                log.info("LLM фолбек: %s → %s (причина: %s)", model, chain[chain.index(model)+1], last_err)
                continue
        raise last_err or LLMError("все LLM-модели исчерпаны")

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self._http.post(
                f"{self.base}{path}", headers=self._headers, json=payload
            )
        except httpx.HTTPError as e:
            raise LLMError(f"сеть: {e}") from e

    # ---------- распознавание речи (опционально) ----------

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        if not self.cfg.llm_stt_enabled:
            raise LLMError("распознавание речи отключено (LLM_STT_ENABLED=false)")
        try:
            resp = await self._http.post(
                f"{self.base}/audio/transcriptions",
                headers={"Authorization": self._headers.get("Authorization", "")}
                if self.cfg.llm_api_key else {},
                files={"file": (filename, audio, "audio/ogg")},
                data={"model": self.cfg.llm_voice_model},
            )
        except httpx.HTTPError as e:
            raise LLMError(f"сеть: {e}") from e

        if resp.status_code in (404, 422):
            raise LLMError(
                "провайдер не поддерживает audio/transcriptions — отключите голосовой "
                "ввод (LLM_STT_ENABLED=false) или задайте LLM_VOICE_MODEL"
            )
        if resp.status_code != 200:
            raise LLMError(
                f"audio/transcriptions: {resp.status_code} {resp.text[:150]} "
                f"({_hint(resp.status_code)})"
            )
        return resp.json().get("text", "").strip()


def _retry_after(resp: httpx.Response) -> float | None:
    for h in ("Retry-After", "retry-after", "X-RateLimit-Reset", "x-ratelimit-reset"):
        v = resp.headers.get(h)
        if v:
            try:
                fv = float(v)
                if fv > 1e12:
                    fv = fv/1000 - time.time()
                elif fv > 1e9:
                    fv = fv - time.time()
                if 0 < fv < 60:
                    return fv
                if fv >= 60:
                    return 5.0
            except ValueError:
                continue
    return None


def _hint(status: int) -> str:
    return {
        401: "неверный LLM_API_KEY",
        403: "нет доступа к модели LLM_MODEL",
        404: "проверьте LLM_API_URL (обычно заканчивается на /v1) и LLM_MODEL",
        429: "лимит провайдера — подождите",
        500: "ошибка на стороне провайдера",
    }.get(status, "проверьте LLM_API_URL / LLM_API_KEY / LLM_MODEL")


def create_llm_client(cfg: Config):
    """Фабрика: OpenAI-совместимый -> нативный GigaChat -> None (офлайн-парсер)."""
    if cfg.llm_api_url:
        log.info("AI: OpenAI-совместимый провайдер %s (model=%s fallback=%s)",
                 cfg.llm_api_url, cfg.llm_model, cfg.llm_fallback_models)
        return OpenAICompatClient(cfg)
    if cfg.giga_api_key:
        from bot.services.giga import GigaChatClient  # лениво: тяжёлые импорты не нужны
        log.info("AI: нативный GigaChat (OAuth Сбера)")
        return GigaChatClient(cfg)
    log.warning("AI не настроен (LLM_API_URL или GIGACHAT_API_KEY) — "
                "работает только офлайн-парсер дат")
    return None

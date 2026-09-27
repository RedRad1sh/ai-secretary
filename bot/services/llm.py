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
        self._no_json_models: set[str] = set()
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
                   temperature: float = 0.1, max_tokens: int | None = None) -> str:
        try:
            return await asyncio.wait_for(
                self._chat(system, user, temperature=temperature, max_tokens=max_tokens),
                timeout=self.cfg.llm_total_timeout,
            )
        except asyncio.TimeoutError as e:
            raise LLMError("исчерпан общий бюджет времени LLM; используем локальный парсер") from e

    async def _chat(self, system, user, *, temperature, max_tokens):
        last_err = LLMError("все LLM-модели исчерпаны")
        for model in self._model_chain():
            tokens = min(max_tokens or self.cfg.llm_max_tokens, self.cfg.llm_max_tokens_limit)
            use_json = self.cfg.llm_json_mode and model not in self._no_json_models
            failures = 0
            empty_retry = False
            while failures < max(1, self.cfg.llm_max_retries):
                payload = {
                    "model": model,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "temperature": temperature, "max_tokens": tokens,
                }
                if use_json:
                    payload["response_format"] = {"type": "json_object"}
                await self._limiter.acquire()  # EVERY physical request counts
                try:
                    resp = await self._post("/chat/completions", payload)
                except LLMError as e:
                    last_err = e
                    failures += 1
                    if failures < max(1, self.cfg.llm_max_retries):
                        await asyncio.sleep(1.5 ** (failures - 1))
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    failures += 1
                    last_err = LLMError(f"{model}: HTTP {resp.status_code}")
                    if failures < max(1, self.cfg.llm_max_retries):
                        await asyncio.sleep(_retry_after(resp) or 1.5 ** (failures - 1))
                    continue
                # Some providers call JSON mode a 'feature', not response_format.
                body = resp.text.lower()
                unsupported = any(word in body for word in ("response_format", "json", "does not support fea", "unsupported feature"))
                if resp.status_code in (400, 422) and use_json and unsupported:
                    use_json = False
                    self._no_json_models.add(model)
                    log.warning("LLM %s: JSON mode unsupported; retry without it", model)
                    continue
                if resp.status_code != 200:
                    last_err = LLMError(f"{model}: HTTP {resp.status_code} ({_hint(resp.status_code)})")
                    if resp.status_code == 401:
                        raise last_err
                    break
                try:
                    raw = resp.json()
                    choice = raw["choices"][0]
                    content = (choice.get("message") or {}).get("content")
                    reason = choice.get("finish_reason")
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    last_err = LLMError(f"{model}: неверная структура ответа")
                    break
                # Check truncation BEFORE empty-content/JSON compatibility retries.
                if reason == "length":
                    last_err = LLMError(f"{model}: ответ обрезан, max_tokens={tokens}")
                    if tokens < self.cfg.llm_max_tokens_limit:
                        tokens = min(tokens * 2, self.cfg.llm_max_tokens_limit)
                        log.warning("LLM %s: length, retry with %s tokens", model, tokens)
                        continue
                    break
                if isinstance(content, str) and content.strip():
                    log.info("LLM ok: requested=%s actual=%s tokens=%s", model, raw.get("model"), (raw.get("usage") or {}).get("completion_tokens"))
                    return content
                if use_json and not empty_retry:
                    empty_retry = True
                    use_json = False
                    continue  # transient empty output does not poison capability cache
                last_err = LLMError(f"{model}: пустой content, finish_reason={reason}")
                break
            log.warning("LLM fallback after %s: %s", model, last_err)
        raise last_err

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

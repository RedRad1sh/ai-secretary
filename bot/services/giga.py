"""Асинхронный клиент GigaChat API (ТЗ §4: основной AI-провайдер).

- Авторизация по ключу (Basic) -> Bearer-токен, кэшируется до истечения (30 мин).
- chat/completions — извлечение событий.
- audio/transcriptions (whisper) — транскрипция голосовых сообщений (ТЗ §2.2).
- TLS: поддерживает сертификат Минцифры через GIGACHAT_CA_CERT.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx

from bot.config import Config
from bot.services.llm import LLMError

log = logging.getLogger(__name__)


class GigaChatError(LLMError):
    pass


class GigaChatClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=cfg.giga_verify_tls,  # путь к CA Минцифры | True | False
        )

    async def close(self) -> None:
        await self._http.aclose()

    # ---------- авторизация ----------

    async def _authorize(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token

        key = (self.cfg.giga_api_key or "").replace("\n", "").replace("\r", "").strip()
        # Заголовки — как в официальных примерах GigaChat (включая Accept!),
        # форматы тела: json -> form-urlencoded; между попытками паузы,
        # потому что шлюз Сбера (SynGX) ограничивает частоту OAuth-запросов.
        attempts = (("json", 0.0), ("form", 1.5), ("json", 5.0))
        last_err = ""
        resp_status = 0
        for mode, delay in attempts:
            if delay:
                await asyncio.sleep(delay)
            headers = {
                "Authorization": f"Basic {key}",
                "RqUID": str(uuid.uuid4()),
                "Accept": "application/json",
                "Content-Type": (
                    "application/json" if mode == "json"
                    else "application/x-www-form-urlencoded"
                ),
            }
            kwargs: dict[str, Any] = (
                {"json": {"scope": self.cfg.giga_scope}} if mode == "json"
                else {"data": {"scope": self.cfg.giga_scope}}
            )
            try:
                resp = await self._http.post(
                    self.cfg.giga_oauth_url, headers=headers, **kwargs
                )
            except httpx.HTTPError as e:
                last_err = f"сеть: {e}"
                log.warning("GigaChat OAuth: %s", last_err)
                continue
            if resp.status_code == 200:
                data = resp.json()
                self._token = data["access_token"]
                self._token_exp = time.time() + int(data.get("expires_at", 1800)) / 1000
                log.info("GigaChat: получен новый access-токен (mode=%s)", mode)
                return self._token
            last_err = f"{resp.status_code} {resp.text[:200]}"
            resp_status = resp.status_code
            log.warning("GigaChat OAuth неудачно (mode=%s): %s", mode, last_err)

        hint = {
            401: "неверный GIGACHAT_API_KEY (нужна строка «Авторизационные данные», не Secret key)",
            403: "нет доступа к этому scope — для физлиц GIGACHAT_SCOPE=GIGACHAT_API_PERS",
            429: "шлюз Сбера троттлит — подождите минуту и повторите",
        }.get(resp_status, "проверьте GIGACHAT_API_KEY и GIGACHAT_SCOPE")
        raise GigaChatError(f"OAuth GigaChat failed: {last_err} ({hint})")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        token = await self._authorize()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        resp = await self._http.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:  # токен внезапно протух — один ретрай
            self._token = None
            token = await self._authorize()
            headers["Authorization"] = f"Bearer {token}"
            resp = await self._http.request(method, url, headers=headers, **kwargs)
        return resp

    # ---------- генерация ----------

    async def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 512,
    ) -> str:
        """Возвращает текст ответа модели (ТЗ §2.1 п.2)."""
        if not self.cfg.giga_api_key:
            raise GigaChatError("GIGACHAT_API_KEY не задан в .env")
        resp = await self._request(
            "POST",
            f"{self.cfg.giga_api_url}/chat/completions",
            json={
                "model": self.cfg.giga_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
        if resp.status_code != 200:
            raise GigaChatError(
                f"chat/completions failed: {resp.status_code} {resp.text[:300]}"
            )
        return resp.json()["choices"][0]["message"]["content"]

    # ---------- распознавание речи (ТЗ §2.2) ----------

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        """Speech-to-Text через модель whisper. Возвращает текст сообщения."""
        if not self.cfg.giga_api_key:
            raise GigaChatError("GIGACHAT_API_KEY не задан в .env")

        resp = await self._request(
            "POST",
            f"{self.cfg.giga_api_url}/audio/transcriptions",
            files={"file": (filename, audio, "audio/ogg")},
            data={"model": self.cfg.giga_asr_model},
        )
        if resp.status_code == 400 and filename.endswith(".ogg"):
            # некоторые модели ASR не любят ogg/opus — конвертируем в mp3 через ffmpeg
            mp3 = await _convert_to_mp3(audio)
            if mp3:
                return await self.transcribe(mp3, "voice.mp3")
        if resp.status_code != 200:
            raise GigaChatError(f"ASR failed: {resp.status_code} {resp.text[:300]}")
        return resp.json().get("text", "").strip()


async def _convert_to_mp3(audio: bytes) -> bytes | None:
    """Fallback-конвертация ogg->mp3 системным ffmpeg (не обязателен)."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-b:a", "64k", "-f", "mp3", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(audio), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    return out or None

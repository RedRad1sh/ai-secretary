"""Конфигурация из переменных окружения (ТЗ §3.4: секреты не в коде)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    # Telegram
    bot_token: str = ""
    allowed_user_id: int | None = None

    # Пути
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "bot.db")

    # Универсальный AI (OpenAI-совместимый контракт)
    llm_api_url: str = ""
    llm_api_key: str = ""
    # дефолт — самая стабильная free-модель OpenRouter на 27.09.2026 (3/3 ответов, 262K, ~0.8s):
    # nvidia/nemotron-3-super — топ по надёжности; при смене провайдера переопределите LLM_MODEL
    llm_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    llm_fallback_models: str = "cohere/north-mini-code:free,inclusionai/ling-3.0-flash-fin:free,liquid/lfm-2.5-2.6b:free,openrouter/free"
    llm_rate_limit_rpm: int = 18  # безопасно ниже лимита OpenRouter 20 RPM
    llm_max_retries: int = 3      # ретраев на 429/5xx + фолбек по моделям
    llm_voice_model: str = "whisper-1"
    llm_stt_enabled: bool = True
    llm_json_mode: bool = True   # response_format=json_object; при отказе провайдера авто-отключается

    # GigaChat (нативный, альтернатива)
    giga_api_key: str = ""
    giga_scope: str = "GIGACHAT_API_PERS"
    giga_model: str = "GigaChat"
    giga_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    giga_api_url: str = "https://gigachat.devices.sberbank.ru/api/v1"
    giga_verify_tls: bool | str = False  # False | True | путь к CA-сертификату
    giga_asr_model: str = "whisper"

    # Google Calendar
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""

    # Поведение
    default_tz: str = "Europe/Moscow"
    default_calendar_id: str = "primary"
    log_level: int = 20  # logging.INFO
    max_answer_seconds: float = 5.0

    @property
    def google_ready(self) -> bool:
        return bool(
            self.google_refresh_token
            and self.google_client_id
            and self.google_client_secret
        )


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")

    cfg = Config(
        bot_token=_get("BOT_TOKEN"),
        allowed_user_id=int(_get("ALLOWED_USER_ID")) if _get("ALLOWED_USER_ID") else None,
        llm_api_url=_get("LLM_API_URL"),
        llm_api_key=_get("LLM_API_KEY"),
        llm_model=_get("LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
        llm_fallback_models=_get("LLM_FALLBACK_MODELS", "cohere/north-mini-code:free,inclusionai/ling-3.0-flash-fin:free,liquid/lfm-2.5-2.6b:free,openrouter/free"),
        llm_rate_limit_rpm=int(_get("LLM_RATE_LIMIT_RPM", "18") or 18),
        llm_max_retries=int(_get("LLM_MAX_RETRIES", "3") or 3),
        llm_voice_model=_get("LLM_VOICE_MODEL", "whisper-1"),
        llm_stt_enabled=_get("LLM_STT_ENABLED", "true").lower() != "false",
        llm_json_mode=_get("LLM_JSON_MODE", "true").lower() != "false",
        giga_api_key=_get("GIGACHAT_API_KEY"),
        giga_scope=_get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        giga_model=_get("GIGACHAT_MODEL", "GigaChat"),
        google_client_id=_get("GOOGLE_CLIENT_ID"),
        google_client_secret=_get("GOOGLE_CLIENT_SECRET"),
        google_refresh_token=_get("GOOGLE_REFRESH_TOKEN"),
        default_tz=_get("DEFAULT_TIMEZONE", "Europe/Moscow"),
        default_calendar_id=_get("DEFAULT_CALENDAR_ID", "primary"),
        log_level=getattr(__import__("logging"), _get("LOG_LEVEL", "INFO").upper(), 20),
    )

    data_dir = BASE_DIR / _get("DATA_DIR", "data")
    db_path = Path(_get("DB_PATH")) if _get("DB_PATH") else data_dir / "bot.db"
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    object.__setattr__(cfg, "data_dir", data_dir)
    object.__setattr__(cfg, "db_path", db_path)

    # TLS для GigaChat: сертификат Минцифры -> verify по нему; иначе по флагу
    ca_cert = _get("GIGACHAT_CA_CERT")
    if ca_cert:
        object.__setattr__(cfg, "giga_verify_tls", ca_cert)
    else:
        object.__setattr__(
            cfg, "giga_verify_tls", _get("GIGACHAT_VERIFY_TLS", "false").lower() == "true"
        )

    return cfg


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
        if not _config.bot_token:
            raise RuntimeError(
                "BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните его."
            )
    return _config

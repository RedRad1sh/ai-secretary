#!/usr/bin/env python3
"""Собирает netrun-bot.zip для загрузки на https://netrun.io (docs/DEPLOY_NETRUN.md).

В архиве: пакет bot/, requirements.txt и Dockerfile с ENV DATA_DIR=/data —
на Netrun папка /data персистентна (БД и напоминания переживают перезапуски).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "netrun-bot.zip"

DOCKERFILE = """FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tzdata \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot/ ./bot/

RUN useradd -m botuser && mkdir -p /data && chown -R botuser /app /data
USER botuser

# Long polling: вебхуки не нужны, боту не важен домен и HTTPS
CMD ["python", "-m", "bot.main"]
"""

README = """Загружено на Netrun: Telegram-бот «AI-секретарь» (long polling).

ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (раздел Secrets в панели Netrun) — обязательно:
  BOT_TOKEN        токен от @BotFather
  ALLOWED_USER_ID  ваш числовой Telegram ID (бот отвечает только ему)
  GIGACHAT_API_KEY ключ GigaChat API (без него работает локальный парсер дат)
  GIGACHAT_SCOPE   GIGACHAT_API_PERS (для физлиц)

Необязательно:
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN
                   автосоздание событий в Google Календаре (иначе режим .ics)
  DEFAULT_TIMEZONE Europe/Moscow

Файлы (БД, логи) пишутся в /data — эта папка на Netrun персистентна.

Важно: на бесплатном тарифе Netrun бот работает 3 часа после запуска,
для 24/7 нужен Pro (см. FAQ netrun.io).
"""


def main() -> None:
    files: list[Path] = sorted((BASE / "bot").rglob("*.py"))
    files.append(BASE / "requirements.txt")

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            arcname = f.relative_to(BASE)
            z.write(f, arcname)
        z.writestr("Dockerfile", DOCKERFILE)
        z.writestr("NETRUN_README.txt", README)
        for info in z.infolist():
            print("  +", info.filename)

    print(f"\nГотово: {OUT.name} ({OUT.stat().st_size / 1024:.0f} КБ).")


if __name__ == "__main__":
    main()

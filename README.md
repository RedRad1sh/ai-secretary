# 🤖 AI-секретарь — Telegram-бот с экспортом .ics

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-0088cc.svg)](https://docs.aiogram.dev/)
[![Docker](https://img.shields.io/badge/docker-supported-2496ed.svg)](https://docs.docker.com/)
[![Tests](https://img.shields.io/badge/tests-offline%2C%20no%20network-4caf50.svg)](#тесты)
[![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial-ff69b4.svg)](LICENSE.md)
[![Status](https://img.shields.io/badge/status-in%20development-yellow.svg)](docs/ROADMAP.md)

Персональный Telegram-бот: принимает текст, голос и пересланные сообщения на русском языке,
извлекает через любой OpenAI-совместимый AI-API (ProxyAPI, VseGPT, OpenRouter, OpenAI, Ollama… или нативный GigaChat) информацию о встречах/делах/напоминаниях и готовит файлы .ics для импорта в календарь. Автоматическая интеграция Google Calendar отложена.

Реализует ТЗ: доступ из РФ без VPN, бесплатные AI-лимиты, подтверждение inline-кнопками, `/list`, `/undo`, `/settings`.

---

## Возможности

| Ввод | Что происходит |
|---|---|
| Текст («встреча с Иваном завтра в 15:00 в офисе на Ленина») | GigaChat извлекает JSON `{title, date, time, duration, location, description, participants, recurrence}` |
| Пересланное сообщение | Текст (или caption) передаётся в AI-модуль |
| Голосовое сообщение | Транскрибация через GigaChat Whisper (`/audio/transcriptions`), затем тот же пайплайн |
| Фото | OCR Tesseract rus+eng (платный whitelist), затем предпросмотр |
| ⏰ Напоминания | Бот сам пишет в Telegram заранее («Через 10 мин…»); интервал — в /settings |

Бот показывает карточку предпросмотра с кнопками **[✅ Создать] [✏️ Изменить] [❌ Отмена]**,
спрашивает уточнение, если дата не распознана, поддерживает повторяющиеся события (RRULE),
часовые пояса, fallback-экспорт **.ics**-файла, если Google Calendar недоступен/не настроен.

## Структура проекта

```
ai-secretary-bot/
├── bot/
│   ├── main.py              # точка входа (long polling)
│   ├── config.py            # настройки из переменных окружения
│   ├── db.py                # SQLite (aiosqlite): настройки, события, логи
│   ├── security.py          # шифрование Fernet (токены Google)
│   ├── models.py            # EventDraft: валидация, нормализация, RRULE
│   ├── services/
│   │   ├── giga.py          # клиент GigaChat API (chat + whisper ASR)
│   │   ├── extractor.py     # промпт + валидация JSON + dateparser-fallback
│   │   ├── gcal.py          # Google Calendar API (OAuth refresh, insert/delete/list)
│   │   └── ics.py           # генерация .ics (fallback по ТЗ §8)
│   └── handlers/
│       ├── commands.py      # /start /help /list /undo /settings
│       └── parse_flow.py    # текст/голос/форварды, FSM, inline-кнопки
├── scripts/
│   ├── gcal_auth.py         # одноразовый OAuth-скрипт → refresh-токен
│   ├── set_webhook.py       # установка webhook (для serverless/VPS)
│   └── build_yc_zip.py      # сборка yc-function.zip для Yandex Cloud Functions
├── serverless/
│   └── yandex_handler.py    # адаптер Yandex Cloud Functions (хостинг за 0 ₽)
├── docs/
│   ├── HOSTING.md           # обзор дешёвых/бесплатных хостингов
│   ├── DEPLOY_YC.md         # пошаговый деплой на Yandex Cloud Functions
│   └── SETUP_GOOGLE.md      # пошаговая настройка Google Cloud OAuth
├── tests/test_smoke.py      # офлайн-тесты (даты, RRULE, .ics)
├── Dockerfile / docker-compose.yml
├── requirements.txt / .env.example
```

## Быстрый старт (локально, 5 минут)

```bash
git clone <репозиторий> && cd ai-secretary-bot
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # заполните BOT_TOKEN и GIGACHAT_API_KEY
python -m bot.main
```

1. Создайте бота у [@BotFather](https://t.me/BotFather) → получите `BOT_TOKEN`.
2. Свой Telegram ID узнайте у [@userinfobot](https://t.me/userinfobot) → `ALLOWED_USER_ID` для прежней базы владельца; `ALLOWED_USER_IDS` для общего списка и `PAID_USER_IDS` для платного доступа.
3. AI: задайте в `.env` три переменные любого OpenAI-совместимого провайдера —
   `LLM_API_URL` + `LLM_API_KEY` + `LLM_MODEL` (примеры в `.env.example`:
   ProxyAPI и VseGPT — РФ и рубли; OpenRouter/OpenAI/DeepSeek; локальная Ollama —
   вообще без ключа). Проверка: `python scripts/test_llm.py`.
   Альтернатива — нативный GigaChat: ключ с [developers.sber.ru/gigachat](https://developers.sber.ru/gigachat).
   **Голосовые** (STT) идут на тот же провайдер; если LLM — на бесплатных
   моделях OpenRouter, а аудио там платное (402 при балансе < $0.50) —
   отведите голос на бесплатный Groq: `STT_API_URL=https://api.groq.com/openai/v1`,
   `STT_API_KEY=gsk_...`, `STT_MODEL=whisper-large-v3-turbo` (issue #12).
4. Google OAuth на этом этапе не настраивайте: бот выдаёт .ics. Сохранённый код интеграции пока не используется обработчиками.

## Запуск на сервере (Docker)

```bash
scp -r ai-secretary-bot user@vps:~/ && ssh user@vps
cd ai-secretary-bot && cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

Бот работает 24/7, `restart: unless-stopped` переживает перезагрузки. Все файлы (БД, ключи, логи) — в `./data`.

## Yandex Cloud Functions — бесплатный вариант (0 ₽/мес)

Вместо VDS бот можно развернуть как serverless-функцию с webhook: 1 000 000 вызовов и 10 GB×час в месяц — бесплатно,
личному боту хватает на порядки. Адаптер уже в репозитории: [serverless/yandex_handler.py](serverless/yandex_handler.py),
инструкция — в [docs/HOSTING.md](docs/HOSTING.md). Минус: холодный старт ~1 с и нужен платёжный аккаунт Яндекса.

## Команды бота

| Команда | Действие |
|---|---|
| `/start`, `/help` | справка |
| `/today`, `/tomorrow` | события на день (включая повторы 🔁) |
| `/list` | 10 последних созданных ботом событий со ссылками |
| `/undo` | удалить последнее созданное событие (и его напоминание) |
| `/settings` | часовой пояс, ID календаря, напоминания (10/30/60 мин) |
| `/stats` | статистика: события, напоминания, сбои AI, аптайм |

**Слова-команды** (просто текстом, офлайн-парсер): «отмени приём у терапевта» —
отмена; «перенеси встречу на 15:00» / «…на завтра в 10:30» — перенос (с подтверждением).
| `/cancel` | сбросить текущий диалог |

## Безопасность (ТЗ §3.4)

- Все секреты — в переменных окружения (`.env`), в коде их нет.
- Токены Google хранятся зашифрованными (Fernet, ключ в `ENCRYPTION_KEY` или `data/secret.key`).
- Бот отвечает только whitelist в личных чатах. Пустой whitelist закрывает доступ. Данные новых пользователей — `data/users/<ID>.db`; прежняя `bot.db` принадлежит только `ALLOWED_USER_ID`.
- TLS GigaChat: рекомендуется сертификат Минцифры (`GIGACHAT_CA_CERT`), `GIGACHAT_VERIFY_TLS=false` — только для старта.

## Тесты

```bash
python tests/test_smoke.py   # офлайн: нормализация дат, RRULE, .ics, парсинг JSON
```

## Ориентировочный бюджет

| Статья | Стоимость |
|---|---|
| AI-API (GigaChat freemium / VseGPT / ProxyAPI / Ollama) | от 0 ₽ |
| Google Calendar API | 0 ₽ |
| Telegram Bot API | 0 ₽ |
| Хостинг: Yandex Cloud Functions | **0 ₽/мес** |
| Хостинг: самый дешёвый VDS (SprintHost / 4VPS / RuVDS) | 91–139 ₽/мес |


## Доступ и эксплуатация (27.09.2026)

Сначала заполните оба списка из `.env.example`. В `PAID_USER_IDS` сейчас укажите
только свой **пользовательский** Telegram ID (не ID бота из лога).
Общий доступ: одиночное событие, текст/голос/пересылка, .ics, настройки,
`/list`, `/undo`, обычные напоминания. Платный: пакет событий, повторы,
`/today`, `/tomorrow`, управление текстом, OCR, `/stats`.
Платёжного провайдера, подписок и автоматических списаний пока нет.

Для 24/7 выбран Docker/VDS с постоянным `./data`, **один экземпляр polling**.
OCR в Docker установлен; при локальном запуске нужны `tesseract-ocr`,
`tesseract-ocr-rus`, `tesseract-ocr-eng`. Фото ≤5 МБ, OCR ограничен 20 секундами.
Текст с фото всегда проходит подтверждение; PDF и видео не распознаются.

Ежедневный бэкап: `python scripts/backup.py /secure/backups` (в Docker —
`docker compose exec bot python scripts/backup.py /app/data/backups`, затем
копирование на другой носитель). Настройте cron/Task Scheduler самостоятельно.
Скрипт использует SQLite backup API и включает все пользовательские БД и локальный
`secret.key`; `.env`/внешний `ENCRYPTION_KEY` храните отдельно в защищённом хранилище.
Восстановление: остановить бот, вернуть `bot.db`, `users/*.db` и ключ на прежние
пути, восстановить прежний `ALLOWED_USER_ID`, запустить и проверить `/list`.
Бэкапы содержат личные данные: не публикуйте их и ограничьте доступ.

Webhook-адаптер сохранён, но `/tmp` в YC Functions **не является постоянной БД**.
Без внешнего хранилища этот вариант не считается готовым для надёжных напоминаний.
Подробности и незакрытые проверки: [дорожная карта](docs/ROADMAP.md).

Проверки без сети:
```bash
python tests/test_smoke.py
python tests/test_integration.py
python tests/test_multuser_security.py
python tests/test_html_escaping.py
python tests/test_parsing_ics.py
python tests/test_stage3_reliability.py
python tests/test_stage4_acceptance.py   # OCR-часть: реальный tesseract, если он установлен
python -m unittest discover -s tests -p test_regressions.py
```

# 🤖 AI-секретарь — Telegram-бот для Google Календаря

Персональный Telegram-бот: принимает текст, голос и пересланные сообщения на русском языке,
извлекает через любой OpenAI-совместимый AI-API (ProxyAPI, VseGPT, OpenRouter, OpenAI, Ollama… или нативный GigaChat) информацию о встречах/делах/напоминаниях и создаёт события в Google Календаре.

Реализует ТЗ: доступ из РФ без VPN, бесплатные AI-лимиты, подтверждение inline-кнопками, `/list`, `/undo`, `/settings`.

---

## Возможности

| Ввод | Что происходит |
|---|---|
| Текст («встреча с Иваном завтра в 15:00 в офисе на Ленина») | GigaChat извлекает JSON `{title, date, time, duration, location, description, participants, recurrence}` |
| Пересланное сообщение | Текст (или caption) передаётся в AI-модуль |
| Голосовое сообщение | Транскрибация через GigaChat Whisper (`/audio/transcriptions`), затем тот же пайплайн |
| Изображение | Пока не поддерживается (этап 2 по ТЗ) |
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
2. Свой Telegram ID узнайте у [@userinfobot](https://t.me/userinfobot) → `ALLOWED_USER_ID` (бот игнорирует всех остальных).
3. AI: задайте в `.env` три переменные любого OpenAI-совместимого провайдера —
   `LLM_API_URL` + `LLM_API_KEY` + `LLM_MODEL` (примеры в `.env.example`:
   ProxyAPI и VseGPT — РФ и рубли; OpenRouter/OpenAI/DeepSeek; локальная Ollama —
   вообще без ключа). Проверка: `python scripts/test_llm.py`.
   Альтернатива — нативный GigaChat: ключ с [developers.sber.ru/gigachat](https://developers.sber.ru/gigachat).
4. Google Календарь: выполните `python scripts/gcal_auth.py` (нужен `client_secret.json` из Google Cloud Console — пошагово в [docs/SETUP_GOOGLE.md](docs/SETUP_GOOGLE.md)) и вставьте `GOOGLE_REFRESH_TOKEN` в `.env`. Без этого шага бот работает в режиме `.ics`-файлов.

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
- Бот отвечает только на сообщения от `ALLOWED_USER_ID`.
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

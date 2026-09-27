# 🔑 Настройка Google Calendar API (OAuth 2.0) — пошагово

Google Calendar API **работает из РФ без VPN**. Плата — 0 ₽ (лимит 1 млн запросов/день).
Понадобится обычный Google-аккаунт. Займёт ~10 минут.

## Шаг 1. Проект в Google Cloud Console

1. Откройте https://console.cloud.google.com/ (из РФ открывается).
2. Сверху: **Select project → New Project** → имя `ai-secretary` → **Create**.

## Шаг 2. Включаем Calendar API

1. Меню → **APIs & Services → Library**.
2. Найдите **Google Calendar API** → **Enable**.

## Шаг 3. OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User Type: **External** → Create.
3. App name: `AI Secretary`, e-mail — ваш.
4. **Scopes** — пропустите (добавим вручную).
5. **Test users**: добавьте **свой Google-адрес** (обязательно — иначе при тестовом статусе OAuth не пустит).
6. Save. Статус приложения можно оставить **Testing** — refresh-токен работает и так;
   если через 7 дней токен перестанет обновляться, нажмите «Publish app» (верификация не нужна для личного use-case с scope calendar).

## Шаг 4. Создаём OAuth-клиент (Desktop app)

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Desktop app** → Create.
3. **Download JSON** → сохраните как `client_secret.json` в корень проекта (в `.gitignore` уже добавлен).

## Шаг 5. Получаем refresh-токен

```bash
pip install -r requirements.txt
python scripts/gcal_auth.py   # откроется браузер, войдите, разрешите доступ
```

Скрипт выведет `GOOGLE_REFRESH_TOKEN=...` — скопируйте строку в `.env`.
Токен бессрочный, пока вы не отзовёте доступ в https://myaccount.google.com/permissions.

## Шаг 6. Проверка

Отправьте боту: «встреча-тест завтра в 9:00 на 15 минут» → **✅ Создать**.
Событие появится в календаре, бот пришлёт ссылку `https://calendar.google.com/...`.

## Если Google недоступен / что-то сломалось

Бот автоматически предложит скачать **`.ics`-файл** (fallback из ТЗ §8) — его можно
импортировать в Google Calendar, Яндекс.Календарь, Outlook и любой другой.
Создать событие без календаря: просто ответьте боту и нажмите кнопку.

## Частые проблемы

| Ошибка | Решение |
|---|---|
| `403 access_denied` — app not verified | Добавьте себя в Test users (шаг 3.5) |
| `invalid_grant` при обновлении | Системное время на сервере — включите NTP (`timedatectl set-ntp true`) |
| Токен протух через 7 дней | Опубликуйте приложение (Publish app) или пройдите скрипт заново |
| `409 duplicate` | Событие с таким же временем уже есть — это не ошибка бота |

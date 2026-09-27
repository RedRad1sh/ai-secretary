# ☁️ Деплой на Yandex Cloud Functions — хостинг за 0 ₽/мес

Бесплатные лимиты YC каждый месяц: **1 000 000 вызовов** функции + **10 GB×час** вычислений.
Личному боту нужно ~сотни вызовов в месяц → запас в сотни раз, платить не придётся.
Оплата картой нужна только для активации биллинг-аккаунта, списаний в рамках лимитов нет.

## Что понадобится (5 минут подготовки)

1. YAML/логин на Яндексе → https://console.yandex.cloud
2. Создать **биллинг-аккаунт** (привязка карты РФ; в рамках бесплатных лимитов — 0 ₽).
3. Создать **каталог** (папку), например `bot`.
4. Подготовленные ключи: `BOT_TOKEN` (@BotFather), `GIGACHAT_API_KEY` (developers.sber.ru),
   опционально `GOOGLE_*` (docs/SETUP_GOOGLE.md).

## Шаг 1. Соберите ZIP для функции

```bash
python scripts/build_yc_zip.py
# -> yc-function.zip (bot/ + yandex_handler.py + requirements.txt)
```

## Шаг 2. Создайте функцию (консоль)

1. Консоль → каталог → **Cloud Functions → Создать функцию** → имя `ai-secretary`.
2. Редактор:
   - Среда выполнения: **Python 3.12**
   - Код: загрузить **ZIP** `yc-function.zip`
   - **Точка входа:** `yandex_handler.handler`
   - Память: **512 МБ** (хватит и 256, но dateparser прожорлив на старте)
   - Таймаут: **60 с** (GigaChat иногда отвечает несколько секунд)
3. **Переменные окружения:**

   | Ключ | Значение |
   |---|---|
   | `BOT_TOKEN` | токен из BotFather |
   | `ALLOWED_USER_ID` | ваш Telegram ID |
   | `LLM_API_URL` | OpenAI-совместимый API (ProxyAPI/VseGPT/OpenRouter/Ollama…) — или GigaChat ниже |
   | `LLM_API_KEY` / `LLM_MODEL` | ключ и модель провайдера |
   | `GIGACHAT_API_KEY` / `GIGACHAT_SCOPE` | альтернатива: нативный GigaChat |
   | `DEFAULT_TIMEZONE` | `Europe/Moscow` |
   | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` | опционально (иначе режим .ics) |

4. **Сохранить** → в разделе «Обзор» скопируйте **ссылку** вида
   `https://functions.yandexcloud.net/xxxxxxxxxxxxxxxx`.

## Шаг 3. Включите публичный доступ

1. Консоль → каталог → **Cloud Functions → функция `ai-secretary`**.
2. Вкладка **«Обзор»**: в поле **«Ссылка для вызова»** скопируйте адрес
   вида `https://functions.yandexcloud.net/xxxxxxxxxxxxxxxx` (нужен на шаге 4).
3. На той же странице включите переключатель **«Публичная функция»**.
   Если не находите его — меню **⋮ → «Включить публичный доступ»** (или вкладка «Настройки»).
4. Альтернатива через CLI: `yc serverless function allow-unauthenticated-invoke ai-secretary`

Без публичного доступа Telegram будет получать **403 Forbidden** и вебхук не заработает.
Для безопасности это ок: у бота белый список по `ALLOWED_USER_ID`, чужие сообщения
функция молча игнорирует.

## Шаг 4. Установите вебхук Telegram

**Способ А — прямо в браузере (ничего устанавливать не нужно):**

1. Возьмите токен бота от @BotFather (вида `123456789:AAF...`).
2. Подставьте его и ссылку функции в адрес ниже — **без пробелов и переносов,
   токен приклеен к слову `bot`**:

   ```
   https://api.telegram.org/bot123456789:AAF.../setWebhook?url=https://functions.yandexcloud.net/xxxxxxxxxxxxxxxx
   ```

3. Откройте адрес в браузере. Успех выглядит так:
   `{"ok":true,"result":true,"description":"Webhook was set"}`
4. Проверка: откройте тот же адрес, заменив `setWebhook` на `getWebhookInfo` —
   в ответе должен быть ваш `url` и **отсутствовать** `last_error_message`.

**Способ Б — скриптом из проекта (на машине с Python):**

```bash
python scripts/set_webhook.py https://functions.yandexcloud.net/<id>
python scripts/set_webhook.py --info   # проверить: url и pending_updates_count
```

или одним curl:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://functions.yandexcloud.net/<id>"
```

Готово: напишите боту «встреча завтра в 15:00» — придёт карточка с кнопками.

### Как читать логи функции (после обновления версии кода)

Каждый шаг обработки теперь пишется в логи — по ним видно, где именно затык:

| Строка в логе | Что означает |
|---|---|
| `Бот инициализирован. ALLOWED_USER_ID=..., GigaChat key=...` | Проверьте здесь, что переменные окружения попали в версию корректно |
| `Апдейт Telegram: update_id=... from=... ожидаемый_ID=...` | Сообщение дошло! `from` и `ожидаемый_ID` должны совпасть — иначе бот молча игнорирует (белый список) |
| `Апдейт ... обработан` | Обработка завершилась без ошибок |
| `handler failed: ... Unauthorized` | Неверный `BOT_TOKEN` в переменных версии |
| `GigaChat вызов упал — пробуем локальный dateparser` | Проблема с ключом/лимитами GigaChat. Бот всё равно ответит карточкой (локальный парсер дат) |
| `Запрос без апдейта Telegram (проверка живости)` | Кто-то открыл ссылку функции в браузере — это нормально, не ошибка |

### Если не работает

| Симптом (getWebhookInfo / логи YC) | Причина и лечение |
|---|---|
| `Code: 499 Request cancelled` в логах функции | Клиент (Telegram) оборвал соединение во время долгой обработки. Telegram повторит доставку сам — после обновления версии недоставленные сообщения могут прийти повторно |
| `last_error_message: ... Connection timed out` | Telegram не смог соединиться с функцией: чаще всего функция была непубличной в момент доставки или версия пересоздавалась. Проверьте ссылку браузером (должно быть «Bot is alive»), затем `deleteWebhook?drop_pending_updates=true` и `setWebhook` заново |
| `last_error_message: ... 403 Forbidden` | Не включён «Публичная функция» (шаг 3) |
| `... 500 Internal Server Error` | Ошибка в коде/переменных: вкладка «Логи» функции покажет traceback |
| `... Function not found` | Опечатка в ссылке функции в шаге 4 |
| После сообщений в логах вообще нет строк `START RequestID` | Telegram не доставляет апдейты: проверьте getWebhookInfo (url, pending_update_count, last_error_message), затем `deleteWebhook?drop_pending_updates=true` → пауза 1 мин → `setWebhook` заново → отправьте свежее сообщение |
| Бот молчит, ошибок нет | Проверьте `ALLOWED_USER_ID` — сообщения чужих Telegram ID молча игнорируются |
| `Unauthorized` в ответе Telegram | Неверный BOT_TOKEN в адресе |

⚠️ Если позже запустите бота локально через `python -m bot.main` (long polling) —
сначала снимите вебхук: `https://api.telegram.org/bot<TOKEN>/deleteWebhook`,
иначе polling не будет получать сообщения. И наоборот: после локальных тестов
повторите `setWebhook`.

## Шаг 5 (альтернатива). Всё через yc CLI

```bash
curl -sSL https://storage.yandexcloud.net/yandexcloud-yc/install.sh | bash   # установка CLI
yc init                                       # выбрать каталог и биллинг
python scripts/build_yc_zip.py

yc serverless function create --name=ai-secretary

yc serverless function version create \
  --function-name=ai-secretary \
  --runtime=python312 \
  --entrypoint=yandex_handler.handler \
  --memory=512mb \
  --execution-timeout=60s \
  --source-path=yc-function.zip \
  --environment="BOT_TOKEN=...,ALLOWED_USER_ID=...,LLM_API_URL=...,LLM_API_KEY=...,LLM_MODEL=..."

yc serverless function allow-unauthenticated-invoke ai-secretary

URL=$(yc serverless function get --name=ai-secretary --format=json | jq -r .http_invoke_url)
python scripts/set_webhook.py "$URL"
```

## Напоминания в Telegram (⏰ плановые уведомления)

Бот сам пишет вам заранее: «⏰ Через 10 мин (13:00) — Приём у терапевта».

- **Локально / VDS**: фоновый планировщик проверяет напоминания каждые 30 с — работает сразу, ничего настраивать не нужно.
- **Serverless (YC Functions)**: напоминания отправляются «попутно» при любом сообщении боту. Чтобы они приходили гарантированно (даже когда бот «спит»), добавьте Timer-триггер, который будит функцию каждую минуту:

  Консоль: каталог → **Триггеры → Создать триггер → Тип: Timer** → расписание «каждая минута» (cron `*/1 * * * ?`) → «Функция»: `organaizer` → сервисный аккаунт с ролью `serverless.functions.invoker` → Создать.

  Или CLI:

  ```bash
  yc serverless trigger create timer \
    --name organaizer-reminders \
    --cron '*/1 * * * ?' \
    --invoke-function-name organaizer \
    --invoke-function-service-account-id <ID-сервисного-аккаунта>
  ```

  Это 43 200 вызовов в месяц — бесплатный лимит (1 млн) покрывает с запасом в 20+ раз.

Выбор «за сколько минут» и выключение — в боте: `/settings → 🔔 Напоминания`.
`/undo` удаляет и напоминание события. Для повторяющихся событий напоминание
ставится на первое вхождение (дальше уведомляет сам календарь).

## Обновление бота

Правите код → снова `python scripts/build_yc_zip.py` → новая версия функции
(консоль: «Редактор» → загрузить zip → «Создать версию»; CLI: та же команда
`function version create`). Вебхук менять не нужно — ссылка прежняя.

## Логи и отладка

```bash
yc serverless function logs --name=ai-secretary --since=1h
```
или вкладка «Логи» в консоли. Все запросы бота пишутся туда (`INFO`/`ERROR`).

## Нюансы и ограничения (важно!)

| Нюанс | Что делать |
|---|---|
| Файловая система функции **эфемерная** (`/tmp`) | SQLite (история `/list`) живёт до «засыпания» функции. Для постоянной истории подключите YDB Serverless (бесплатный лимит 1 ГБ) — заменяется только `bot/db.py` |
| Холодный старт **~1–3 с** (импорт библиотек) | В лимит Telegram и ТЗ (5 с) укладывается. Уменьшить: минимальный `requirements.txt` без google-библиотек |
| Секреты в переменных окружения видны в консоли | Для продвинутого режима — Yandex Lockbox (секреты монтируются в функцию) |
| Параллельные вызовы | YC масштабирует инстансы автоматически; FSM-состояние в MemoryStorage живёт в каждом инстансе — для одного пользователя это не проблема |
| Оплата сверх лимитов | 1 млн вызовов/мес бесплатно; сверх — порядка копеек за 1000 вызовов. Для личного бота недостижимо |

## Стоимость итоговая

| Статья | Цена |
|---|---|
| Yandex Cloud Functions | 0 ₽ (лимиты закрывают личный бот с запасом) |
| GigaChat freemium | 0 ₽ |
| Telegram Bot API | 0 ₽ |
| Google Calendar API | 0 ₽ |
| **Итого в месяц** | **0 ₽** |

# 🖥 Хостинг для бота: самый дешёвый и бесплатный (исследование, сентябрь 2026)

Требования из ТЗ: сервер доступен из РФ без VPN, оплата российской картой (СБП/МИР), 24/7,
для Python-бота достаточно 1 vCPU / 512 МБ–1 ГБ RAM / 5–10 ГБ диска.

## TL;DR — рекомендации

| Вариант | Цена | Вердикт |
|---|---|---|
| **Yandex Cloud Functions (serverless, webhook)** | **0 ₽/мес** | 🥇 Идеально по ТЗ: бесплатно навсегда в рамках лимитов (1 млн вызовов + 10 GB×час/мес), работает из РФ. Адаптер в `serverless/yandex_handler.py`. Минусы: холодный старт ~1 с, нужен платёжный аккаунт Яндекса |
| **Старый Android + Termux / домашний ПК / Raspberry Pi** | 0 ₽ | 🥈 Работает long polling, ничего не нужно платить. Минусы: зависит от вашей квартиры/розетки |
| **SprintHost VDS (Москва/СПб)** | **от 91 ₽/мес** | 🥉 Самый дешёвый осмысленный VDS: 1 vCPU / 512 МБ / 10 ГБ NVMe, порт 10 Гбит/с, Anti-DDoS, оплата СБП и МИР |
| 4VPS.su (RU-локация) | от ~80–126 ₽/мес | Дешевле всех формально (126 ₽ за 1 CPU / 1 ГБ / 10 ГБ NVMe в РФ) |
| UltraVDS | от 119 ₽/мес | Самый дешёвый в обзоре Хабра (май 2026) |
| RuVDS | от 139 ₽/мес (512 МБ HDD; 209 ₽ за SSD) | Крупный провайдер, свой ДЦ, 3 дня триала |
| Спринтбокс | от 130 ₽/мес (0,5 ГБ / NVMe) | Брат SprintHost |
| VDSina | от 150 ₽/мес | Посуточная оплата — удобно «попробовать и выключить» |
| Beget / FirstVDS | от 210–219 ₽/мес | Консервативно и надёжно, 30 дней теста у Beget |
| Timeweb Cloud | от ~477–710 ₽/мес | Подорожал: минимальный тариф больше не бюджетный |

**Практический совет:** для стабильности берите тариф с **1 ГБ RAM** (у 4VPS это ~126 ₽/мес, у SprintHost второй тариф ~150–180 ₽). На 512 МБ бот тоже живёт, но `pip install` тяжёлых зависимостей (`google-api-python-client`, `dateparser`) может упасть по OOM — добавьте swap 1–2 ГБ:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## Бесплатные варианты — подробности

### 1. Yandex Cloud Functions (рекомендую) — 0 ₽
- Бесплатные лимиты каждый месяц: **1 000 000 вызовов** Cloud Functions + **10 GB×час** вычислений; личному боту нужно ~сотни–тысячи вызовов в месяц, т.е. запас в сотни раз.
- Схема: Telegram webhook → публичный URL функции → наш адаптер `serverless/yandex_handler.py` (aiogram 3, `Dispatcher.feed_update`) → GigaChat + Google Calendar.
- Работает из РФ, оплата не требуется вовсе (лимиты бесплатного потребления), но для активации облака нужен платёжный аккаунт.
- Минусы: холодный старт ~0.5–1.5 c (уложимся в 5 с из ТЗ), долгие операции (>120 c) невозможны, SQLite в функции эфемерный — подключите YDB Serverless (тоже бесплатный лимит) или храните состояние в файловой функции/секретах. Для простоты можно оставить SQLite и пересоздавать OAuth-токен.

### 2. PythonAnywhere (free) — 0 ₽, но ❌ не подходит
`api.telegram.org` в whitelist бесплатных аккаунтов, но исходящие запросы ограничены белым списком — **GigaChat (ngw.devices.sberbank.ru / gigachat.devices.sberbank.ru) там нет**, AI-парсинг не заработает. Плюс нет always-on процессов на бесплатном тарифе. Годится только для ботов без внешних API.

### 3. Google Apps Script webhook — 0 ₽, экзотика
Webhook-бот на Apps Script: квоты 90 мин/день и 20 000 UrlFetch-вызовов хватает личному боту, `CalendarApp` работает нативно, Google доступен из РФ. Минус: переписывать логику на JS — отклонение от стека ТЗ. Держим в уме как запасной аэродром.

### 4. Oracle Cloud Free Tier — ❌
Формально бесплатный VPS навсегда, но регистрация из РФ не поддерживается, нужна зарубежная карта — противоречит ТЗ. Render/Fly.io/Railway — та же история.

## Дешёвые платные VDS — сводная таблица

| Провайдер | Цена от | Конфигурация | Локации | Оплата | Источник |
|---|---|---|---|---|---|
| 4VPS.su | ~80–126 ₽ | 1 CPU/1 ГБ/10 ГБ NVMe | РФ + 35 стран | карты РФ | looking.center, обзоры 2026 |
| SprintHost | 91 ₽ | 1 vCPU/512 МБ/10 ГБ NVMe, 10 Гбит/с | Москва, СПб | СБП, МИР | vc.ru (авг 2026) |
| UltraVDS | 119 ₽ | мин. тариф | РФ | карты РФ | habr (май 2026) |
| Спринтбокс | 130 ₽ | 1 ядро/0,5 ГБ/10 ГБ NVMe | РФ | карты РФ | РБК (2025) |
| RuVDS | 139 ₽ | 1 ядро/512 МБ/10 ГБ HDD (SSD 209 ₽) | 20+ ДЦ | карты РФ, 3 дня trial | habr, easylinklife (2026) |
| VDSina | 150 ₽ | 1 vCPU/1 ГБ/NVMe, посуточная оплата | Москва, Амстердам | карты РФ | vc.ru, DTF (2026) |
| Beget | 210–330 ₽ | KVM, автобэкапы | РФ | карты РФ | DTF (авг 2026) |
| FirstVDS | 219 ₽ | 32 ТБ трафика | РФ, NL | карты РФ | DTF (май 2026) |
| hsvds.ru | ~186 ₽ | 1 ядро/1 ГБ/10 ГБ SSD | РФ | карты РФ | сайт |
| Timeweb Cloud | от ~477 ₽ | 1 vCPU/1 ГБ/15 ГБ NVMe | РФ, ЕС | карты РФ | vpsindex.ru (2026) |

## Чек-лист настройки VDS (после покупки)

```bash
apt update && apt upgrade -y
adduser bot && usermod -aG sudo bot          # не работаем под root
# swap, если RAM < 1 ГБ (см. выше)
apt install -y docker.io docker-compose-v2   # Docker
usermod -aG docker bot
# залить проект, заполнить .env
docker compose up -d --build
# автозапуск при ребуте уже даёт restart: unless-stopped
```

Задержки: ДЦ в Москве → ~1–2 мс до Telegram-серверов, ~40–60 мс до Google/GigaChat. Ответ бота в лимит 5 с из ТЗ укладывается с запасом.

## Источники

- Обзор дешёвых VPS, Хабр, май 2026: https://habr.com/en/articles/990310/
- ТОП дешёвых VPS 2026, vc.ru: https://vc.ru/top-raiting/2628070-luchshie-deshevye-vps-vds
- VPS с оплатой СБП, vc.ru, авг 2026: https://vc.ru/top-raiting/2879928-luchshie-vps-vds-s-oplatoi-sbp-v-rossii
- VPS для Telegram-ботов, DTF: https://dtf.ru/top-raiting/5207192-luchshie-vps-dlya-telegram-botov
- Тарифы 4VPS: https://looking.center/companies/4vps-su/virtual-servers
- Timeweb Cloud: https://vpsindex.ru/provider/timeweb
- Бесплатный Telegram-бот на Yandex Cloud: https://tproger.ru/articles/kak-besplatno-razvernut-telegram-bota-na-node-js-v-yandeks-oblake-pri-pomoshhi-frejmvorka-serverless
- Экономика serverless-бота (Хабр): https://habr.com/ru/articles/1040472/
- Whitelist PythonAnywhere: https://www.pythonanywhere.com/forums/topic/2674/

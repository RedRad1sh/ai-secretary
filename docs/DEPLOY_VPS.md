# 🚀 Деплой на свой VPS (Ubuntu + systemd + GitHub Actions)

Ручной деплой в один клик: **pull кода → зависимости → миграции → рестарт →
проверка**, плюс автоматический откат, если сервис не поднялся.

```
GitHub Actions (workflow «Deploy to VPS», запуск вручную)
        │  SSH (ключ из секрета VPS_SSH_KEY)
        ▼
VPS (Ubuntu)  /opt/ai-secretary
   1. git fetch + checkout нужного ref
   2. .venv: pip install -r requirements.txt
   3. scripts/migrate.py — миграции SQLite (data/bot.db + data/users/*.db)
   4. systemctl restart ai-secretary
   5. ждём «Бот запущен» в journalctl → успех / откат на прежний коммит
```

Никакого Docker: сервис живёт как systemd-юнит обычного пользователя, ~60–90 МБ
RSS на процесс. Так дешевле по ресурсам и проще, когда на одном VPS крутится
несколько приложений (см. [раздел 8](#8-несколько-приложений-на-одном-vps)).

## Что лежит в репозитории

| Файл | Роль |
|---|---|
| `.github/workflows/deploy.yml` | workflow ручного деплоя (секреты `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`) |
| `deploy/bootstrap.sh` | первичная настройка сервера: пакеты, код, venv, `.env`, юнит, sudo-правила |
| `deploy/remote-deploy.sh` | сам деплой (pull → зависимости → миграции → рестарт → проверка); запускается и из Actions, и руками |
| `deploy/ai-secretary.service` | шаблон systemd-юнита (`__APP_DIR__`, `__USER__` подставляет bootstrap) |
| `deploy/sudoers-ai-secretary` | минимум sudo-прав, нужный деплою (systemctl для своего юнита + journalctl) |
| `scripts/migrate.py`, `migrations/` | миграции схемы БД (идемпотентные, см. `migrations/README.md`) |

## 1. Что нужно

* **VPS с Ubuntu 22.04 / 24.04**, 1 vCPU, **1 ГБ RAM** (512 МБ — рабочий вариант,
  но добавьте swap 1–2 ГБ: `pip install` прожорлив), 10 ГБ диска.
  Дешёвые варианты — в [HOSTING.md](HOSTING.md).
* Пользователь для деплоя (**не root**), он же попадёт в секрет `VPS_USER`.
* Доступ сервера к GitHub: для публичного репозитория — ничего, для приватного —
  deploy key (см. [FAQ](#приватный-репозиторий-git-просит-логин)).

## 2. Первичная установка (один раз, ~10 минут)

```bash
ssh deploy@VPS_IP                # ваш пользователь из VPS_USER

# swap, если RAM ≤ 1 ГБ (иначе pip/OCR могут упасть по OOM)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# настройка: пакеты, код, venv, .env, systemd-юнит, sudo-правила
curl -fsSL https://raw.githubusercontent.com/RedRad1sh/ai-secretary/master/deploy/bootstrap.sh -o bootstrap.sh
bash bootstrap.sh
```

Скрипт идемпотентный (можно перезапускать) и делает:

1. `apt-get install`: `git`, `python3-venv`, `python3-pip`, `ffmpeg`, `tzdata`,
   `tesseract-ocr` (+ языки rus/eng — OCR фото);
2. `git clone` репозитория в `/opt/ai-secretary`;
3. `.venv` + `pip install -r requirements.txt`;
4. `.env` из `.env.example` с правами `600`;
5. `/etc/systemd/system/ai-secretary.service` и `/etc/sudoers.d/ai-secretary`
   (последний — только `systemctl` для своего юнита и `journalctl`), автозапуск;

Дальше заполните `.env` и запустите бота:

```bash
nano /opt/ai-secretary/.env      # BOT_TOKEN, ALLOWED_USER_IDS, LLM_*/GIGACHAT_*
sudo systemctl start ai-secretary
sudo journalctl -u ai-secretary -f     # ждём строку «Бот запущен»
```

Полезно знать:

```bash
sudo systemctl status ai-secretary            # состояние
sudo systemctl restart ai-secretary           # рестарт
sudo journalctl -u ai-secretary -n 100        # последние логи
sudo systemctl disable ai-secretary --now     # выключить и убрать из автозапуска
```

### Те же шаги вручную (если не запускать bootstrap)

```bash
sudo apt update && sudo apt install -y git python3-venv python3-pip ffmpeg tzdata \
    tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng
sudo mkdir -p /opt/ai-secretary && sudo chown "$USER:" /opt/ai-secretary
git clone https://github.com/RedRad1sh/ai-secretary.git /opt/ai-secretary
cd /opt/ai-secretary
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
# юнит и sudo-правила: подставьте свой каталог и пользователя
sudo sed -e "s|__APP_DIR__|$PWD|g" -e "s|__USER__|$USER|g" deploy/ai-secretary.service \
    | sudo tee /etc/systemd/system/ai-secretary.service >/dev/null
sudo sed -e "s|__USER__|$USER|g" -e "s|__SERVICE__|ai-secretary|g" deploy/sudoers-ai-secretary \
    | sudo tee /etc/sudoers.d/ai-secretary >/dev/null
sudo chmod 440 /etc/sudoers.d/ai-secretary && sudo visudo -c
sudo systemctl daemon-reload && sudo systemctl enable --now ai-secretary
sudo usermod -aG systemd-journal "$USER"   # читать логи без sudo (нужен ре-логин)
```

## 3. Секреты и переменные GitHub

**Settings → Secrets and variables → Actions → Secrets:**

| Секрет | Значение |
|---|---|
| `VPS_HOST` | IP или домен сервера |
| `VPS_USER` | пользователь деплоя (например, `deploy`) |
| `VPS_SSH_KEY` | **приватный** SSH-ключ целиком, вместе со строками `-----BEGIN/END-----` |

**Settings → Secrets and variables → Actions → Variables** (необязательные, если
каталог/юнит/порт нестандартные):

| Переменная | По умолчанию |
|---|---|
| `VPS_PORT` | `22` |
| `VPS_APP_DIR` | `/opt/ai-secretary` |
| `VPS_SERVICE` | `ai-secretary` |

Отдельный ключ только для деплоя (не переиспользуйте личный):

```bash
# на локальной машине
ssh-keygen -t ed25519 -C "github-actions ai-secretary" -f ~/.ssh/ai-secretary-deploy -N ""
ssh-copy-id -i ~/.ssh/ai-secretary-deploy.pub deploy@VPS_IP
ssh -i ~/.ssh/ai-secretary-deploy deploy@VPS_IP 'echo ok'    # проверка
cat ~/.ssh/ai-secretary-deploy                                # → целиком в секрет VPS_SSH_KEY
```

## 4. Деплой

**Actions → «Deploy to VPS» → Run workflow:**

* `ref` — ветка, тег или коммит (`master`, `v1.2.3`, `a1b2c3d`). По умолчанию `master`;
* `restart_only` — только перезапустить сервис, без `git pull` и `pip install`
  (когда код не менялся, а боту нужно «перезагрузиться»).

Работа занимает ~40–60 с и печатает шаги прямо в логе, включая `journalctl` хвост.
Если сервис не поднялся — код автоматически откатывается на прежний коммит, а
workflow падает красным (в логе видно причину).

**Ручной деплой с сервера** — тот же скрипт, что вызывает Actions:

```bash
cd /opt/ai-secretary
bash deploy/remote-deploy.sh                 # деплой master
REF=v1.2.3 bash deploy/remote-deploy.sh      # конкретный тег/коммит
RESTART_ONLY=true bash deploy/remote-deploy.sh
DRY_RUN=true bash deploy/remote-deploy.sh    # показать план, ничего не менять
```

Полезные переменные скрипта: `APP_DIR`, `SERVICE`, `REF`, `RESTART_ONLY`,
`GIT_CLEAN` (снести незакоммиченные файлы перед запуском), `HEALTH_TIMEOUT`
(сколько секунд ждать «Бот запущен», по умолчанию 60), `HEALTH_PATTERN`,
`ROLLBACK_ON_FAILURE=false` (выключить авто-откат), `DRY_RUN`.

Как включить автодеплой по push (если захотите): в `deploy.yml` добавьте

```yaml
on:
  push:
    branches: [master]
  workflow_dispatch: ...
```

## 5. Миграции БД

Шаг «миграции» стоит между установкой зависимостей и рестартом и выполняет
`scripts/migrate.py`:

* создаёт отсутствующие таблицы (базовая схема из `bot/db.py`);
* применяет `migrations/*.sql` по одному разу — учёт в таблице
  `schema_migrations`, каждый файл — в своей транзакции (упавшая миграция не
  «полуприменится»);
* делает это для `data/bot.db` и всех `data/users/<ID>.db`.

Когда меняете схему — добавьте файл `migrations/000N_название.sql` и закоммитьте
вместе с кодом: деплой применит его автоматически. Правила и примеры —
[migrations/README.md](../migrations/README.md).

```bash
# локально потренироваться на копии базы
cp data/bot.db /tmp/probe.db
.venv/bin/python scripts/migrate.py --data-dir /tmp/probe --dry-run
.venv/bin/python scripts/migrate.py --data-dir /tmp/probe
python tests/test_migrations.py     # 20 проверок: идемпотентность, откат, dry-run
```

Бэкап перед рискованными изменениями схемы:

```bash
cd /opt/ai-secretary && .venv/bin/python scripts/backup.py /var/backups/ai-secretary
# ежедневно в 4:00 (crontab -e)
0 4 * * * cd /opt/ai-secretary && .venv/bin/python scripts/backup.py /var/backups/ai-secretary
```

## 6. Откат

* **Автоматически** — если после рестарта нет строки «Бот запущен»/сервис не
  активен, деплой вернёт прежний коммит и перезапустит сервис.
* **Вручную** — выберите прежний коммит в качестве `ref` в workflow либо:

```bash
cd /opt/ai-secretary
git log --oneline -10
REF=<прежний-sha> bash deploy/remote-deploy.sh
```

Миграции откат **не** разворачивает: поэтому пишите их совместимыми со старой
версией кода (добавлять колонки/таблицы, а не удалять).

## 7. Если что-то не работает

| Симптом в логе | Причина и что делать |
|---|---|
| `sudo: a password is required` | нет `/etc/sudoers.d/ai-secretary`: `sed`-подстановка + `visudo -c` из [раздела 2](#2-первичная-установка-один-раз-10-минут) |
| `Нет git-репозитория в /opt/ai-secretary` | сервер не настроен — прогоните `deploy/bootstrap.sh` |
| `нет deploy/remote-deploy.sh` | код на сервере старее деплой-кита: `git fetch --force origin && git checkout --force --detach origin/master` или перезапустите bootstrap |
| `ssh: handshake failed: unable to authenticate` | не тот ключ в `VPS_SSH_KEY` / не тот `VPS_USER`; проверьте локально `ssh -i ~/.ssh/ai-secretary-deploy VPS_USER@VPS_HOST` |
| Деплой «зависает» на `pip install` | мало RAM (уходит в своп) — добавьте swap или тариф с 1 ГБ |
| Лог: `BOT_TOKEN не задан` | не заполнен `/opt/ai-secretary/.env` |
| Лог: `Conflict: terminated by other getUpdates` | запущен второй экземпляр бота (локальный или Netrun/YC) — одновременно допустим только один long polling |
| `journalctl` показывает пустоту / `No journal files` | пользователь не в группе `systemd-journal` (нужен ре-логин по SSH) |
| Сервис `active`, но строки «Бот запущен» нет | бот не успел/другой формат лога — увеличьте `HEALTH_TIMEOUT` или задайте `HEALTH_PATTERN` |
| Бот отвечает, но OCR/голос падают | не установлены `tesseract-ocr*`/`ffmpeg` (bootstrap ставит их; при `SKIP_OCR=true` — нет) |

### Приватный репозиторий: `git` просит логин

Серверу нужен доступ на чтение. Проще всего deploy key:

```bash
# на сервере от пользователя деплоя
ssh-keygen -t ed25519 -C "vps ai-secretary" -f ~/.ssh/ai-secretary -N ""
cat ~/.ssh/ai-secretary.pub
# GitHub → репозиторий → Settings → Deploy keys → Add deploy key (read-only, без write)
# и переключите remote на SSH:
cd /opt/ai-secretary && git remote set-url origin git@github.com:RedRad1sh/ai-secretary.git
ssh -T git@github.com        # проверка
```

## 8. Несколько приложений на одном VPS

Схема «один каталог + один systemd-юнит на приложение» масштабируется до
нескольких сервисов без оркестратора:

```
/opt/ai-secretary   →  ai-secretary.service      (этот репозиторий)
/opt/bot-two        →  bot-two.service           (второй бот)
/opt/web-app        →  web-app.service           (веб-морда + nginx/Caddy)
```

Второе приложение = второй прогон bootstrap с другими `APP_DIR`/`SERVICE`
(в workflow — переменные `VPS_APP_DIR`/`VPS_SERVICE`, либо копия workflow
с собственным именем и значениями). Пользователя деплоя можно держать одного:
шаблон sudoers выдаёт права только на юнит своего приложения.

Чтобы приложения не мешали друг другу, закрепите лимиты в юните
(`MemoryMax=512M`, `CPUQuota=80%`) и смотрите потребление через `systemd-cgtop`.

### Docker или systemd?

| Вариант | Накладные расходы | Когда оправдан |
|---|---|---|
| **systemd + venv** (этот гайд) | ~0: только процесс бота и его venv на диске | 2–10 сервисов без конфликтов версий системных библиотек — ваш случай |
| **Docker / docker compose** (в репозитории есть `Dockerfile` и `docker-compose.yml`) | демон ~100–150 МБ RSS + образ `python:3.12-slim` ~120 МБ (общий для всех контейнеров) | разные версии Python/системных библиотек, нужна воспроизводимость окружения, готовые образы с tesseract/ffmpeg |
| **Dokploy / Coolify** (self-hosted PaaS поверх Docker) | Docker + сам PaaS (в покое 300–600 МБ), комфортно на 2 ГБ RAM | хочется веб-UI, деплой из Git по кнопке, TLS и логи «как в Vercel» |
| **k3s / Kubernetes / Nomad** | от ~500 МБ + порог входа | несколько серверов/кластер; для одного VPS — оверкилл |

Практический вывод: **пока приложений единицы и они на Python — оставайтесь на
systemd**; Docker включайте, когда появится зоопарк версий или понадобится
контейнерный CI. Если всё-таки перейдёте на Docker, в деплое меняется один шаг:

```bash
# вместо systemctl + venv в remote-deploy.sh
cd /opt/ai-secretary && sudo docker compose up -d --build
```

### Если появятся вебхуки/веб-морды

Боту для long polling порт не нужен вообще; веб-приложениям — нужен reverse proxy.
Caddy удобнее: сам получает сертификаты Let's Encrypt.

```caddy
# /etc/caddy/Caddyfile — несколько приложений на одном сервере
bot-two.example.com  { reverse_proxy 127.0.0.1:8081 }
web.example.com      { reverse_proxy 127.0.0.1:8082 }
```

`sudo apt install caddy && sudo systemctl reload caddy`; приложения слушают
`127.0.0.1:<порт>`, наружу торчит только 80/443.

## 9. Базовая гигиена сервера

```bash
sudo apt install -y unattended-upgrades fail2ban && sudo dpkg-reconfigure unattended-upgrades
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
sudo -u deploy crontab -e      # ежедневный бэкап (см. раздел 5)
```

## 10. Чек-лист

- [ ] сервер: Ubuntu, swap при RAM ≤ 1 ГБ, `bootstrap.sh` прогнан
- [ ] `/opt/ai-secretary/.env` заполнен, права `600`, `systemctl status` — active
- [ ] в GitHub: секреты `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` (при необходимости — переменные `VPS_PORT`/`VPS_APP_DIR`/`VPS_SERVICE`)
- [ ] `sudo -n systemctl is-active ai-secretary` на сервере проходит без пароля
- [ ] workflow «Deploy to VPS» прогнан вручную: зелёный, в логе «Готово: ai-secretary работает»
- [ ] бэкапы по cron + проверено восстановление из бэкапа
- [ ] один экземпляр long polling (локальный бот/Netrun/YC остановлены)

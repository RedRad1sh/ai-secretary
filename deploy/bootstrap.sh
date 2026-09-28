#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Первичная настройка Ubuntu-VPS под ai-secretary (Docker не нужен).
#
#  Что делает (идемпотентно, можно запускать повторно):
#    1. ставит системные пакеты: git, python3-venv/pip, ffmpeg, tesseract (OCR);
#    2. клонирует репозиторий в APP_DIR (или обновляет существующий);
#    3. создаёт venv и ставит зависимости из requirements.txt;
#    4. создаёт .env из .env.example (если файла ещё нет);
#    5. ставит systemd-юнит и правила sudo для деплоя;
#    6. включает автозапуск и (если .env заполнен) запускает бота.
#
#  Запуск на сервере от пользователя деплоя (он же VPS_USER в секретах GitHub),
#  у которого есть sudo с паролем:
#
#     curl -fsSL https://raw.githubusercontent.com/RedRad1sh/ai-secretary/master/deploy/bootstrap.sh -o bootstrap.sh
#     bash bootstrap.sh                 # или из уже склонированного репозитория
#
#  Переменные (необязательные):
#     APP_DIR      /opt/ai-secretary
#     SERVICE      ai-secretary
#     REPO_URL     https://github.com/RedRad1sh/ai-secretary.git
#     REF          master          ветка/тег для первой установки
#     DEPLOY_USER  $(id -un)
#     SKIP_OCR     true            не ставить tesseract (экономия ~60 МБ)
#     SKIP_PACKAGES true           не трогать apt (пакеты уже стоят)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ai-secretary}"
SERVICE="${SERVICE:-ai-secretary}"
REPO_URL="${REPO_URL:-https://github.com/RedRad1sh/ai-secretary.git}"
REF="${REF:-master}"
DEPLOY_USER="${DEPLOY_USER:-$(id -un)}"
SKIP_OCR="${SKIP_OCR:-false}"
SKIP_PACKAGES="${SKIP_PACKAGES:-false}"

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   ⚠️  %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }
is_true() { [[ "${1,,}" == "true" || "$1" == "1" || "${1,,}" == "yes" ]]; }

[[ "$APP_DIR" == /* ]] || die "APP_DIR должен быть абсолютным путём: $APP_DIR"
command -v apt-get >/dev/null || die "Скрипт рассчитан на Ubuntu/Debian (нужен apt-get)."
command -v python3 >/dev/null || die "python3 не найден."

log "Проверка окружения"
info "пользователь: $DEPLOY_USER"
info "каталог:      $APP_DIR"
info "сервис:       $SERVICE"
info "python3:      $(python3 --version)"

sudo -v || die "Нужен sudo (на первичную настройку — с паролем)."

PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"
[[ "$PY_OK" == "1" ]] || die "Нужен Python 3.11+ (сейчас $(python3 --version)). На Ubuntu 22.04+ подойдёт python3 из системы."

# ── 1. пакеты ────────────────────────────────────────────────────────────────
if ! is_true "$SKIP_PACKAGES"; then
  log "1/6 Системные пакеты"
  PKGS=(git python3-venv python3-pip ffmpeg tzdata)
  if ! is_true "$SKIP_OCR"; then
    PKGS+=(tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng)
  fi
  sudo apt-get update -y
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PKGS[@]}"
else
  log "1/6 Пакеты: пропущено (SKIP_PACKAGES=true)"
fi

# ── 2. код ───────────────────────────────────────────────────────────────────
log "2/6 Код в $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
  info "репозиторий уже есть — обновляю ссылку и код"
  git -C "$APP_DIR" remote set-url origin "$REPO_URL"
  git -C "$APP_DIR" fetch --prune --tags --force origin
else
  sudo mkdir -p "$APP_DIR"
  sudo chown "$DEPLOY_USER:" "$APP_DIR"
  # Каталог должен быть пустым, иначе git clone откажется.
  if [[ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
    die "$APP_DIR не пуст и не является git-репозиторием. Освободите каталог или укажите другой APP_DIR."
  fi
  git clone "$REPO_URL" "$APP_DIR"
fi
TARGET="origin/$REF"
git -C "$APP_DIR" rev-parse --verify --quiet "$TARGET^{commit}" >/dev/null || TARGET="$REF"
git -C "$APP_DIR" checkout --force --detach "$TARGET"
info "развёрнут: $(git -C "$APP_DIR" log -1 --oneline)"

# ── 3. venv и зависимости ────────────────────────────────────────────────────
log "3/6 Виртуальное окружение и зависимости"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"
info "готово: $APP_DIR/.venv"

# ── 4. .env ──────────────────────────────────────────────────────────────────
log "4/6 .env"
if [[ -f "$APP_DIR/.env" ]]; then
  info ".env уже есть — не трогаю"
else
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  info "создан $APP_DIR/.env (шаблон) — заполните BOT_TOKEN, ALLOWED_USER_IDS и LLM_*/GIGACHAT_*"
fi

# ── 5. systemd + sudoers ─────────────────────────────────────────────────────
log "5/6 systemd-юнит и права деплоя"
sudo sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$DEPLOY_USER|g" \
  "$APP_DIR/deploy/ai-secretary.service" \
  | sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null

# Сначала проверяем синтаксис на временном файле: битый файл в /etc/sudoers.d
# ломает sudo полностью, поэтому кладём его только после успешной проверки.
tmp_sudoers="$(mktemp)"
trap 'rm -f "$tmp_sudoers"' EXIT
sed -e "s|__USER__|$DEPLOY_USER|g" -e "s|__SERVICE__|$SERVICE|g" \
  "$APP_DIR/deploy/sudoers-ai-secretary" > "$tmp_sudoers"
sudo visudo -cf "$tmp_sudoers" >/dev/null \
  || die "Правила sudo не прошли проверку ($tmp_sudoers). Установите их вручную: sudo visudo -f /etc/sudoers.d/$SERVICE"
sudo install -m 440 -o root -g root "$tmp_sudoers" "/etc/sudoers.d/$SERVICE"
rm -f "$tmp_sudoers"
trap - EXIT
# sudo-visudo читает /etc/sudoers и все drop-in'ы: убеждаемся, что система цела.
sudo visudo -c >/dev/null || die "Проверка sudo не проходит — посмотрите вывод: sudo visudo -c"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null
# Чтение журнала без sudo (применится при следующем входе по SSH).
sudo usermod -aG systemd-journal "$DEPLOY_USER" || warn "не удалось добавить в группу systemd-journal"
info "юнит: /etc/systemd/system/$SERVICE.service; автозапуск включён"

# ── 6. первый запуск ─────────────────────────────────────────────────────────
log "6/6 Первый запуск"
# В .env.example лежит плейсхолдер вида «123456789:AAF...your-bot-token»,
# поэтому «заполнено» = непустой BOT_TOKEN, отличный от значения из шаблона.
placeholder="$(grep -m1 '^BOT_TOKEN=' "$APP_DIR/.env.example" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')"
token="$(grep -m1 '^BOT_TOKEN=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')"
if [[ -n "$token" && "$token" != "$placeholder" ]]; then
  if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
    warn "BOT_TOKEN не похож на токен Telegram (ожидается «цифры:AA...») — проверьте значение"
  fi
  sudo systemctl restart "$SERVICE"
  sleep 3
  if sudo systemctl is-active --quiet "$SERVICE"; then
    info "✅ Сервис работает. Журнал: sudo journalctl -u $SERVICE -f"
  else
    warn "Сервис не поднялся — смотрите: sudo journalctl -u $SERVICE -n 50"
  fi
else
  warn "BOT_TOKEN в .env пуст (или остался плейсхолдер из .env.example) — бот не запущен."
  info "Заполните $APP_DIR/.env и запустите: sudo systemctl start $SERVICE"
fi

log "Дальше"
cat <<EOF
   1. Заполните $APP_DIR/.env (BOT_TOKEN, ALLOWED_USER_IDS, LLM_*/GIGACHAT_*).
   2. Проверьте: sudo systemctl status $SERVICE && sudo journalctl -u $SERVICE -f
   3. Пропишите секреты в GitHub: VPS_HOST, VPS_USER ($DEPLOY_USER), VPS_SSH_KEY
      и запустите workflow «Deploy to VPS» — дальше деплой делается из Actions.
   4. Настройте бэкапы: (crontab -l; echo '0 4 * * * cd $APP_DIR && .venv/bin/python scripts/backup.py /var/backups/ai-secretary') | crontab -
   Подробности и разбор ошибок: docs/DEPLOY_VPS.md
EOF

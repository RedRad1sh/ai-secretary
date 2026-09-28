#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Деплой ai-secretary на VPS (Ubuntu + systemd, без Docker).
#
#  Схема: git pull → venv + зависимости → миграции → рестарт → health-check.
#  Запускается из GitHub Actions (.github/workflows/deploy.yml) или вручную
#  прямо на сервере:
#
#     cd /opt/ai-secretary
#     bash deploy/remote-deploy.sh                      # деплой ветки master
#     REF=v1.2.3 bash deploy/remote-deploy.sh           # деплой тега/коммита
#     RESTART_ONLY=true bash deploy/remote-deploy.sh    # только перезапуск
#     DRY_RUN=true bash deploy/remote-deploy.sh         # показать план, не менять
#
#  Переменные (все необязательные, значения по умолчанию — для одного VPS):
#     APP_DIR   каталог приложения            /opt/ai-secretary
#     SERVICE   имя systemd-юнита             ai-secretary
#     REF       ветка / тег / коммит          master
#     RESTART_ONLY  true — не делать pull и pip install, только рестарт
#     GIT_CLEAN true — убрать незакоммиченные файлы (git clean -fd) перед запуском
#     HEALTH_TIMEOUT   секунд ожидания «Бот запущен»   60
#     HEALTH_PATTERN   строка в журнале = успех         «Бот запущен»
#     ROLLBACK_ON_FAILURE  true — вернуться на прежний коммит, если сервис не поднялся
#     DRY_RUN   true — печатать команды, ничего не выполнять
#
#  Права: для systemctl нужен NOPASSWD-sudo (deploy/sudoers-ai-secretary),
#  для чтения журнала — членство пользователя в группе systemd-journal.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ai-secretary}"
SERVICE="${SERVICE:-ai-secretary}"
REF="${1:-${REF:-master}}"
RESTART_ONLY="${2:-${RESTART_ONLY:-false}}"
GIT_CLEAN="${GIT_CLEAN:-false}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
HEALTH_PATTERN="${HEALTH_PATTERN:-Бот запущен}"
ROLLBACK_ON_FAILURE="${ROLLBACK_ON_FAILURE:-true}"
DRY_RUN="${DRY_RUN:-false}"

VENV_PY="$APP_DIR/.venv/bin/python"

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   ⚠️  %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

# Выполнить команду (или напечатать её в dry-run).
run() {
  if [[ "$DRY_RUN" == "true" ]]; then
    printf '   [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# Последние строки журнала юнита: сначала напрямую, иначе через sudo.
logs() {
  local args=("$@")
  journalctl -u "$SERVICE" ${args[@]+"${args[@]}"} --no-pager 2>/dev/null \
    || sudo -n journalctl -u "$SERVICE" ${args[@]+"${args[@]}"} --no-pager
}

# Булевы значения приходят и из env, и из GitHub Actions («true»/«false»).
is_true() { [[ "${1,,}" == "true" || "$1" == "1" || "${1,,}" == "yes" ]]; }

log "Деплой ai-secretary"
info "каталог:  $APP_DIR"
info "сервис:   $SERVICE"
info "ref:      $REF (restart_only=$RESTART_ONLY, dry_run=$DRY_RUN)"

[[ "$APP_DIR" == /* ]] || die "APP_DIR должен быть абсолютным путём, получено: $APP_DIR"
[[ -d "$APP_DIR/.git" ]] || die "В $APP_DIR нет git-репозитория. Первичная установка: docs/DEPLOY_VPS.md (bash deploy/bootstrap.sh)."
[[ "$REF" =~ ^[A-Za-z0-9._/@-]{1,150}$ ]] || die "Недопустимый REF: '$REF' (разрешены буквы, цифры, . _ / @ -)"
[[ "$SERVICE" =~ ^[A-Za-z0-9_.@-]{1,64}$ ]] || die "Недопустимый SERVICE: '$SERVICE'"

if ! sudo -n true 2>/dev/null; then
  die "Нет sudo без пароля для $(id -un). Установите правила деплоя: deploy/sudoers-ai-secretary (см. docs/DEPLOY_VPS.md)."
fi

cd "$APP_DIR"
PREV="$(git rev-parse --short HEAD)"
info "текущий коммит: $PREV"

# ── Шаги 1–2: код и зависимости ──────────────────────────────────────────────
if ! is_true "$RESTART_ONLY"; then
  log "1/5 Код: fetch + checkout $REF"
  run git fetch --prune --tags --force origin

  TARGET="$REF"
  if git rev-parse --verify --quiet "origin/$REF^{commit}" >/dev/null; then
    TARGET="origin/$REF"
  elif ! git rev-parse --verify --quiet "$REF^{commit}" >/dev/null; then
    die "В репозитории нет ref '$REF' (ни ветки, ни тега, ни коммита)."
  fi
  run git checkout --force --detach "$TARGET"
  if is_true "$GIT_CLEAN"; then
    run git clean -fdq
  fi
  info "развёрнут коммит: $(git log -1 --oneline)"

  log "2/5 Зависимости (venv)"
  if [[ ! -x "$VENV_PY" ]]; then
    info "venv не найден — создаю $APP_DIR/.venv"
    run python3 -m venv "$APP_DIR/.venv"
  fi
  run "$VENV_PY" -m pip install --quiet --upgrade pip
  run "$VENV_PY" -m pip install --quiet -r "$APP_DIR/requirements.txt"
else
  log "1–2/5 Пропущены: restart_only=$RESTART_ONLY"
fi

# ── Шаг 3: остановка сервиса (чтобы миграции шли без второго писателя) ────────
log "3/5 Остановка $SERVICE"
run sudo -n systemctl stop "$SERVICE" || true

# ── Шаг 4: миграции ──────────────────────────────────────────────────────────
log "4/5 Миграции БД"
if [[ -f "$APP_DIR/scripts/migrate.py" ]]; then
  run "$VENV_PY" "$APP_DIR/scripts/migrate.py"
else
  info "scripts/migrate.py нет — пропускаю (схема создаётся при старте бота)"
fi

# ── Шаг 5: запуск + проверка ─────────────────────────────────────────────────
log "5/5 Запуск и проверка"
if [[ "$DRY_RUN" == "true" ]]; then
  info "[dry-run] sudo -n systemctl start $SERVICE"
  info "[dry-run] ожидание «$HEALTH_PATTERN» в journalctl -u $SERVICE (до ${HEALTH_TIMEOUT} с)"
  info "Ничего не изменено (DRY_RUN=true)."
  exit 0
fi

START_TS="$(date '+%Y-%m-%d %H:%M:%S')"
if ! sudo -n systemctl start "$SERVICE"; then
  die "Не удалось запустить $SERVICE. Проверьте, что юнит установлен и доступен по sudo: systemctl status $SERVICE (установка — deploy/bootstrap.sh)."
fi

ok=false
deadline=$((SECONDS + HEALTH_TIMEOUT))
while ((SECONDS < deadline)); do
  if sudo -n systemctl is-active "$SERVICE" >/dev/null 2>&1 \
     && logs --since "$START_TS" | grep -q -- "$HEALTH_PATTERN"; then
    ok=true
    break
  fi
  sleep 3
done

if [[ "$ok" != "true" ]]; then
  printf '\n❌ Сервис не поднялся за %s с (или в журнале нет «%s»).\n' "$HEALTH_TIMEOUT" "$HEALTH_PATTERN"
  sudo -n systemctl status "$SERVICE" --no-pager -l | tail -30 || true
  TAIL="$(logs -n 60 || true)"
  printf '%s\n' "$TAIL"
  if grep -qi "BOT_TOKEN" <<<"$TAIL"; then
    warn "Похоже, не заполнен $APP_DIR/.env (BOT_TOKEN) — заполните и запустите деплой снова."
  fi
  if grep -q "Address already in use\|Conflict: terminated by other getUpdates" <<<"$TAIL"; then
    warn "Второй экземпляр бота уже слушает Telegram (long polling) — остановите его и повторите."
  fi

  if is_true "$ROLLBACK_ON_FAILURE" && ! is_true "$RESTART_ONLY"; then
    log "↩️ Откат на прежний коммит $PREV"
    if git checkout --force --detach "$PREV" && sudo -n systemctl restart "$SERVICE"; then
      info "Код откачен на $PREV, сервис перезапущен. Проверьте журнал: sudo journalctl -u $SERVICE -n 50"
    else
      warn "Откат не удался — разбирайтесь руками (журнал выше)."
    fi
  fi
  die "Деплой не удался."
fi

info "✅ Готово: $SERVICE работает, коммит $(git rev-parse --short HEAD), «$HEALTH_PATTERN» в журнале."
info "Журнал: sudo journalctl -u $SERVICE -f"

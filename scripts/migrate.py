#!/usr/bin/env python3
"""Идемпотентные миграции схемы SQLite — шаг деплоя между pull и рестартом.

Зачем отдельный скрипт: деплой — это «pull кода + перезапуск». Если новая
версия меняет схему БД, одного рестарта мало, а миграции должны выполняться
ровно один раз и ничего не ломать при повторном запуске (в том числе на пустой
базе и на базе, которую ведёт старая версия бота).

Как работает:
  1. Базовая схема из `bot/db.py` (`CREATE TABLE IF NOT EXISTS ...`) — создаёт
     отсутствующие таблицы и никогда не трогает существующие данные.
  2. Файлы `migrations/*.sql` в порядке возрастания имени: каждый применяется
     ровно один раз, факт применения пишется в таблицу `schema_migrations`
     в одной транзакции с самим SQL (упавшая миграция не «полуприменится»).
  3. Пункты 1–2 выполняются для основной базы (`data/bot.db`) и для всех
     пользовательских (`data/users/<ID>.db`).

Запуск (перед рестартом сервиса; лучше при остановленном боте):
    .venv/bin/python scripts/migrate.py                 # применить всё новое
    .venv/bin/python scripts/migrate.py --dry-run       # показать план
    .venv/bin/python scripts/migrate.py --data-dir /tmp/test-data

Файлы миграций пишутся вручную и коммитятся в репозиторий: см. migrations/README.md.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from bot.config import load_config  # noqa: E402
from bot.db import _SCHEMA  # noqa: E402  # единый источник истины базовой схемы

MIGRATIONS_DIR = BASE_DIR / "migrations"


def _sql_files(directory: Path) -> list[Path]:
    """Миграции в порядке применения: 0001_..., 0002_... (сортировка по имени)."""
    return sorted(p for p in directory.glob("*.sql") if p.is_file())


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL DEFAULT (datetime('now'))
           );"""
    )


def _applied(conn: sqlite3.Connection) -> set[str]:
    try:
        return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    except sqlite3.OperationalError:
        return set()  # таблицы учёта ещё нет — значит, не применено ничего


def _apply(conn: sqlite3.Connection, path: Path) -> None:
    """Применить один файл и отметить его — атомарно."""
    version = path.name.replace("'", "''")
    body = path.read_text(encoding="utf-8").rstrip().rstrip(";")
    conn.executescript(
        f"BEGIN;\n{body};\n"
        f"INSERT INTO schema_migrations (version) VALUES ('{version}');\n"
        "COMMIT;"
    )


def migrate_db(db_path: Path, files: list[Path], *, dry_run: bool = False) -> list[str]:
    """Приводит одну базу к актуальной схеме; возвращает применённые миграции."""
    if dry_run and not db_path.exists():
        print(f"== {db_path}")
        print(f"   базы нет — будет создана; уже применено: 0, новых: {len(files)}")
        for path in files:
            print(f"   [dry-run] будет применена: {path.name}")
        return [f.name for f in files]

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: транзакциями управляем сами (BEGIN/COMMIT в _apply).
    uri = f"file:{db_path}?mode=ro" if dry_run else str(db_path)
    conn = sqlite3.connect(uri, uri=dry_run, isolation_level=None)
    try:
        if not dry_run:
            conn.executescript(_SCHEMA)
            _ensure_migrations_table(conn)
        done = _applied(conn)
        pending = [f for f in files if f.name not in done]

        print(f"== {db_path}")
        print(f"   базовая схема {'проверена' if not dry_run else 'не менялась'}; "
              f"уже применено: {len(done)}, новых: {len(pending)}")
        for path in pending:
            if dry_run:
                print(f"   [dry-run] будет применена: {path.name}")
            else:
                _apply(conn, path)
                print(f"   ✅ применена: {path.name}")
        return [f.name for f in pending]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Идемпотентные миграции SQLite для ai-secretary",
    )
    parser.add_argument("--data-dir", type=Path,
                        help="каталог данных (по умолчанию из .env / BASE_DIR/data)")
    parser.add_argument("--migrations-dir", type=Path, default=MIGRATIONS_DIR,
                        help="каталог с *.sql (по умолчанию migrations/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, что будет применено")
    args = parser.parse_args(argv)

    if args.data_dir:
        data_dir = args.data_dir
        db_path = data_dir / "bot.db"
    else:
        cfg = load_config()
        data_dir, db_path = cfg.data_dir, cfg.db_path

    files = _sql_files(args.migrations_dir)
    print(f"Каталог миграций: {args.migrations_dir} — {len(files)} файл(ов)"
          + (", dry-run" if args.dry_run else ""))

    targets = [db_path, *sorted((data_dir / "users").glob("*.db"))]
    print(f"Баз для обработки: {len(targets)}"
          + (f" (+ {len(targets) - 1} пользовательских)" if len(targets) > 1 else ""))

    applied = 0
    for target in targets:
        applied += len(migrate_db(target, files, dry_run=args.dry_run))

    if args.dry_run:
        print(f"\nИтог: план без изменений, к применению {applied} миграций.")
    elif applied:
        print(f"\nИтог: применено {applied} миграций.")
    else:
        print("\nИтог: схема уже актуальна, миграций не требовалось.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

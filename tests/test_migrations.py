"""Тесты миграций: scripts/migrate.py (шаг деплоя на VPS).

Проверяем то, от чего зависит безопасность деплоя:
  * базовая схема создаётся на пустой базе и данные не теряются;
  * migrations/*.sql применяются по одному разу (учёт в schema_migrations);
  * повторный прогон — no-op (идемпотентность);
  * пользовательские базы data/users/<ID>.db мигрируют вместе с основной;
  * --dry-run ничего не меняет;
  * упавшая миграция не «полуприменяется» и не помечается применённой.

Запуск: python tests/test_migrations.py   (сеть не нужна)
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MIGRATE = BASE_DIR / "scripts" / "migrate.py"
REAL_MIGRATIONS = BASE_DIR / "migrations"

OK, FAIL = "✅", "❌"
passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"{OK} {name}")
    else:
        failed += 1
        print(f"{FAIL} {name} {extra}")


def run_migrate(data_dir: Path, migrations_dir: Path | None = None,
                *extra: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(MIGRATE), "--data-dir", str(data_dir)]
    if migrations_dir is not None:
        cmd += ["--migrations-dir", str(migrations_dir)]
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR)


def tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}


def scalar(db_path: Path, sql: str, params: tuple = ()):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchone()[0]


tmp = Path(tempfile.mkdtemp(prefix="ai-secretary-migrations-"))

# ---------- 1. пустой каталог: базовая схема + реальные миграции ----------

data_dir = tmp / "fresh"
res = run_migrate(data_dir)
db_path = data_dir / "bot.db"
check("свежая база: код возврата 0", res.returncode == 0, res.stderr[-400:])
check("свежая база: bot.db создана", db_path.exists())
check("свежая база: таблицы бота на месте",
      {"kv", "events", "reqlog", "reminders"} <= tables(db_path),
      extra=str(sorted(tables(db_path))))
check("свежая база: таблица учёта миграций создана", "schema_migrations" in tables(db_path))

real_files = sorted(p.name for p in REAL_MIGRATIONS.glob("*.sql"))
check("реальные миграции применены",
      all(scalar(db_path, "SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (n,)) == 1
          for n in real_files) and bool(real_files),
      extra=f"файлов: {len(real_files)}")
check("индексы из 0001_indexes.sql созданы",
      scalar(db_path, "SELECT COUNT(*) FROM sqlite_master "
                      "WHERE type = 'index' AND name = 'idx_reminders_due'") == 1)

# ---------- 2. повторный прогон — no-op ----------

before = scalar(db_path, "SELECT COUNT(*) FROM schema_migrations")
res2 = run_migrate(data_dir)
check("повторный прогон: код возврата 0", res2.returncode == 0, res2.stderr[-400:])
check("повторный прогон: «уже актуальна»", "уже актуальна" in res2.stdout, extra=res2.stdout[-200:])
check("повторный прогон: новых записей нет",
      scalar(db_path, "SELECT COUNT(*) FROM schema_migrations") == before)

# ---------- 3. данные существующей базы не теряются ----------

with sqlite3.connect(db_path) as conn:
    conn.execute("INSERT INTO events (calendar_id, event_id, title, start_iso, end_iso) "
                 "VALUES ('primary', 'e1', 'Встреча', '2026-09-28T10:00:00+03:00', NULL)")
    conn.commit()
run_migrate(data_dir)
check("данные сохраняются после миграций",
      scalar(db_path, "SELECT title FROM events WHERE event_id = 'e1'") == "Встреча")

# ---------- 4. пользовательские базы ----------

users_dir = data_dir / "users"
users_dir.mkdir(exist_ok=True)
for uid in ("111", "222"):
    sqlite3.connect(users_dir / f"{uid}.db").close()
res3 = run_migrate(data_dir)
check("пользовательские базы мигрируют",
      res3.returncode == 0
      and all({"events", "reminders", "schema_migrations"} <= tables(users_dir / f"{uid}.db")
              for uid in ("111", "222")),
      extra=res3.stderr[-400:])

# ---------- 5. идемпотентность и порядок на кастомных миграциях ----------

custom = tmp / "custom-migrations"
custom.mkdir()
(custom / "0001_first.sql").write_text(
    "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, note TEXT);\n"
    "INSERT INTO probe (note) VALUES ('first');\n", encoding="utf-8")
(custom / "0002_second.sql").write_text(
    "CREATE TABLE IF NOT EXISTS probe2 (id INTEGER PRIMARY KEY);\n", encoding="utf-8")

data2 = tmp / "custom"
res4 = run_migrate(data2, custom)
check("кастомные миграции: код возврата 0", res4.returncode == 0, res4.stderr[-400:])
check("кастомные миграции: 0001 и 0002 применены",
      res4.stdout.find("0001_first.sql") < res4.stdout.find("0002_second.sql")
      and "0002_second.sql" in res4.stdout, extra=res4.stdout[-300:])
run_migrate(data2, custom)
check("кастомные миграции: INSERT выполнен ровно один раз",
      scalar(data2 / "bot.db", "SELECT COUNT(*) FROM probe") == 1,
      extra=f"строк: {scalar(data2 / 'bot.db', 'SELECT COUNT(*) FROM probe')}")

# ---------- 6. dry-run ничего не меняет ----------

data3 = tmp / "dry"
res5 = run_migrate(data3, custom, "--dry-run")
check("dry-run: код возврата 0", res5.returncode == 0, res5.stderr[-400:])
check("dry-run: база не создана", not (data3 / "bot.db").exists())
check("dry-run: план показан", "dry-run" in res5.stdout and "0001_first.sql" in res5.stdout)

# ---------- 7. упавшая миграция: откат и без отметки ----------

broken = tmp / "broken-migrations"
broken.mkdir()
(broken / "0001_broken.sql").write_text(
    "CREATE TABLE IF NOT EXISTS ok_table (id INTEGER PRIMARY KEY);\n"
    "ALTER TABLE no_such_table ADD COLUMN x INTEGER;\n", encoding="utf-8")
data4 = tmp / "broken"
res6 = run_migrate(data4, broken)
check("битая миграция: ненулевой код возврата", res6.returncode != 0)
check("битая миграция: не помечена применённой",
      scalar(data4 / "bot.db", "SELECT COUNT(*) FROM schema_migrations") == 0)
check("битая миграция: частичных изменений нет (транзакция откатилась)",
      "ok_table" not in tables(data4 / "bot.db"),
      extra=str(sorted(tables(data4 / "bot.db"))))

shutil.rmtree(tmp, ignore_errors=True)
print(f"\nИтог: {passed} ок, {failed} провалено")
sys.exit(1 if failed else 0)

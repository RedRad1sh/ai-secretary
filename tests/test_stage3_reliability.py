"""Надёжность 24/7 — кодовая часть (issue #5).

Проверяется тем же кодом, что в проде (Database, reminders.sweep_all,
scripts/backup.py):

1. Перезапуск процесса: события и незапущенные напоминания переживают
   рестарт (они в SQLite); FSM-состояние (неподтверждённая карточка)
   теряется — ожидаемое ограничение, а не дефект.
2. Напоминания: зрелые (sent=0) отправляются после рестарта; mark sent —
   только после успешной отправки; при ошибке остаётся для следующего
   прогона; at-least-once: сбой между send и mark даёт задокументированный
   дубль.
3. Базовые напоминания доступны всем разрешённым пользователям (в т.ч.
   неплатным).
4. Бэкап/восстановление: scripts/backup.py -> полная копия bot.db,
   users/*.db и secret.key; из копии восстанавливается рабочий каталог с
   теми же событиями/напоминаниями; права на файлах 0600, на каталоге 0700.

Действия на боевом хосте (cron, 7 дней, перезапуск машины) НЕ входят в этот
тест — см. issue #5, «на боевом хосте».

Запуск: python tests/test_stage3_reliability.py   (сеть не нужна)
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ALLOWED_USER_ID", "100")
os.environ.setdefault("ALLOWED_USER_IDS", "100,200,300")
os.environ.setdefault("PAID_USER_IDS", "100,200")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ai-secretary-s3-"))

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import Update  # noqa: E402

from bot import reminders  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

OWN, PAID, FREE = 100, 200, 300
FUTURE = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ {name}")
    else:
        failed += 1
        print(f"❌ {name}  -> {extra}")


class FakeLLM:
    async def chat(self, system: str, user: str, **kw) -> str:
        return json.dumps({
            "is_event": True, "title": f"Событие {user[:24]}", "date": FUTURE,
            "time": "12:00", "end_time": None, "duration_minutes": 60,
            "all_day": False, "location": None, "participants": None,
            "description": None, "recurrence": None, "confidence": 0.95,
            "missing": [],
        }, ensure_ascii=False)

    async def close(self) -> None:
        pass


class RecSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return getattr(method, "__return_value__", True)

    async def stream_content(self, *a, **k):
        yield b""


N = 0


def _msg(uid: int, text: str) -> Update:
    global N
    N += 1
    return Update.model_validate({"update_id": N, "message": {
        "message_id": N + 1000, "date": 1758800000,
        "chat": {"id": uid, "type": "private"},
        "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"},
        "text": text}})


def _cbq(uid: int, data: str) -> Update:
    global N
    N += 1
    return Update.model_validate({"update_id": N, "callback_query": {
        "id": f"cb-{N}", "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"},
        "chat_instance": "x", "data": data,
        "message": {"message_id": N + 5000, "date": 1758800000,
                    "chat": {"id": uid, "type": "private"},
                    "from": {"id": 777000, "is_bot": True, "first_name": "Bot"}}}})


class CapBot:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw) -> None:
        if self.fail:
            raise RuntimeError("telegram offline")
        self.sent.append((chat_id, text))


class CrashAfterSend:
    """send прошёл, но процесс «упал» до mark_reminder_sent."""

    async def send_message(self, chat_id, text, **kw) -> None:
        raise RuntimeError("crash between send and mark")


async def main() -> None:
    cfg = load_config()
    open_: list = []
    try:
        # ===== Фаза 1: первый «прогон» процесса =====
        db = Database(cfg.db_path)
        await db.connect()
        open_.append(db)
        bot = Bot(cfg.bot_token, session=RecSession())
        open_.append(bot.session)
        llm = FakeLLM()
        open_.append(llm)
        dp = build_dispatcher(cfg, db, llm, None)

        async def feed(u: Update):
            n0 = len(bot.session.calls)
            await asyncio.wait_for(dp.feed_update(bot, u), timeout=15)
            return [c for c in bot.session.calls[n0:]
                    if c.__class__.__name__ == "SendMessage"]

        # Владелец: событие в будущем + напоминание (не зрелое сейчас)
        await feed(_msg(OWN, "встреча A"))
        await feed(_cbq(OWN, "ev:create"))
        # Владелец: карточка показана, НО не подтверждена (FSM-состояние живое)
        await feed(_msg(OWN, "встреча B"))
        # Неплатный: своё одиночное событие (базовый доступ)
        await feed(_msg(FREE, "одна встреча C"))
        await feed(_cbq(FREE, "ev:create"))
        # Платный невладелец: тоже своё событие (появится users/200.db)
        await feed(_msg(PAID, "одна встреча D"))
        await feed(_cbq(PAID, "ev:create"))
        # Зрелое сейчас напоминание (как после долгого простаивания в БД)
        now = datetime.now(timezone.utc)
        await db.add_reminder(event_pk=1, title="Зрелое напоминание",
                              start_iso=(now + timedelta(minutes=5)).isoformat(),
                              remind_at=(now - timedelta(seconds=1)).isoformat(),
                              minutes_before=5)
        # Неплатному — тоже зрелое (базовые напоминания всем разрешённым)
        d300 = Database(cfg.db_path.parent / "users" / "300.db")
        await d300.connect()
        await d300.add_reminder(event_pk=1, title="Напоминание неплатного",
                                start_iso=(now + timedelta(minutes=5)).isoformat(),
                                remind_at=(now - timedelta(seconds=1)).isoformat(),
                                minutes_before=5)
        await d300.close()

        # ===== «Перезапуск процесса»: всё закрываем =====
        # В проде это новый процесс. В тесте «новый процесс» = новое
        # SQLite-соединение к тем же файлам + пустое MemoryStorage (FSM в
        # памяти). До рестарта неподтверждённая карточка в FSM есть:
        old_key = StorageKey(bot_id=123456, chat_id=OWN, user_id=OWN)
        old_data = await dp.storage.get_data(old_key)
        check("1.0 до рестарта: неподтверждённая карточка в FSM",
              bool(old_data.get("draft")), str(old_data)[:120])
        await bot.session.close()
        await db.close()
        await llm.close()

        # ===== Фаза 2: новый процесс, тот же DATA_DIR =====
        db2 = Database(cfg.db_path)
        await db2.connect()
        open_.append(db2)

        evs_own = [e["title"] for e in await db2.list_events()]
        check("1.1 после рестарта: события владельца сохранены в БД",
              any("встреча A" in t for t in evs_own), str(evs_own))
        d300r = Database(cfg.db_path.parent / "users" / "300.db")
        await d300r.connect()
        evs_free = [e["title"] for e in await d300r.list_events()]
        await d300r.close()
        check("1.2 после рестарта: события неплатного сохранены в БД",
              any("встреча C" in t for t in evs_free), str(evs_free))
        fresh = FSMContext(MemoryStorage(), old_key)
        check("1.3 после рестарта: неподтверждённая карточка потеряна "
              "(FSM в памяти — ожидаемое ограничение)",
              (await fresh.get_data()) == {}, str(await fresh.get_data()))

        # Зрелые напоминания переживают рестарт и доставляются владельцам
        cap = CapBot()
        n = await reminders.sweep_all(cap, db2, cfg)
        check("2.1 после рестарта: зрелые напоминания отправлены", n >= 2,
              f"n={n} sent={cap.sent}")
        check("2.2 напоминание владельца — только ему",
              any(cid == OWN and "Зрелое напоминание" in t
                  for cid, t in cap.sent), str(cap.sent))
        check("2.3 базовое напоминание неплатному — доставлено",
              any(cid == FREE and "Напоминание неплатного" in t
                  for cid, t in cap.sent), str(cap.sent))

        # mark sent — только после успешной отправки
        d300b = Database(cfg.db_path.parent / "users" / "300.db")
        await d300b.connect()
        rows = await d300b.due_reminders((now + timedelta(seconds=10)).isoformat())
        check("2.4 после успешной отправки: нет pending у неплатного",
              not rows, str(rows))
        await d300b.close()

        # Ошибка отправки: напоминание остаётся для следующего прогона
        # (start_iso отличается — иначе dedup в add_reminder его отбросит)
        await db2.add_reminder(event_pk=1, title="Сбойный",
                               start_iso=(now + timedelta(minutes=6)).isoformat(),
                               remind_at=(now - timedelta(seconds=1)).isoformat(),
                               minutes_before=5)
        failbot = CapBot(fail=True)
        n = await reminders.sweep_all(failbot, db2, cfg)
        check("3.1 сбой Telegram: ничего не отправлено, цикл не упал",
              n == 0 and not failbot.sent, f"n={n}")
        rows = await db2.due_reminders((now + timedelta(seconds=10)).isoformat())
        check("3.2 сбой: напоминание осталось pending (sent=0)",
              any(r["title"] == "Сбойный" and not r["sent"] for r in rows),
              str(rows))
        cap2 = CapBot()
        n = await reminders.sweep_all(cap2, db2, cfg)
        check("3.3 следующий прогон: напоминание доставлено",
              any(cid == OWN and "Сбойный" in t for cid, t in cap2.sent),
              str(cap2.sent))

        # at-least-once: сбой МЕЖДУ send и mark -> задокументированный дубль
        await db2.add_reminder(event_pk=1, title="Дубль",
                               start_iso=(now + timedelta(minutes=7)).isoformat(),
                               remind_at=(now - timedelta(seconds=1)).isoformat(),
                               minutes_before=5)
        n = await reminders.sweep_all(CrashAfterSend(), db2, cfg)
        rows = await db2.due_reminders((now + timedelta(seconds=10)).isoformat())
        pending = [r for r in rows if r["title"] == "Дубль"]
        check("3.4 at-least-once: после «краха» между send и mark строка pending",
              pending, str(rows))
        cap3 = CapBot()
        n = await reminders.sweep_all(cap3, db2, cfg)
        dups = [t for cid, t in cap3.sent if "Дубль" in t]
        check("3.5 at-least-once: повторный прогон отправляет ещё раз (дубль — "
              "известное ограничение, задокументировано в ROADMAP)",
              len(dups) == 1, str(cap3.sent))

        # ===== Бэкап / восстановление =====
        (cfg.data_dir / "secret.key").write_bytes(b"fernet-key-for-test")
        spec = importlib.util.spec_from_file_location(
            "backup_mod",
            Path(__file__).resolve().parent.parent / "scripts" / "backup.py")
        backup_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backup_mod)

        target_dir = Path(tempfile.mkdtemp(prefix="ai-secretary-bkp-"))
        snapshot = backup_mod.backup(target_dir)
        check("4.1 бэкап: каталог создан", snapshot.is_dir(), str(snapshot))
        for rel in ("bot.db", "users/200.db", "users/300.db", "secret.key"):
            p = snapshot / rel
            check(f"4.2 бэкап содержит {rel}", p.is_file())
            if p.is_file():
                mode = stat.S_IMODE(p.stat().st_mode)
                check(f"4.3 права на {rel} == 0600", mode == 0o600, oct(mode))
        snap_mode = stat.S_IMODE(snapshot.stat().st_mode)
        check("4.4 права на каталог бэкапа == 0700", snap_mode == 0o700,
              oct(snap_mode))
        key_ok = (snapshot / "secret.key").read_bytes() == b"fernet-key-for-test"
        check("4.5 secret.key скопирован побайтово", key_ok)

        # Восстановление в новый каталог и сверка содержимого
        restore_dir = Path(tempfile.mkdtemp(prefix="ai-secretary-restore-"))
        shutil.copytree(snapshot, restore_dir, dirs_exist_ok=True)
        d_own_r = Database(restore_dir / "bot.db")
        await d_own_r.connect()
        evs_r = [e["title"] for e in await d_own_r.list_events()]
        st_r = await d_own_r.stats()
        await d_own_r.close()
        d300_r = Database(restore_dir / "users" / "300.db")
        await d300_r.connect()
        evs300_r = [e["title"] for e in await d300_r.list_events()]
        await d300_r.close()
        evs_now = [e["title"] for e in await db2.list_events()]
        d300c = Database(cfg.db_path.parent / "users" / "300.db")
        await d300c.connect()
        evs300_now = [e["title"] for e in await d300c.list_events()]
        await d300c.close()
        check("4.6 восстановление: события владельца совпадают",
              sorted(evs_r) == sorted(evs_now), f"{evs_r} vs {evs_now}")
        check("4.7 восстановление: события неплатного совпадают",
              sorted(evs300_r) == sorted(evs300_now),
              f"{evs300_r} vs {evs300_now}")
        check("4.8 восстановление: статистика напоминаний совпадает",
              st_r["reminders_pending"] == (await db2.stats())["reminders_pending"],
              str(st_r))

        shutil.rmtree(target_dir, ignore_errors=True)
        shutil.rmtree(restore_dir, ignore_errors=True)
    finally:
        for obj in reversed(open_):
            try:
                await obj.close()
            except Exception:  # noqa: BLE001 — очистка при любом исходе
                pass

    if failed:
        print(f"\nИтог: {passed} ок, {failed} провалено")
        raise SystemExit(1)
    print(f"\nИтог: {passed} ок, 0 провалено")


if __name__ == "__main__":
    asyncio.run(main())

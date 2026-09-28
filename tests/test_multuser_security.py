"""Многопользовательская изоляция и платные ограничения (issue #3).

Прогоняет реальный диспетчер (build_dispatcher — тот же код, что в main.py и
serverless-адаптере) на пяти «пользователях»:

  100 — владелец старой базы bot.db (ALLOWED_USER_ID), в обоих списках;
  200 — платный пользователь (ALLOWED + PAID), отдельная users/200.db;
  300 — неплатный пользователь (только ALLOWED), отдельная users/300.db;
  400 — «платный-only»: есть в PAID_USER_IDS, НО нет в ALLOWED_USER_IDS;
  999 — чужой ID, не в ни одном списке.

Критерии (issue #3):
  1) два пользователя не видят/не изменяют чужие события и настройки;
  2) платный список не обходит общий;
  3) неплатный не обходит ограничения текстом, фото, кнопками и «старым»
     FSM-state после ручного отзыва платного статуса;
  4) отзыв из общего списка: бот молчит, callback отклонён, база сохранена,
     напоминания не шлются;
  5) в групповых чатах приватные данные не выдаются.

LLM заменён заглушкой (сеть не нужна). Запуск: python tests/test_multuser_security.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ALLOWED_USER_ID", "100")
os.environ.setdefault("ALLOWED_USER_IDS", "100,200,300")
os.environ.setdefault("PAID_USER_IDS", "100,200,400")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ai-secretary-sec-"))

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import Update  # noqa: E402

from bot import reminders  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

OWN, PAID, FREE, PAIDONLY, STRANGER = 100, 200, 300, 400, 999

# Явная дата в будущем относительно любого «сейчас»: не зависит от даты запуска.
EVENT_DATE = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")

PAID_NOTICE = "🔒 Эта функция доступна платному списку"


class FakeLLM:
    """Офлайн-заглушка LLM: управляет ответом по маркерам в тексте."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, system: str, user: str, **kw) -> str:
        self.calls.append(user)
        if "двух встреч" in user:
            return json.dumps({"events": [
                {"title": "Первая", "date": EVENT_DATE, "time": "10:00",
                 "duration_minutes": 30, "all_day": False, "recurrence": None,
                 "confidence": 0.9, "missing": []},
                {"title": "Вторая", "date": EVENT_DATE, "time": "15:00",
                 "duration_minutes": 30, "all_day": False, "recurrence": None,
                 "confidence": 0.9, "missing": []},
            ]}, ensure_ascii=False)
        if "повтор" in user:
            return json.dumps({"is_event": True, "title": "Повторка",
                               "date": EVENT_DATE, "time": "09:00",
                               "duration_minutes": 30, "all_day": False,
                               "recurrence": {"freq": "DAILY"},
                               "confidence": 0.9, "missing": []}, ensure_ascii=False)
        return json.dumps({
            "is_event": True, "title": f"Событие {user[:24]}", "date": EVENT_DATE,
            "time": "14:00", "end_time": None, "duration_minutes": 60,
            "all_day": False, "location": None, "participants": None,
            "description": None, "recurrence": None, "confidence": 0.95,
            "missing": [],
        }, ensure_ascii=False)

    async def close(self) -> None:
        pass


class RecSession(BaseSession):
    """Записывает все исходящие вызовы Telegram API вместо сети."""

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


def _msg(uid: int, text: str = "", chat_id: int | None = None,
         chat_type: str = "private", photo: bool = False) -> Update:
    global N
    N += 1
    m: dict = {"message_id": N + 1000, "date": 1758800000,
               "chat": {"id": chat_id or uid, "type": chat_type},
               "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"}}
    if photo:
        m["photo"] = [{"file_id": "f", "file_unique_id": "u", "width": 1,
                       "height": 1, "file_size": 1000}]
    elif text:
        m["text"] = text
    return Update.model_validate({"update_id": N, "message": m})


def _cbq(uid: int, data: str, chat_id: int | None = None,
         chat_type: str = "private") -> Update:
    global N
    N += 1
    return Update.model_validate({"update_id": N, "callback_query": {
        "id": f"cb-{N}", "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"},
        "chat_instance": "x", "data": data,
        "message": {"message_id": N + 5000, "date": 1758800000,
                    "chat": {"id": chat_id or uid, "type": chat_type},
                    "from": {"id": 777000, "is_bot": True, "first_name": "Bot"}}}})


class CapBot:
    """Записывает send_message для проверки маршрутизации напоминаний."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw) -> None:
        self.sent.append((chat_id, text))


async def main() -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    await db.connect()
    bot = Bot(cfg.bot_token, session=RecSession())
    llm = FakeLLM()
    dp = build_dispatcher(cfg, db, llm, None)
    results: list[tuple[str, bool, str]] = []

    async def feed(update: Update) -> list[tuple[str, str]]:
        n0 = len(bot.session.calls)
        await asyncio.wait_for(dp.feed_update(bot, update), timeout=15)
        out = []
        for c in bot.session.calls[n0:]:
            nm = c.__class__.__name__
            if nm == "SendMessage":
                out.append(("msg", c.text or ""))
            elif nm == "AnswerCallbackQuery":
                out.append(("cbans", c.text or ""))
            elif nm == "SendDocument":
                out.append(("doc", "<ics>"))
            elif nm != "SendChatAction":
                out.append((nm, ""))
        return out

    def msgs(res: list[tuple[str, str]]) -> list[str]:
        return [t for k, t in res if k == "msg"]

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append((name, bool(cond), extra))
        print(f"{'✅' if cond else '❌'} {name}" + (f"  -> {extra}" if not cond else ""))

    # ── 1. Создание событий и изоляция /list ──────────────────────────────
    await feed(_msg(OWN, "встреча A"))
    await feed(_cbq(OWN, "ev:create"))
    await feed(_msg(PAID, "встреча B"))
    await feed(_cbq(PAID, "ev:create"))

    own_list = msgs(await feed(_msg(OWN, "/list")))
    paid_list = msgs(await feed(_msg(PAID, "/list")))
    check("1.1 /list владельца: только свои события",
          own_list and "встреча A" in own_list[0] and "встреча B" not in own_list[0],
          str(own_list))
    check("1.2 /list платного: только свои события",
          paid_list and "встреча B" in paid_list[0] and "встреча A" not in paid_list[0],
          str(paid_list))

    # ── 2. Изоляция /undo ──────────────────────────────────────────────────
    await feed(_msg(PAID, "/undo"))
    own_list2 = msgs(await feed(_msg(OWN, "/list")))
    paid_list2 = msgs(await feed(_msg(PAID, "/list")))
    check("2.1 /undo платного не трогает базу владельца",
          own_list2 and "встреча A" in own_list2[0], str(own_list2))
    check("2.2 /undo удалил событие из своей базы",
          paid_list2 and "встреча B" not in paid_list2[0], str(paid_list2))

    # ── 3. Изоляция настроек (st:* callbacks пишут в свою БД) ─────────────
    await feed(_cbq(PAID, "st:tz"))
    await feed(_cbq(PAID, "st:tzset:Asia/Yekaterinburg"))
    user200db = cfg.db_path.parent / "users" / "200.db"
    d200 = Database(user200db)
    await d200.connect()
    tz200 = await d200.get_setting("timezone", "")
    await d200.close()
    tz_own = await db.get_setting("timezone", "")
    check("3.1 чужая настройка tz не попала в базу владельца", tz_own == "", tz_own)
    check("3.2 платный сохранил tz в СВОЕЙ базе", tz200 == "Asia/Yekaterinburg", tz200)

    # ── 4. Неплатный: ограничения по командам/тексту/фото ─────────────────
    for t in ["/today", "/tomorrow", "/stats", "отмени встречу",
              "перенеси встречу на 15:00"]:
        res = msgs(await feed(_msg(FREE, t)))
        check(f"4. неплатный {t!r} -> платное уведомление",
              any(PAID_NOTICE in (x or "") for x in res), str(res))
    res = msgs(await feed(_msg(FREE, "x", photo=True)))
    check("4. неплатный: фото -> платное уведомление",
          any(PAID_NOTICE in (x or "") for x in res), str(res))
    res = msgs(await feed(_msg(FREE, "у меня двух встреч завтра")))
    check("4. неплатный: несколько событий -> платное уведомление",
          any(PAID_NOTICE in (x or "") for x in res), str(res))
    res = msgs(await feed(_msg(FREE, "напомни про повтор каждый день")))
    check("4. неплатный: повтор -> платное уведомление",
          any(PAID_NOTICE in (x or "") for x in res), str(res))
    res = msgs(await feed(_msg(FREE, "/list")))
    check("4. неплатный: /list доступен (общий доступ)",
          res and "Пока я не создал" in res[0], str(res))
    # Базовый доступ: одно неповторяющееся событие создаётся
    await feed(_msg(FREE, "одна встреча C"))
    res = await feed(_cbq(FREE, "ev:create"))
    check("4. неплатный: одиночное событие создаётся (.ics)",
          any(k == "doc" for k, _ in res), str(res))

    # ── 5. Платный-only не обходит общий список ───────────────────────────
    res = await feed(_msg(PAIDONLY, "/list"))
    check("5.1 paid-only: сообщение молча игнорируется", res == [], str(res))
    res = await feed(_cbq(PAIDONLY, "ev:create"))
    check("5.2 paid-only: callback «Нет доступа»",
          res == [("cbans", "Нет доступа")], str(res))

    # ── 6. Чужой ID ───────────────────────────────────────────────────────
    res = await feed(_msg(STRANGER, "/list"))
    res2 = await feed(_cbq(STRANGER, "ev:create"))
    check("6. чужой ID: ни сообщений, ни данных", res == [] and
          res2 == [("cbans", "Нет доступа")], f"{res} {res2}")

    # ── 7. Групповые чаты ─────────────────────────────────────────────────
    res = await feed(_msg(PAID, "встреча G", chat_id=555, chat_type="group"))
    check("7.1 группа: сообщение не обрабатывается", res == [], str(res))
    res = await feed(_cbq(PAID, "ev:create", chat_id=555, chat_type="group"))
    check("7.2 группа: callback отклонён",
          res == [("cbans", "Откройте личный чат с ботом")], str(res))

    # ── 8. /stats: своя база, а не bot.db владельца ───────────────────────
    await db.set_setting("pad", "x" * 20000)  # разводим размеры баз
    paid_stats = msgs(await feed(_msg(PAID, "/stats")))
    owner_size_kb = round(cfg.db_path.stat().st_size / 1024)
    user200_size_kb = round(user200db.stat().st_size / 1024)
    check("8.1 /stats платного: размер СВОЙ базы, не bot.db",
          paid_stats and str(user200_size_kb) in paid_stats[0]
          and owner_size_kb != user200_size_kb
          and str(owner_size_kb) not in paid_stats[0],
          f"bot.db={owner_size_kb}KB users/200.db={user200_size_kb}KB {paid_stats}")

    # ── 9. Отзыв платного статуса: «старые» кнопки и FSM-state ────────────
    # Предпросмотр пакета показан, пока пользователь ещё платный…
    res = msgs(await feed(_msg(PAID, "у меня двух встреч завтра")))
    check("9.0 платный: мульти-предпросмотр показан",
          any("Нашёл 2 события" in (x or "") for x in res), str(res))
    # …а затем оператор убрал ID из PAID_USER_IDS (живой процесс, FSM сохранён)
    dp["cfg"] = dataclasses.replace(cfg, paid_user_ids=frozenset({100, 400}))
    res = await feed(_cbq(PAID, "ev:create_all"))
    check("9.1 после отзыва: ev:create_all заблокирован",
          any(PAID_NOTICE in (x or "") for _, x in res), str(res))
    res = await feed(_cbq(PAID, "ev:create"))
    check("9.2 после отзыва: старая ev:create заблокирована",
          any(PAID_NOTICE in (x or "") for _, x in res), str(res))
    res = await feed(_cbq(PAID, "mg:yes"))
    check("9.3 после отзыва: mg:yes заблокирован",
          any(PAID_NOTICE in (x or "") for _, x in res), str(res))
    # Базовый доступ сохранён: одно событие создаётся
    await feed(_cbq(PAID, "ev:edit"))
    await feed(_msg(PAID, "одна встреча D"))
    res = await feed(_cbq(PAID, "ev:create"))
    check("9.4 после отзыва: одиночное событие разрешено",
          any(k == "doc" for k, _ in res), str(res))

    # ── 10. Маршрутизация напоминаний ─────────────────────────────────────
    d200 = Database(user200db)
    await d200.connect()
    now = datetime.now(timezone.utc)
    await d200.add_reminder(event_pk=1, title="Собственное напоминание 200",
                            start_iso=(now + timedelta(minutes=10)).isoformat(),
                            remind_at=(now - timedelta(seconds=1)).isoformat(),
                            minutes_before=10)
    await d200.close()
    cap = CapBot()
    n = await reminders.sweep_all(cap, db, cfg)
    check("10.1 зрелое напоминание доставлено", n >= 1, f"n={n} sent={cap.sent}")
    check("10.2 напоминание ушло только в чат владельца события",
          cap.sent and all(cid == PAID for cid, _ in cap.sent), str(cap.sent))
    check("10.3 текст содержит только данные этого пользователя",
          cap.sent and "Собственное напоминание 200" in cap.sent[0][1]
          and "встреча A" not in cap.sent[0][1], str(cap.sent))

    # ── 11. Отзыв из общего списка ────────────────────────────────────────
    dp["cfg"] = dataclasses.replace(cfg,
                                    allowed_user_ids=frozenset({100, 200}),
                                    paid_user_ids=frozenset({100}))
    res = await feed(_msg(FREE, "/list"))
    check("11.1 удалённый из общего списка: /list молча игнорируется",
          res == [], str(res))
    res = await feed(_cbq(FREE, "ev:create"))
    check("11.2 удалённый: callback «Нет доступа»",
          res == [("cbans", "Нет доступа")], str(res))
    user300db = cfg.db_path.parent / "users" / "300.db"
    d300 = Database(user300db)
    await d300.connect()
    evs300 = [e["title"] for e in await d300.list_events()]
    await d300.close()
    check("11.3 база удалённого пользователя сохранена",
          user300db.exists() and any("встреча C" in t for t in evs300), str(evs300))
    cap2 = CapBot()
    await reminders.sweep_all(cap2, db, dp["cfg"])
    check("11.4 напоминания удалённому пользователю не шлются",
          all(cid != FREE for cid, _ in cap2.sent), str(cap2.sent))

    # ── 12. Совместимость с прежней базой владельца ───────────────────────
    evs_own = [e["title"] for e in await db.list_events()]
    check("12.1 данные владельца остались в bot.db",
          any("встреча A" in t for t in evs_own), str(evs_own))
    check("12.2 события не-владельцев не записаны в bot.db",
          not any("встреча B" in t or "встреча C" in t or "встреча D" in t
                  for t in evs_own), str(evs_own))
    d200 = Database(user200db)
    await d200.connect()
    evs200 = [e["title"] for e in await d200.list_events()]
    await d200.close()
    check("12.3 события платного пользователя в users/200.db",
          any("встреча D" in t for t in evs200), str(evs200))

    await bot.session.close()
    await db.close()

    failed = [r for r in results if not r[1]]
    print(f"\nИтог: {len(results) - len(failed)}/{len(results)} ок"
          + (f", провалено: {[r[0] for r in failed]}" if failed else ""))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

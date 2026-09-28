"""Плановые запросы (серии событий): маркировка допущений, без тихих дефолтов (issue #13).

Контекст — боевой лог 28.09.2026: «настрой мне программу тренировок на всю
неделю. Создай 12 событий» — бот сгенерировал 12 событий с выдуманным
временем 08:00–09:00 и тихим расколом «утро/вечер»; предпросмотр из 12
карточек был нечитаемым.

Что проверяется:
  S1. Плановый запрос без времени: модель не выдумывает время (time=null),
      система ставит дефолт И маркирует допущение («⚠️ время не было
      указано — поставил дефолт …», «…длительность…»).
  S2. Модель сама подставила дефолты и пометила их в assumptions —
      маркеры видны в предпросмотре дословно (включая «раскладку»).
  S3. Модель подставила время, но забыла пометить — страховочная маркировка
      кодом: если во всём запросе не было ни одного времени, любое время в
      событиях помечается явно.
  S4. Два события (не серия) — обычные карточки, как раньше.
  S5. Одиночное событие с допущением модели — маркер в карточке.
  S6. System prompt содержит правила для плановых запросов.
  S7. Допущения экранируются в HTML (issue #8).

Запуск: python tests/test_planned_requests.py   (сеть не нужна)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ALLOWED_USER_ID", "100")
os.environ.setdefault("ALLOWED_USER_IDS", "100,200,300")
os.environ.setdefault("PAID_USER_IDS", "100,200")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ai-secretary-planned-"))

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import Message, Update  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402
from bot.models import DEFAULT_DURATION, DEFAULT_TIME  # noqa: E402
from bot.services.extractor import PROMPT  # noqa: E402

OWN = 100
TODAY = datetime.now(ZoneInfo("Europe/Moscow"))
TOMORROW = TODAY + timedelta(days=1)

MSK = ZoneInfo("Europe/Moscow")

# ожидаемый дефолт системы (issue #13: явный, с пометкой)
DEF_START = DEFAULT_TIME.strftime("%H:%M")
DEF_END = (datetime.combine(datetime.now(), DEFAULT_TIME) + DEFAULT_DURATION).strftime("%H:%M")
DEF_SPAN = f"{DEF_START}–{DEF_END}"
DEF_MINS = int(DEFAULT_DURATION.total_seconds() // 60)

passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ {name}")
    else:
        failed += 1
        print(f"❌ {name}  -> {extra}")


def _msk(start_iso: str) -> datetime:
    """start_iso из БД -> московское время (сырые UTC-строки не сравниваем)."""
    return datetime.fromisoformat(start_iso).astimezone(MSK)


def _day(i: int) -> str:
    return (TOMORROW + timedelta(days=i)).strftime("%Y-%m-%d")


def _planned_events(n: int, title: str, desc: str | None, *, time_s=None,
                    duration=None, assumptions=None, missing=None) -> str:
    """Серия из n похожих событий подряд, начиная с завтра."""
    events = []
    for i in range(n):
        events.append({
            "is_event": True, "title": title, "date": _day(i),
            "time": time_s, "end_time": None, "duration_minutes": duration,
            "all_day": False, "location": None, "participants": None,
            "description": desc, "recurrence": None, "confidence": 0.9,
            "missing": list(missing or []), "assumptions": list(assumptions or []),
        })
    return json.dumps({"events": events}, ensure_ascii=False)


S1_TEXT = ("настрой мне программу тренировок на всю неделю. "
           "Создай 12 событий, в каждом: подтягивания, брусья, отжимания, бег")
S2_TEXT = "распиши тренировки на 3 дня, 3 события"
S3_TEXT = "план йоги на неделю, 5 событий"
S4_TEXT = "двух дел завтра: уборка в 10:00 и ужин в 15:00"
S5_TEXT = "напоминание про витамины"
S7_TEXT = "злой план: 3 события в 10:00"


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.systems: list[str] = []

    async def chat(self, system: str, user: str, **kw) -> str:
        self.calls.append(user)
        self.systems.append(system)
        if user == S1_TEXT:
            # модель НЕ выдумывает время (time=null) — так велит новый prompt
            return _planned_events(12, "Тренировка",
                                   "подтягивания, брусья, отжимания, бег",
                                   time_s=None, duration=None,
                                   missing=["time"], assumptions=[])
        if user == S2_TEXT:
            # модель подставила дефолт САМА и честно пометила допущения
            return _planned_events(3, "Растяжка", "шпагат и мостик",
                                   time_s="08:00", duration=60,
                                   assumptions=[
                                       "время не было указано — поставил дефолт 08:00–09:00",
                                       "раскладка утро/вечер — моё допущение",
                                   ])
        if user == S3_TEXT:
            # модель подставила время, но забыла пометить (assumptions пустой)
            return _planned_events(5, "Йога", "сурья намаскар",
                                   time_s="07:30", duration=60, assumptions=[])
        if user == S4_TEXT:
            return json.dumps({"events": [
                {"is_event": True, "title": "Уборка", "date": _day(0),
                 "time": "10:00", "duration_minutes": 60, "all_day": False,
                 "description": None, "recurrence": None, "confidence": 0.9,
                 "missing": [], "assumptions": []},
                {"is_event": True, "title": "Ужин", "date": _day(0),
                 "time": "15:00", "duration_minutes": 90, "all_day": False,
                 "description": None, "recurrence": None, "confidence": 0.9,
                 "missing": [], "assumptions": []},
            ]}, ensure_ascii=False)
        if user == S5_TEXT:
            return json.dumps({
                "is_event": True, "title": "Витамины", "date": _day(0),
                "time": "09:00", "end_time": None, "duration_minutes": 30,
                "all_day": False, "location": None, "participants": None,
                "description": None, "recurrence": None, "confidence": 0.9,
                "missing": [], "assumptions": ["дни недели не были указаны — взял будни"],
            }, ensure_ascii=False)
        if user == S7_TEXT:
            return _planned_events(3, "Хитрый", "опасный план",
                                   time_s="10:00", duration=30,
                                   assumptions=["опасное <b>допущение</b>"])
        return json.dumps({"is_event": False}, ensure_ascii=False)

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
        if type(method).__name__ == "SendMessage":
            return Message.model_validate({
                "message_id": 900000 + len(self.calls), "date": 1758800000,
                "chat": {"id": getattr(method, "chat_id", OWN), "type": "private"},
                "text": getattr(method, "text", "") or "",
            }, context={"bot": bot})
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
        "text": text,
    }})


def _cbq(uid: int, data: str) -> Update:
    global N
    N += 1
    return Update.model_validate({"update_id": N, "callback_query": {
        "id": f"cb-{N}", "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"},
        "chat_instance": "x", "data": data,
        "message": {"message_id": N + 5000, "date": 1758800000,
                    "chat": {"id": uid, "type": "private"},
                    "from": {"id": 777000, "is_bot": True, "first_name": "Bot"}}}})


async def main() -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    await db.connect()
    bot = Bot(cfg.bot_token, session=RecSession())
    llm = FakeLLM()
    dp = build_dispatcher(cfg, db, llm, None)

    async def feed(update: Update):
        n0 = len(bot.session.calls)
        await asyncio.wait_for(dp.feed_update(bot, update), timeout=30)
        return bot.session.calls[n0:]

    def texts(calls) -> list[str]:
        return [c.text or "" for c in calls if c.__class__.__name__ == "SendMessage"]

    def all_text(calls) -> str:
        return "\n".join(texts(calls))

    def preview_kb(calls):
        for c in reversed(calls):
            if c.__class__.__name__ == "SendMessage" and c.reply_markup:
                for row in c.reply_markup.inline_keyboard:
                    for b in row:
                        if b.callback_data in ("ev:create", "ev:create_all", "mg:yes"):
                            return b.callback_data
        return None

    try:
        # ============ S1: плановый запрос без времени -> дефолт + маркеры ============
        calls = await feed(_msg(OWN, S1_TEXT))
        t = all_text(calls)
        check("S1a 12 событий без времени: компактный блок серии, не 12 карточек",
              "📌" not in t and t.count("📦") == 1 and "12 событий" in t,
              t[:300])
        check("S1b маркер допущения: время не указано -> дефолт " + DEF_SPAN,
              f"⚠️ время не было указано — поставил дефолт {DEF_SPAN}" in t, t[:300])
        check("S1c маркер допущения: длительность не указана -> дефолт "
              f"{DEF_MINS} минут",
              f"⚠️ длительность не была указана — поставил дефолт {DEF_MINS} минут" in t,
              t[:300])
        check("S1d маркер допущения показан один раз (флаги серии без повторов)",
              t.count("время не было указано") == 1, t[:300])
        check("S1e кнопка «Создать все (12)» и «Создать все?» остались",
              preview_kb(calls) == "ev:create_all" and "Создать все?" in t
              and any("Создать все (12)" in (b.text or "")
                      for c in calls if c.__class__.__name__ == "SendMessage" and c.reply_markup
                      for row in c.reply_markup.inline_keyboard for b in row),
              t[:300])

        calls = await feed(_cbq(OWN, "ev:create_all"))
        rows = [r for r in await db.all_events() if r["title"] == "Тренировка"]
        times_ok = len(rows) == 12 and all(
            _msk(r["start_iso"]).strftime("%H:%M") == DEF_START
            and _msk(r["end_iso"]).strftime("%H:%M") == DEF_END for r in rows)
        check("S1f «Создать все»: 12 событий в БД с дефолтным временем (МСК)",
              times_ok, f"n={len(rows)} " +
              str([_msk(r["start_iso"]).strftime("%H:%M") for r in rows][:4]))

        # ============ S2: модель пометила допущения сама -> маркеры дословно ============
        calls = await feed(_msg(OWN, S2_TEXT))
        t = all_text(calls)
        check("S2a серия из 3: компактный блок («3 события»)",
              "📌" not in t and t.count("📦") == 1 and "| 3 события" in t, t[:300])
        check("S2b допущения модели показаны дословно (время и раскладка)",
              "⚠️ время не было указано — поставил дефолт 08:00–09:00" in t
              and "⚠️ раскладка утро/вечер — моё допущение" in t, t[:300])
        check("S2c тихих дефолтов кода нет: время/длительность были заданы",
              "длительность не была указана" not in t and DEF_SPAN not in t, t[:300])

        # ============ S3: модель забыла пометить -> страховка кода ============
        calls = await feed(_msg(OWN, S3_TEXT))
        t = all_text(calls)
        check("S3a серия из 5: компактный блок («5 событий»)",
              "📌" not in t and t.count("📦") == 1 and "| 5 событий" in t, t[:300])
        check("S3b время не было в запросе -> маркер допущения даже без assumptions",
              "⚠️ время не было указано — поставил дефолт 07:30–08:30" in t, t[:300])

        # ============ S4: два события (не серия) -> обычные карточки ============
        calls = await feed(_msg(OWN, S4_TEXT))
        t = all_text(calls)
        check("S4a 2 события: обычные карточки, как раньше",
              t.count("📌") == 2 and "📦" not in t and "Нашёл 2 события" in t, t[:300])
        check("S4b времена заданы в запросе -> допущений нет",
              "⚠️" not in t, t[:300])

        # ============ S5: одиночное событие с допущением -> маркер в карточке ============
        calls = await feed(_msg(OWN, S5_TEXT))
        t = all_text(calls)
        check("S5a одиночное событие: карточка с кнопкой Создать",
              "📌" in t and preview_kb(calls) == "ev:create", t[:300])
        check("S5b допущение модели помечено в карточке",
              "⚠️ дни недели не были указаны — взял будни" in t, t[:300])
        check("S5c время подобрано моделью без пометки -> страховка кода (09:00–09:30)",
              "⚠️ время не было указано — поставил дефолт 09:00–09:30" in t, t[:300])

        # ============ S6: правила плановых запросов в system prompt ============
        check("S6a в system prompt есть раздел про плановые запросы",
              "ПЛАНОВЫЕ ЗАПРОСЫ" in PROMPT and "не выдумывай время" in PROMPT.lower())
        check("S6b prompt требует assumptions и запрещает тихие дефолты",
              "\"assumptions\"" in PROMPT and "Никаких тихих дефолтов" in PROMPT
              and "СТРОГО из запроса пользователя" in PROMPT)

        # ============ S7: допущения экранируются в HTML (issue #8) ============
        calls = await feed(_msg(OWN, S7_TEXT))
        t = all_text(calls)
        check("S7a допущение с HTML-тегами экранировано в компактном блоке",
              "⚠️ опасное &lt;b&gt;допущение&lt;/b&gt;" in t, t[:300])
        check("S7b сырые теги из допущения не попали в сообщение",
              "<b>допущение</b>" not in t, t[:300])

        print(f"\nИтог: {passed} ок, {failed} провалено")
    finally:
        await bot.session.close()
        await db.close()
        await llm.close()

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

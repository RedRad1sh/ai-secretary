"""Offline regression tests: python -m unittest discover -s tests -p test_regressions.py"""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from bot.access import UserAccessMiddleware, user_db_path
from bot.config import Config
from bot.db import Database
from bot.models import EventDraft, get_tz
from bot.reminders import schedule_for_event, sweep, replenish_recurring
from bot.services.extractor import extract_events_local
from bot.services.llm import OpenAICompatClient, LLMError


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_and_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(allowed_user_id=1, allowed_user_ids=frozenset({2, 3}),
                         paid_user_ids=frozenset({1, 99}), db_path=Path(directory)/'bot.db')
            self.assertFalse(Config().is_allowed(1))
            self.assertFalse(cfg.is_paid(99))
            self.assertTrue(cfg.is_paid(1))
            self.assertNotEqual(user_db_path(cfg, 1), user_db_path(cfg, 2))
            middleware = UserAccessMiddleware()
            async def event(uid, text):
                return SimpleNamespace(from_user=SimpleNamespace(id=uid), text=text,
                    chat=SimpleNamespace(type='private'), answer=AsyncMock())
            handler = AsyncMock()
            await middleware(handler, await event(99, '/list'), {'cfg': cfg})
            handler.assert_not_called()
            blocked = await event(2, '/stats')
            await middleware(handler, blocked, {'cfg': cfg})
            handler.assert_not_called()
            blocked.answer.assert_awaited_once()
            async def store(ev, data):
                self.assertIsNone(data['gcal'])
                db = data['db']
                self.assertEqual(await db.all_events(), [])
                await db.set_setting('timezone', str(ev.from_user.id))
                await db.add_event(calendar_id='primary', event_id='x', title='private', start_iso=None, end_iso=None)
            for uid in (2, 3):
                await middleware(store, await event(uid, '/list'), {'cfg': cfg, 'gcal': object()})
            for uid in (2, 3):
                db = Database(user_db_path(cfg, uid)); await db.connect()
                self.assertEqual(await db.get_setting('timezone'), str(uid))
                self.assertEqual(len(await db.all_events()), 1)
                await db.close()

    async def test_llm_capabilities_and_truncation(self):
        requests = []
        def respond(request):
            p = json.loads(request.content); requests.append(p)
            if p['model'] == 'a' and 'response_format' in p:
                return httpx.Response(400, json={'error': 'does not support feature json'})
            if p['model'] == 'a':
                return httpx.Response(404)
            if p['max_tokens'] == 1024:
                return httpx.Response(200, json={'choices': [{'finish_reason':'length', 'message':{'content':None}}]})
            return httpx.Response(200, json={'model':'b', 'choices':[{'finish_reason':'stop', 'message':{'content':'{}'}}]})
        cfg = Config(llm_api_url='https://example.test', llm_model='a', llm_fallback_models='b',
            llm_rate_limit_rpm=0, llm_max_tokens=1024, llm_max_tokens_limit=2048, llm_max_retries=1)
        client = OpenAICompatClient(cfg, httpx.MockTransport(respond))
        try:
            self.assertEqual(await client.chat('system','text'), '{}')
            self.assertEqual([p['model'] for p in requests], ['a','a','b','b'])
            self.assertIn('response_format', requests[2])
            self.assertIn('response_format', requests[3])
        finally:
            await client.close()

    async def test_total_timeout(self):
        async def respond(request):
            await asyncio.sleep(1)
            return httpx.Response(500)
        client = OpenAICompatClient(Config(llm_api_url='https://example.test', llm_total_timeout=.01), httpx.MockTransport(respond))
        try:
            with self.assertRaises(LLMError):
                await client.chat('s','u')
        finally:
            await client.close()

    async def test_local_range_and_multiple(self):
        now = datetime(2026,9,27,19,0,tzinfo=get_tz('Europe/Moscow'))
        for text, hours in [('завтра встреча с 10:00 до 11:30', 1.5), ('завтра смена с 23:00 до 01:00', 2), ('завтра в 10:00 встреча на 90 минут', 1.5)]:
            drafts = extract_events_local(text, 'Europe/Moscow', now)
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].start.day, 28)
            self.assertEqual(drafts[0].end-drafts[0].start, timedelta(hours=hours))
            self.assertTrue(drafts[0].extraction_note)
        drafts = extract_events_local('завтра в 10:00 планёрка, а в 15:00 врач', 'Europe/Moscow', now)
        self.assertEqual([(d.start.day,d.start.hour) for d in drafts], [(28,10),(28,15)])

    async def test_recurrence_first_and_refill_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory)/'bot.db'); await db.connect()
            now = datetime.now(timezone.utc).replace(microsecond=0)
            draft = EventDraft(title='<private>', start=now+timedelta(hours=1), recurrence={'freq':'DAILY'})
            pk = await db.add_event(calendar_id='primary', event_id='x', title=draft.title,
                start_iso=draft.start.isoformat(), end_iso=None, rrule=draft.rrule)
            await schedule_for_event(db, pk, draft, 10)
            rows = await db.due_reminders((now+timedelta(days=20)).isoformat())
            self.assertEqual(len(rows), 8)
            self.assertEqual(rows[0]['start_iso'], draft.start.isoformat())
            await replenish_recurring(db, now)
            self.assertEqual((await db.stats())['reminders_pending'], 8)
            await replenish_recurring(db, now+timedelta(days=8))
            self.assertGreater((await db.stats())['reminders_pending'], 8)
            await db.add_reminder(event_pk=None,title='<test>',start_iso=now.isoformat(),remind_at=(now-timedelta(seconds=1)).isoformat(),minutes_before=0)
            bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('offline')))
            cfg = Config(allowed_user_id=1)
            self.assertEqual(await sweep(bot, db, cfg), 0)
            self.assertEqual(len(await db.due_reminders(now.isoformat())), 1)
            bot.send_message = AsyncMock()
            self.assertEqual(await sweep(bot, db, cfg, 2), 1)
            self.assertEqual(bot.send_message.call_args.args[0], 2)
            self.assertIn('&lt;test&gt;', bot.send_message.call_args.args[1])
            await db.close()

    async def test_local_daily_and_weekdays(self):
        now = datetime(2026,9,27,19,0,tzinfo=get_tz('Europe/Moscow'))
        for text, freq, hour in [('каждый день в 09:00 зарядка', 'DAILY', 9),
                                 ('по будням в 11:00 дейлик', 'WEEKLY', 11)]:
            drafts = extract_events_local(text, 'Europe/Moscow', now)
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].start.day, 28)
            self.assertEqual(drafts[0].start.hour, hour)
            self.assertEqual(drafts[0].recurrence['freq'], freq)

    async def test_ocr_limits(self):
        from bot.services.ocr import recognize_image
        from unittest.mock import patch
        with patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn:
            with self.assertRaises(ValueError):
                await recognize_image(b'x' * (5*1024*1024+1))
            spawn.assert_not_called()
            spawn.side_effect = FileNotFoundError()
            with self.assertRaisesRegex(ValueError, 'OCR не установлен'):
                await recognize_image(b'fake')

    async def test_monthly_rrule(self):
        draft = EventDraft(title='monthly', start=datetime(2026,1,31,tzinfo=timezone.utc),recurrence={'freq':'MONTHLY'})
        self.assertEqual([d.month for d in draft.next_occurrences(2)], [3,5])

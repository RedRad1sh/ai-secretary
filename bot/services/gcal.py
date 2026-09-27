"""Google Calendar: OAuth 2.0 refresh + events.insert/delete/list (ТЗ §2.4).

google-api-python-client синхронный — все вызовы уходят в asyncio.to_thread,
чтобы не блокировать event loop и уложиться в 5 секунд (ТЗ §3.3).
Google-библиотеки импортируются лениво: на serverless (YC Functions) это
экономит ~2 секунды холодного старта, когда календарь ещё не настроен.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from bot.models import EventDraft

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GCalError(RuntimeError):
    pass


class GCalClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._creds = None
        self._service = None

    # ---------- ленивая инициализация ----------

    def _get_creds(self):
        if self._creds is None:
            from google.oauth2.credentials import Credentials

            self._creds = Credentials(
                token=None,
                refresh_token=self._refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=self._client_id,
                client_secret=self._client_secret,
                scopes=SCOPES,
            )
        if self._creds.expired or not self._creds.valid:
            from google.auth.transport.requests import Request

            self._creds.refresh(Request())
        return self._creds

    def _get_service(self):
        from googleapiclient.discovery import build

        if self._service is None:
            self._service = build(
                "calendar", "v3", credentials=self._get_creds(), cache_discovery=False
            )
        return self._service

    # ---------- создание (ТЗ §2.1 п.5) ----------

    async def create_event(
        self, draft: EventDraft, calendar_id: str, tz_name: str
    ) -> dict[str, Any]:
        if draft.start is None:
            raise GCalError("У события нет даты")

        if draft.all_day:
            d = draft.start.date().isoformat()
            start_body: dict[str, Any] = {"date": d}
            end_body: dict[str, Any] = {"date": draft.end.date().isoformat() if draft.end else d}
        else:
            start_body = {"dateTime": draft.start.isoformat(), "timeZone": tz_name}
            end_body = {
                "dateTime": (draft.end or draft.start).isoformat(),
                "timeZone": tz_name,
            }

        body: dict[str, Any] = {
            "summary": draft.title,
            "start": start_body,
            "end": end_body,
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 10}],
            },
        }
        if draft.location:
            body["location"] = draft.location
        desc_parts = []
        if draft.participants:
            desc_parts.append(f"Участники: {draft.participants}")
        if draft.description:
            desc_parts.append(draft.description)
        if desc_parts:
            body["description"] = "\n".join(desc_parts)
        if draft.rrule:
            body["recurrence"] = [f"RRULE:{draft.rrule}"]

        return await asyncio.to_thread(self._create_sync, calendar_id, body)

    def _create_sync(self, calendar_id: str, body: dict) -> dict:
        from googleapiclient.errors import HttpError

        try:
            return (
                self._get_service()
                .events()
                .insert(calendarId=calendar_id, body=body)
                .execute()
            )
        except HttpError as e:
            raise GCalError(f"Google Calendar: {e.status_code} {e.reason}") from e

    # ---------- удаление (/undo) ----------

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, calendar_id, event_id)

    def _delete_sync(self, calendar_id: str, event_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._get_service().events().delete(
                calendarId=calendar_id, eventId=event_id
            ).execute()
        except HttpError as e:
            if e.status_code == 410:  # уже удалено вручную
                return
            raise GCalError(f"Google Calendar: {e.status_code} {e.reason}") from e

    # ---------- изменение времени (перенос) ----------

    async def update_event_times(
        self, calendar_id: str, event_id: str,
        start: datetime, end: datetime, tz_name: str,
    ) -> None:
        await asyncio.to_thread(self._update_sync, calendar_id, event_id, start, end, tz_name)

    def _update_sync(self, calendar_id: str, event_id: str,
                     start: datetime, end: datetime, tz_name: str) -> None:
        from googleapiclient.errors import HttpError

        body = {
            "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
        }
        try:
            self._get_service().events().patch(
                calendarId=calendar_id, eventId=event_id, body=body
            ).execute()
        except HttpError as e:
            raise GCalError(f"Google Calendar: {e.status_code} {e.reason}") from e

    # ---------- список (альтернатива /list из БД) ----------

    async def list_upcoming(self, calendar_id: str, limit: int = 10) -> list[dict]:
        return await asyncio.to_thread(self._list_sync, calendar_id, limit)

    def _list_sync(self, calendar_id: str, limit: int) -> list[dict]:
        from googleapiclient.errors import HttpError

        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            res = (
                self._get_service()
                .events()
                .list(
                    calendarId=calendar_id,
                    timeMin=now_iso,
                    maxResults=limit,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            return res.get("items", [])
        except HttpError as e:
            raise GCalError(f"Google Calendar: {e.status_code} {e.reason}") from e

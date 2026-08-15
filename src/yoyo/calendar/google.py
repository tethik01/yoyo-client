"""Google Calendar adapter — read only.

Shares the OAuth *client registration* with the Gmail adapter: the same "Desktop app" client
in Cloud Console, so enabling calendar is adding an API and a scope, not a second setup.
Tokens are still per-service files, because a calendar token and a mail token grant
different things and mixing them would make revoking one revoke both.

Scope, deliberately minimal:
  calendar.readonly  — list and read events. Cannot create, modify, delete or RSVP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .base import AuthRequired, CalendarError, Event, parse_iso

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


@dataclass
class GoogleCalendarProvider:
    account: str
    client_secrets: Path
    token_path: Path
    calendar_id: str = "primary"
    name: str = "google"

    # ------------------------------------------------------------- auth ---

    def _credentials(self):  # noqa: ANN202
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise CalendarError(
                'Google Calendar needs the mail extra: uv pip install -e ".[mail]"'
            ) from exc

        if not self.token_path.exists():
            raise AuthRequired(
                f"No Google Calendar token for {self.account!r}. "
                f"Run: yoyo calendar auth {self.account}"
            )
        creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save(creds)
        if not creds.valid:
            raise AuthRequired(
                f"Google Calendar token for {self.account!r} is not valid. "
                f"Re-run: yoyo calendar auth {self.account}"
            )
        return creds

    def _save(self, creds) -> None:  # noqa: ANN001
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        try:  # best effort; Windows ACLs are not POSIX modes
            self.token_path.chmod(0o600)
        except OSError:
            pass

    def authenticate(self, interactive: bool = True) -> str:
        if not interactive:
            raise AuthRequired("Google Calendar authentication requires a browser")
        if not self.client_secrets.exists():
            raise CalendarError(
                f"Missing Google client secrets at {self.client_secrets}. You can reuse the "
                f"same 'Desktop app' OAuth client as Gmail — just enable the Google Calendar "
                f"API on the same project and point client_secrets at the same JSON."
            )
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise CalendarError(
                'Google Calendar auth needs the mail extra: uv pip install -e ".[mail]"'
            ) from exc
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        self._save(creds)
        return f"authenticated {self.account} (google calendar, read-only)"

    def is_authenticated(self) -> bool:
        try:
            self._credentials()
        except (AuthRequired, CalendarError):
            return False
        return True

    # ------------------------------------------------------------ calls ---

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        creds = self._credentials()
        r = httpx.get(
            f"{API}{path}",
            params=params,
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=60,
        )
        if r.status_code >= 400:
            raise CalendarError(f"Google Calendar {path} failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    # ------------------------------------------------------------- read ---

    def events(self, start: datetime, end: datetime, limit: int = 50) -> list[Event]:
        data = self._get(
            f"/calendars/{self.calendar_id}/events",
            {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                # Without this a weekly stand-up returns as one recurrence rule rather than
                # the instances in the window, and "what's on Tuesday" silently misses it.
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": min(limit, 250),
            },
        )
        return [self._event(item) for item in data.get("items", [])[:limit]]

    def search(self, query: str, limit: int = 20) -> list[Event]:
        data = self._get(
            f"/calendars/{self.calendar_id}/events",
            {"q": query, "singleEvents": "true", "orderBy": "startTime",
             "maxResults": min(limit, 250)},
        )
        return [self._event(item) for item in data.get("items", [])[:limit]]

    # ------------------------------------------------------------ parse ---

    def _event(self, raw: dict[str, Any]) -> Event:
        start_block = raw.get("start") or {}
        end_block = raw.get("end") or {}
        # An all-day event carries `date`, a timed one carries `dateTime`. Reading only
        # dateTime silently drops every all-day event from the day view.
        all_day = "date" in start_block and "dateTime" not in start_block

        start = parse_iso(
            start_block.get("dateTime") or start_block.get("date"),
            assume_tz=start_block.get("timeZone"),
        )
        end = parse_iso(
            end_block.get("dateTime") or end_block.get("date"),
            assume_tz=end_block.get("timeZone"),
        )

        attendees = [
            a.get("email", "")
            for a in raw.get("attendees", [])
            if a.get("email") and not a.get("resource")
        ]
        mine = next((a for a in raw.get("attendees", []) if a.get("self")), None)
        response = (mine or {}).get("responseStatus", "")
        if response == "needsAction":
            response = "none"

        return Event(
            id=raw.get("id", ""),
            account=self.account,
            title=raw.get("summary", "(no title)"),
            start=start,
            end=end,
            all_day=all_day,
            location=raw.get("location", "") or "",
            organiser=(raw.get("organizer") or {}).get("email", ""),
            attendees=attendees,
            description=(raw.get("description") or "")[:4000],
            status=raw.get("status", ""),
            response=response,
            online_url=(raw.get("hangoutLink") or ""),
            calendar=self.calendar_id,
            recurring=bool(raw.get("recurringEventId")),
        )

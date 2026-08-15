"""Microsoft 365 calendar adapter — read only, over Microsoft Graph.

Shares the Entra app registration with the mail adapter: same Application ID, one more
delegated permission. MSAL's device-code flow again, so no redirect URI is needed.

Scope, deliberately minimal:
  Calendars.Read  — list and read events. Cannot create, modify, delete or RSVP.

`Calendars.ReadWrite` is deliberately NOT requested, so writing is impossible at the token
level rather than merely absent from this code — the same argument as `Mail.Send`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .base import AuthRequired, CalendarError, Event, parse_iso
from .base import html_free as _html_free

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0/me"
SCOPES = ["Calendars.Read"]

_SELECT = (
    "id,subject,start,end,isAllDay,location,organizer,attendees,bodyPreview,"
    "showAs,isCancelled,onlineMeeting,onlineMeetingUrl,seriesMasterId,responseStatus"
)


@dataclass
class GraphCalendarProvider:
    account: str
    client_id: str
    token_path: Path
    tenant: str = "common"
    name: str = "microsoft"

    # ------------------------------------------------------------- auth ---

    def _app(self):  # noqa: ANN202
        try:
            import msal
        except ImportError as exc:
            raise CalendarError(
                'Microsoft Calendar needs the mail extra: uv pip install -e ".[mail]"'
            ) from exc

        cache = msal.SerializableTokenCache()
        if self.token_path.exists():
            cache.deserialize(self.token_path.read_text(encoding="utf-8"))
        app = msal.PublicClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant}",
            token_cache=cache,
        )
        return app, cache

    def _persist(self, cache) -> None:  # noqa: ANN001
        if not cache.has_state_changed:
            return
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(cache.serialize(), encoding="utf-8")
        try:
            self.token_path.chmod(0o600)
        except OSError:
            pass

    def _token(self) -> str:
        app, cache = self._app()
        accounts = app.get_accounts()
        if not accounts:
            raise AuthRequired(
                f"No Microsoft Calendar token for {self.account!r}. "
                f"Run: yoyo calendar auth {self.account}"
            )
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        self._persist(cache)
        if not result or "access_token" not in result:
            raise AuthRequired(
                f"Microsoft Calendar token for {self.account!r} could not be refreshed. "
                f"Re-run: yoyo calendar auth {self.account}"
            )
        return result["access_token"]

    def authenticate(self, interactive: bool = True) -> str:
        if not interactive:
            raise AuthRequired("Microsoft authentication requires a browser")
        app, cache = self._app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise CalendarError(
                f"Could not start device flow: {flow.get('error_description')}"
            )
        print(flow["message"], flush=True)  # noqa: T201 - the user must see the code
        result = app.acquire_token_by_device_flow(flow)
        self._persist(cache)
        if "access_token" not in result:
            raise CalendarError(
                f"Microsoft auth failed: {result.get('error_description', result)}"
            )
        return f"authenticated {self.account} (microsoft calendar, read-only)"

    def is_authenticated(self) -> bool:
        try:
            self._token()
        except (AuthRequired, CalendarError):
            return False
        return True

    # ------------------------------------------------------------ calls ---

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = httpx.get(
            f"{GRAPH}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {self._token()}",
                # Ask Graph to render times in the local zone rather than UTC. Without it
                # every event comes back as UTC and a naive read shows the wrong hour.
                "Prefer": f'outlook.timezone="{_local_windows_zone()}"',
            },
            timeout=60,
        )
        if r.status_code >= 400:
            raise CalendarError(f"Graph {path} failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    # ------------------------------------------------------------- read ---

    def events(self, start: datetime, end: datetime, limit: int = 50) -> list[Event]:
        # calendarView, not /events: it expands recurring series into instances. /events
        # returns the series master, so a weekly stand-up would be invisible on any given
        # day — the same trap as Google's singleEvents.
        data = self._get(
            "/calendarView",
            {
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$select": _SELECT,
                "$orderby": "start/dateTime",
                "$top": min(limit, 100),
            },
        )
        return [self._event(item) for item in data.get("value", [])[:limit]]

    def search(self, query: str, limit: int = 20) -> list[Event]:
        data = self._get(
            "/events",
            {"$search": f'"{query}"', "$select": _SELECT, "$top": min(limit, 100)},
        )
        return [self._event(item) for item in data.get("value", [])[:limit]]

    # ------------------------------------------------------------ parse ---

    def _event(self, raw: dict[str, Any]) -> Event:
        start_block = raw.get("start") or {}
        end_block = raw.get("end") or {}
        attendees = [
            (a.get("emailAddress") or {}).get("address", "")
            for a in raw.get("attendees", [])
            if (a.get("emailAddress") or {}).get("address")
        ]
        response = ((raw.get("responseStatus") or {}).get("response") or "").lower()
        response = {"notresponded": "none", "organizer": "accepted"}.get(response, response)

        online = raw.get("onlineMeetingUrl") or (
            (raw.get("onlineMeeting") or {}).get("joinUrl") or ""
        )

        return Event(
            id=raw.get("id", ""),
            account=self.account,
            title=raw.get("subject", "(no title)"),
            start=parse_iso(start_block.get("dateTime"), assume_tz=start_block.get("timeZone")),
            end=parse_iso(end_block.get("dateTime"), assume_tz=end_block.get("timeZone")),
            all_day=bool(raw.get("isAllDay")),
            location=((raw.get("location") or {}).get("displayName") or ""),
            organiser=((raw.get("organizer") or {}).get("emailAddress") or {}).get("address", ""),
            attendees=attendees,
            description=_html_free(raw.get("bodyPreview") or "")[:4000],
            status="cancelled" if raw.get("isCancelled") else "confirmed",
            response=response,
            online_url=online,
            calendar="primary",
            recurring=bool(raw.get("seriesMasterId")),
        )


def _local_windows_zone() -> str:
    """Graph wants an IANA or Windows zone name in the Prefer header.

    Falls back to UTC rather than guessing: a wrong zone name makes Graph ignore the header
    and return UTC anyway, so the failure mode is identical and there is nothing to gain
    from a creative guess.
    """
    try:
        name = datetime.now().astimezone().tzname()
        return name if name and name.isascii() else "UTC"
    except Exception:  # noqa: BLE001
        return "UTC"

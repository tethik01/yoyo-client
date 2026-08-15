"""Provider-neutral calendar types and the adapter contract.

Same shape and same reasoning as `mail/base.py`: one normalised `Event` so nothing above
this package branches on whether a meeting came from Google or Microsoft.

**Capability boundary: read only.** No create, no update, no delete, no RSVP. Mail has a
draft path because a draft is inert until a human sends it; a calendar has no equivalent
inert state — a tentative event still appears on other people's calendars and still sends
invitations. There is no way for Yoyo to propose a meeting without acting, so it does not
propose meetings. If that changes, the honest shape is writing a draft *note* to the vault
for the human to act on, not a write scope here.

**Timezones are always explicit.** Both APIs return offsets and both are easy to drop.
A meeting silently rendered in the wrong zone is the exact class of confidently-wrong answer
this project keeps finding — and unlike a bad citation, the user does not see it until they
have missed the call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

#: `.1234567` in an ISO string — the fractional part only, so a following offset is left
#: untouched by the substitution.
_LONG_FRACTION = re.compile(r"\.(\d+)")


class CalendarError(RuntimeError):
    pass


class AuthRequired(CalendarError):
    """No usable token. Recoverable by `yoyo calendar auth <account>`."""


@dataclass(slots=True)
class Event:
    id: str
    account: str
    title: str = ""
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    location: str = ""
    organiser: str = ""
    attendees: list[str] = field(default_factory=list)
    description: str = ""
    status: str = ""            # confirmed | tentative | cancelled
    response: str = ""          # your own RSVP: accepted | declined | tentative | none
    online_url: str = ""
    calendar: str = ""
    recurring: bool = False

    @property
    def duration_minutes(self) -> int | None:
        if not self.start or not self.end:
            return None
        return max(0, int((self.end - self.start).total_seconds() // 60))

    def overlaps(self, other: Event) -> bool:
        """Half-open intervals: a 10:00–11:00 and an 11:00–12:00 do NOT clash. Treating
        them as a conflict would flag every back-to-back day as double-booked."""
        if not (self.start and self.end and other.start and other.end):
            return False
        return self.start < other.end and other.start < self.end

    def as_dict(self, include_description: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "account": self.account,
            "title": self.title,
            # isoformat() keeps the offset. A naive string here is how a meeting ends up
            # rendered in the wrong timezone.
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "all_day": self.all_day,
            "duration_minutes": self.duration_minutes,
            "location": self.location,
            "organiser": self.organiser,
            "attendee_count": len(self.attendees),
            "status": self.status,
            "your_response": self.response,
            "online": bool(self.online_url),
            "recurring": self.recurring,
        }
        if self.attendees[:20]:
            out["attendees"] = self.attendees[:20]
        if include_description:
            out["description"] = self.description
        return out


class Provider(Protocol):
    """What every calendar adapter must implement. Read only — by design."""

    name: str

    def authenticate(self, interactive: bool = True) -> str: ...
    def is_authenticated(self) -> bool: ...
    def events(self, start: datetime, end: datetime, limit: int = 50) -> list[Event]: ...
    def search(self, query: str, limit: int = 20) -> list[Event]: ...


# ------------------------------------------------------------------ helpers ---


def day_bounds(day: date, tz: timezone | None = None) -> tuple[datetime, datetime]:
    """Midnight to midnight in the LOCAL zone, returned as aware datetimes.

    Local, not UTC: "what's on today" means the user's today. Asking a US-evening user for
    UTC-today would show them tomorrow morning's meetings and hide tonight's.
    """
    tz = tz or datetime.now().astimezone().tzinfo
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return start, start + timedelta(days=1)


def parse_iso(raw: str | None, assume_tz: str | None = None) -> datetime | None:
    """Both APIs emit ISO-8601, and both sometimes omit the offset.

    A naive result is stamped with `assume_tz` when the API told us the zone separately
    (Graph does this — the offset is in a sibling field), and with the local zone otherwise.
    Returning a naive datetime would let it silently compare against aware ones and raise,
    or worse, be formatted as if it were local when it is not.
    """
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    # Graph returns 7 fractional digits; fromisoformat accepts at most 6. Trim the digits
    # and KEEP whatever follows — an earlier version cut at digit six and silently dropped
    # the trailing "+02:00", turning every Microsoft event into UTC. That is the exact
    # failure mode this module exists to prevent, and it survived one round of tests.
    text = _LONG_FRACTION.sub(lambda m: "." + m.group(1)[:6], text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed
    if assume_tz:
        try:
            from zoneinfo import ZoneInfo

            return parsed.replace(tzinfo=ZoneInfo(assume_tz))
        except Exception:  # noqa: BLE001 - unknown zone name; fall through to local
            pass
    return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)


def html_free(text: str) -> str:
    """Graph's `bodyPreview` is usually plain but occasionally carries markup. Reuses the
    mail helper rather than growing a second HTML stripper that drifts from the first."""
    if not text or "<" not in text:
        return (text or "").strip()
    from ..mail.base import html_to_text

    return html_to_text(text)


def find_conflicts(events: list[Event]) -> list[tuple[Event, Event]]:
    """Overlapping pairs, ignoring cancelled and declined events.

    Declined ones are excluded because they are on the calendar but not on the person —
    reporting them as conflicts manufactures a problem the user already resolved.
    """
    live = [
        e
        for e in events
        if e.start and e.end and not e.all_day
        and e.status != "cancelled"
        and e.response != "declined"
    ]
    live.sort(key=lambda e: e.start)  # type: ignore[arg-type,return-value]
    out = []
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            if b.start >= a.end:  # type: ignore[operator]
                break             # sorted, so nothing later can overlap either
            if a.overlaps(b):
                out.append((a, b))
    return out


def summarise(events: list[Event]) -> dict[str, Any]:
    busy = sum(e.duration_minutes or 0 for e in events if not e.all_day)
    return {
        "count": len(events),
        "all_day": sum(1 for e in events if e.all_day),
        "busy_minutes": busy,
        "conflicts": len(find_conflicts(events)),
        "declined": sum(1 for e in events if e.response == "declined"),
    }

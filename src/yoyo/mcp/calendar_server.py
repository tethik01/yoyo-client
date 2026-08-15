"""MCP server exposing calendar events. Read only.

    python -m yoyo.mcp.calendar_server       # or: yoyo mcp serve-calendar

There is no create/update/delete/RSVP tool and there is no scope that would permit one —
see `yoyo.calendar.base` for why a calendar has no inert "draft" state the way mail does.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime

from .. import calendar as cal
from ._compat import make_server

log = logging.getLogger(__name__)

server = make_server("yoyo-calendar")

MAX_EVENTS = 100
MAX_DAYS = 62


def _parse_day(raw: str) -> date:
    """ISO only. No "tomorrow", no "next Friday".

    The model has `current_time` for the clock and can do the arithmetic itself; letting it
    pass vague words here would put date interpretation in two places and guarantee they
    disagree. A calendar answer for the wrong day is not visibly wrong until the user has
    missed the meeting.
    """
    if not raw:
        return date.today()
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"day must be YYYY-MM-DD, got {raw!r}. Call current_time and compute the date "
            f"yourself rather than passing a relative word."
        ) from exc


def _pack(events, include_description: bool = False) -> dict:
    return {
        "count": len(events),
        "summary": cal.summarise(events),
        "events": [e.as_dict(include_description=include_description) for e in events],
    }


@server.tool(
    description=(
        "Events for a day or a range, merged across every enabled calendar account. "
        "day is YYYY-MM-DD (default today); days extends the window forward. Times are "
        "returned as ISO-8601 WITH offsets — report them in the user's local zone and never "
        "strip the offset. Merging accounts is deliberate: a work meeting clashing with a "
        "personal appointment is only visible when both are in one list."
    )
)
def calendar_agenda(day: str = "", days: int = 1, account: str = "") -> dict:
    events = cal.agenda(
        day=_parse_day(day),
        days=min(max(1, days), MAX_DAYS),
        account=account or None,
        limit=MAX_EVENTS,
    )
    return _pack(events)


@server.tool(
    description=(
        "Overlapping meetings in a window. Ignores cancelled events and ones the user has "
        "already declined — those are on the calendar but not on the person, and reporting "
        "them invents a problem that is already resolved. Back-to-back meetings (one ends "
        "exactly as the next begins) are NOT conflicts."
    )
)
def calendar_conflicts(day: str = "", days: int = 7) -> dict:
    events = cal.agenda(
        day=_parse_day(day), days=min(max(1, days), MAX_DAYS), limit=MAX_EVENTS
    )
    pairs = cal.find_conflicts(events)
    return {
        "count": len(pairs),
        "conflicts": [
            {
                "a": a.as_dict(),
                "b": b.as_dict(),
                "overlap_starts": max(a.start, b.start).isoformat(),
            }
            for a, b in pairs
        ],
    }


@server.tool(
    description=(
        "Search calendar events by text across title, body and attendees. Use this for "
        "'when is the review meeting' rather than scanning an agenda day by day."
    )
)
def calendar_search(query: str, account: str = "", limit: int = 20) -> dict:
    provider = cal.resolve(account or None)
    events = provider.search(query, limit=min(max(1, limit), MAX_EVENTS))
    return _pack(events)


@server.tool(
    description=(
        "Free gaps of at least min_minutes between working hours on a given day, across "
        "all enabled calendars. Reports availability ONLY — it cannot book anything, and "
        "the user must schedule the meeting themselves."
    )
)
def calendar_free_slots(
    day: str = "",
    min_minutes: int = 30,
    work_start_hour: int = 9,
    work_end_hour: int = 18,
) -> dict:
    target = _parse_day(day)
    events = cal.agenda(day=target, days=1, limit=MAX_EVENTS)
    start_of_day, _ = cal.day_bounds(target)
    tz = start_of_day.tzinfo

    busy = sorted(
        (
            (e.start, e.end)
            for e in events
            if e.start and e.end and not e.all_day
            and e.status != "cancelled"
            and e.response != "declined"
        ),
        key=lambda pair: pair[0],
    )

    cursor = start_of_day.replace(hour=max(0, min(23, work_start_hour)), minute=0)
    closing = start_of_day.replace(hour=max(1, min(23, work_end_hour)), minute=0)
    slots = []
    for begin, finish in busy:
        if begin > cursor:
            gap = int((min(begin, closing) - cursor).total_seconds() // 60)
            if gap >= min_minutes:
                slots.append({"start": cursor.isoformat(), "minutes": gap})
        cursor = max(cursor, finish)
        if cursor >= closing:
            break
    if cursor < closing:
        gap = int((closing - cursor).total_seconds() // 60)
        if gap >= min_minutes:
            slots.append({"start": cursor.isoformat(), "minutes": gap})

    return {
        "day": target.isoformat(),
        "timezone": str(tz),
        "working_hours": f"{work_start_hour:02d}:00-{work_end_hour:02d}:00",
        "count": len(slots),
        "free_slots": slots,
        "note": "Availability only — Yoyo cannot create calendar entries.",
    }


@server.tool(description="Configured calendar accounts and whether each is authenticated.")
def calendar_accounts() -> dict:
    return {"accounts": cal.status()}


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    accounts = cal.enabled_accounts()
    if not accounts:
        # stderr, captured and relayed by the client — a bare "Connection closed" tells the
        # user nothing about what to fix.
        print(
            "yoyo-calendar: no enabled accounts in yoyo-calendar.yaml. "
            "Configure one and run `yoyo calendar auth <name>`.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    log.info("yoyo-calendar serving %s over stdio", ", ".join(a.name for a in accounts))
    server.run("stdio")


if __name__ == "__main__":
    main()

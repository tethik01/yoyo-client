"""Calendar tests — offline. No OAuth, no network, no live account.

Every network path is untested by construction, exactly as with mail, and that is recorded
rather than papered over: these tests prove the parsing, the timezone handling, the conflict
maths and the absence of any write path. They prove nothing about whether Google's API
behaves as documented.

Timezone handling gets the most attention here because it is the failure the user cannot
see. A bad citation is visibly wrong; a meeting rendered an hour off looks completely normal
until it is missed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from yoyo import calendar as cal
from yoyo.calendar.base import Event, day_bounds, find_conflicts, parse_iso, summarise

UTC = UTC


def _event(hour: int, minutes: int = 60, **kw) -> Event:
    start = datetime(2026, 8, 14, hour, 0, tzinfo=UTC)
    return Event(
        id=kw.pop("id", f"e{hour}"),
        account="test",
        title=kw.pop("title", f"meeting at {hour}"),
        start=start,
        end=start + timedelta(minutes=minutes),
        **kw,
    )


# --------------------------------------------------------------- iso parsing ---


def test_offset_is_preserved_not_normalised_away():
    parsed = parse_iso("2026-08-14T09:00:00+05:30")
    assert parsed.utcoffset() == timedelta(hours=5, minutes=30)


def test_zulu_is_understood():
    assert parse_iso("2026-08-14T09:00:00Z").utcoffset() == timedelta(0)


def test_graph_seven_digit_fractional_seconds_do_not_break_parsing():
    """Graph returns 7 fractional digits; Python's fromisoformat historically took 6.
    Failing here would have made every Microsoft event vanish."""
    parsed = parse_iso("2026-08-14T09:00:00.1234567")
    assert parsed is not None and parsed.hour == 9


def test_fractional_seconds_with_an_offset_survive_the_trim():
    parsed = parse_iso("2026-08-14T09:00:00.1234567+02:00")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=2)


def test_a_naive_time_is_stamped_with_the_zone_the_api_reported():
    parsed = parse_iso("2026-08-14T09:00:00", assume_tz="Asia/Kolkata")
    assert parsed.utcoffset() == timedelta(hours=5, minutes=30)


def test_a_naive_time_never_comes_back_naive():
    """A naive datetime silently compares wrong against aware ones, or gets formatted as
    local when it is not. There is no code path that returns one."""
    assert parse_iso("2026-08-14T09:00:00").tzinfo is not None


def test_an_unknown_zone_name_falls_back_to_local_rather_than_crashing():
    assert parse_iso("2026-08-14T09:00:00", assume_tz="Mars/Olympus").tzinfo is not None


def test_garbage_and_empty_are_none_not_an_exception():
    for bad in ("", None, "not a date", "2026-13-45T99:99:99"):
        assert parse_iso(bad) is None


def test_a_date_only_value_parses_for_all_day_events():
    assert parse_iso("2026-08-14") is not None


# ------------------------------------------------------------------ day bounds ---


def test_day_bounds_are_local_midnight_to_midnight():
    """Local, not UTC: 'what's on today' means the user's today. A UTC window would show a
    US-evening user tomorrow's morning and hide tonight."""
    start, end = day_bounds(date(2026, 8, 14))
    assert start.hour == 0 and start.tzinfo is not None
    assert end - start == timedelta(days=1)


# ------------------------------------------------------------------- conflicts ---


def test_overlapping_meetings_are_a_conflict():
    a, b = _event(10), _event(10, id="b")
    assert a.overlaps(b)
    assert len(find_conflicts([a, b])) == 1


def test_back_to_back_meetings_are_not_a_conflict():
    """Half-open intervals. Treating 10-11 and 11-12 as a clash would flag every
    normally-busy day as double-booked and make the feature useless."""
    assert not _event(10).overlaps(_event(11))
    assert find_conflicts([_event(10), _event(11)]) == []


def test_partial_overlap_is_caught():
    a = _event(10, minutes=90)      # 10:00-11:30
    b = _event(11)                  # 11:00-12:00
    assert find_conflicts([a, b])


def test_declined_events_are_not_conflicts():
    """They are on the calendar but not on the person. Reporting them manufactures a
    problem the user already resolved."""
    a = _event(10)
    b = _event(10, id="b", response="declined")
    assert find_conflicts([a, b]) == []


def test_cancelled_events_are_not_conflicts():
    assert find_conflicts([_event(10), _event(10, id="b", status="cancelled")]) == []


def test_all_day_events_do_not_clash_with_everything():
    all_day = Event(id="x", account="t", title="Leave", all_day=True,
                    start=datetime(2026, 8, 14, tzinfo=UTC),
                    end=datetime(2026, 8, 15, tzinfo=UTC))
    assert find_conflicts([all_day, _event(10)]) == []


def test_events_without_times_are_skipped_rather_than_crashing():
    assert find_conflicts([Event(id="x", account="t"), _event(10)]) == []


def test_three_way_overlap_reports_each_pair():
    a, b, c = _event(10), _event(10, id="b"), _event(10, id="c")
    assert len(find_conflicts([a, b, c])) == 3


def test_conflicts_are_found_regardless_of_input_order():
    assert len(find_conflicts([_event(11, id="b"), _event(10, minutes=120)])) == 1


# --------------------------------------------------------------------- summary ---


def test_summary_counts_busy_minutes_excluding_all_day():
    all_day = Event(id="x", account="t", all_day=True,
                    start=datetime(2026, 8, 14, tzinfo=UTC),
                    end=datetime(2026, 8, 15, tzinfo=UTC))
    s = summarise([_event(9), _event(11, minutes=30), all_day])
    assert s["busy_minutes"] == 90
    assert s["all_day"] == 1
    assert s["count"] == 3


def test_duration_is_none_when_an_end_is_missing():
    assert Event(id="x", account="t", start=datetime.now(UTC)).duration_minutes is None


def test_serialised_times_keep_their_offset():
    """The dict is what reaches the model. A naive string here is how a meeting ends up
    reported in the wrong timezone."""
    payload = _event(9).as_dict()
    assert payload["start"].endswith("+00:00")


# ---------------------------------------------------------------------- config ---


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "yoyo-calendar.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_missing_config_is_no_accounts_not_a_crash():
    assert cal.load_accounts(Path("/nonexistent/yoyo-calendar.yaml")) == []


def test_accounts_are_read(tmp_path):
    path = _write(
        tmp_path,
        {"accounts": {
            "personal": {"provider": "google", "client_secrets": "secrets/g.json"},
            "work": {"provider": "microsoft", "client_id": "abc", "enabled": False},
        }},
    )
    specs = {s.name: s for s in cal.load_accounts(path)}
    assert specs["personal"].provider == "google"
    assert specs["work"].enabled is False


def test_unknown_provider_is_rejected(tmp_path):
    path = _write(tmp_path, {"accounts": {"x": {"provider": "fastmail"}}})
    with pytest.raises(ValueError, match="fastmail"):
        cal.load_accounts(path)


def test_calendar_tokens_are_separate_from_mail_tokens():
    """Revoking calendar access must not revoke mail access."""
    from yoyo import mail

    assert cal.token_dir() != mail.token_dir()
    assert cal.token_dir().name == "calendar-tokens"


def test_a_placeholder_client_id_is_caught_with_a_useful_message():
    spec = cal.AccountSpec(name="work", provider="microsoft",
                           client_id="REPLACE-WITH-APPLICATION-ID")
    with pytest.raises(cal.CalendarError, match="Entra app registration"):
        cal.build(spec)


def test_google_defaults_to_the_gmail_secrets_file():
    """The shared-registration decision made concrete: no second client to create."""
    provider = cal.build(cal.AccountSpec(name="personal", provider="google"))
    assert provider.client_secrets.name == "gmail-personal.json"


# ------------------------------------------------------------------ resolution ---


def test_no_enabled_accounts_says_so_plainly(monkeypatch):
    monkeypatch.setattr(cal, "enabled_accounts", lambda: [])
    with pytest.raises(cal.CalendarError, match="No calendar accounts are enabled"):
        cal.resolve()


def test_ambiguity_is_an_error_not_a_silent_pick(monkeypatch):
    """Answering from the wrong calendar looks exactly like an empty day."""
    monkeypatch.setattr(
        cal, "enabled_accounts",
        lambda: [cal.AccountSpec("a", "google"), cal.AccountSpec("b", "google")],
    )
    with pytest.raises(cal.CalendarError, match="name the one you want"):
        cal.resolve()


def test_an_unknown_account_name_lists_the_real_ones(monkeypatch):
    monkeypatch.setattr(cal, "enabled_accounts", lambda: [cal.AccountSpec("a", "google")])
    with pytest.raises(cal.CalendarError, match="unknown calendar account"):
        cal.resolve("typo")


# ---------------------------------------------------------------------- policy ---


def test_no_adapter_can_write_to_a_calendar():
    """Structural. A calendar has no inert draft state — a tentative event already appears
    on other people's calendars and already sends invitations."""
    import inspect

    from yoyo.calendar import google, graph

    banned = ("def create", "def update", "def delete", "def rsvp", "def respond")
    for module in (google, graph):
        source = inspect.getsource(module)
        for name in banned:
            assert name not in source, f"{module.__name__} has {name}"


def test_scopes_are_read_only():
    from yoyo.calendar import google, graph

    assert google.SCOPES == ["https://www.googleapis.com/auth/calendar.readonly"]
    assert graph.SCOPES == ["Calendars.Read"]
    for scope in google.SCOPES + graph.SCOPES:
        assert "ReadWrite" not in scope and "events" not in scope


def test_the_mcp_server_exposes_no_write_tool():
    import inspect

    from yoyo.mcp import calendar_server

    source = inspect.getsource(calendar_server)
    for banned in ("def calendar_create", "def calendar_update", "def calendar_delete"):
        assert banned not in source


def test_the_mcp_server_refuses_relative_dates():
    """Date interpretation in two places guarantees the two disagree. The model has a clock
    tool and can do the arithmetic itself."""
    from yoyo.mcp.calendar_server import _parse_day

    assert _parse_day("2026-08-14") == date(2026, 8, 14)
    assert _parse_day("") == date.today()
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _parse_day("tomorrow")


def test_a_negative_offset_also_survives_the_fractional_trim():
    """Regression: the first implementation cut at digit six and dropped everything after,
    silently converting every Microsoft event to UTC."""
    parsed = parse_iso("2026-08-14T09:00:00.1234567-07:00")
    assert parsed.utcoffset() == timedelta(hours=-7)


def test_short_fractional_seconds_are_left_alone():
    parsed = parse_iso("2026-08-14T09:00:00.123+02:00")
    assert parsed.microsecond == 123000
    assert parsed.utcoffset() == timedelta(hours=2)

"""Calendar: provider-neutral read-only access to Google and Microsoft 365.

Accounts are declared in `yoyo-calendar.yaml`. **The OAuth app registration is shared with
mail** — the same Google "Desktop app" client, the same Entra Application ID — so adding
calendar to a working mail setup is enabling an API and a scope, not a second registration.
Tokens are separate files per service, because revoking calendar access should not revoke
mail access.

Read only. See `base.py` for why there is no write path and why "just tentative" is not an
inert state the way an email draft is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..config import REPO_ROOT, get_settings
from .base import (
    AuthRequired,
    CalendarError,
    Event,
    day_bounds,
    find_conflicts,
    parse_iso,
    summarise,
)

log = logging.getLogger(__name__)

CONFIG_FILE = REPO_ROOT / "yoyo-calendar.yaml"


def config_path() -> Path:
    """Where accounts are declared.

    Overridable with YOYO_CALENDAR_CONFIG so the MCP server can be exercised against a known
    config instead of whatever the owner happens to have enabled. A test that reads the
    shipped file passes or fails depending on the developer's own setup, which makes it
    worthless as a check on behaviour — that is exactly how the "refuses to start with no
    accounts" test broke the moment a real calendar was configured.
    """
    import os

    override = os.environ.get("YOYO_CALENDAR_CONFIG")
    return Path(override) if override else CONFIG_FILE

__all__ = [
    "AccountSpec",
    "AuthRequired",
    "CalendarError",
    "Event",
    "agenda",
    "build",
    "day_bounds",
    "find_conflicts",
    "load_accounts",
    "parse_iso",
    "resolve",
    "status",
    "summarise",
    "token_dir",
]


@dataclass
class AccountSpec:
    name: str
    provider: str
    enabled: bool = True
    client_secrets: str | None = None    # google
    client_id: str | None = None         # microsoft
    tenant: str = "common"               # microsoft
    calendar_id: str = "primary"         # google
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def token_dir() -> Path:
    """Separate from mail-tokens/. Same disk, same OQ4 exposure, but revoking one service
    should not revoke the other."""
    return get_settings().data_dir / "calendar-tokens"


def load_accounts(path: Path | None = None) -> list[AccountSpec]:
    path = path or config_path()
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[AccountSpec] = []
    for name, body in (raw.get("accounts") or {}).items():
        body = dict(body or {})
        provider = body.pop("provider", None)
        if provider not in {"google", "microsoft"}:
            raise ValueError(
                f"calendar account {name!r}: provider must be 'google' or 'microsoft', "
                f"got {provider!r}"
            )
        known = {"enabled", "client_secrets", "client_id", "tenant", "calendar_id", "description"}
        out.append(
            AccountSpec(
                name=name,
                provider=provider,
                **{k: v for k, v in body.items() if k in known},
                extra={k: v for k, v in body.items() if k not in known},
            )
        )
    return out


def build(spec: AccountSpec):  # noqa: ANN201
    token_path = token_dir() / f"{spec.name}.json"

    if spec.provider == "google":
        from .google import GoogleCalendarProvider

        secrets = spec.client_secrets or f"secrets/gmail-{spec.name}.json"
        secrets_path = Path(secrets)
        return GoogleCalendarProvider(
            account=spec.name,
            client_secrets=secrets_path if secrets_path.is_absolute() else REPO_ROOT / secrets,
            token_path=token_path,
            calendar_id=spec.calendar_id,
        )

    from .graph import GraphCalendarProvider

    if not spec.client_id or spec.client_id.startswith("REPLACE"):
        raise CalendarError(
            f"calendar account {spec.name!r} has no Application ID. Reuse the same Entra "
            f"app registration as mail and add the delegated Calendars.Read permission."
        )
    return GraphCalendarProvider(
        account=spec.name,
        client_id=spec.client_id,
        token_path=token_path,
        tenant=spec.tenant,
    )


def enabled_accounts() -> list[AccountSpec]:
    return [s for s in load_accounts() if s.enabled]


def resolve(account: str | None = None):  # noqa: ANN201
    """One account by name, or the single enabled one.

    Ambiguity is an error rather than a silent pick: answering "what's on today" from the
    wrong calendar looks exactly like an empty day.
    """
    specs = enabled_accounts()
    if not specs:
        raise CalendarError(
            "No calendar accounts are enabled. Configure one in yoyo-calendar.yaml."
        )
    if account:
        match = next((s for s in specs if s.name == account), None)
        if not match:
            raise CalendarError(
                f"unknown calendar account {account!r}. Enabled: "
                f"{', '.join(s.name for s in specs)}"
            )
        return build(match)
    if len(specs) > 1:
        raise CalendarError(
            f"several calendar accounts are enabled ({', '.join(s.name for s in specs)}) — "
            f"name the one you want."
        )
    return build(specs[0])


def agenda(
    day: date | None = None,
    days: int = 1,
    account: str | None = None,
    limit: int = 100,
) -> list[Event]:
    """Events across every enabled account, or one named account, merged and sorted.

    Merged deliberately: a work meeting and a personal appointment at the same hour is a
    real conflict, and it is invisible if each calendar is only ever queried alone.
    """
    start_day = day or date.today()
    start, _ = day_bounds(start_day)
    _, end = day_bounds(start_day + timedelta(days=max(1, days) - 1))

    specs = [s for s in enabled_accounts() if not account or s.name == account]
    if account and not specs:
        raise CalendarError(f"unknown or disabled calendar account {account!r}")

    events: list[Event] = []
    for spec in specs:
        try:
            events.extend(build(spec).events(start, end, limit=limit))
        except (AuthRequired, CalendarError) as exc:
            # One unauthenticated account must not blank the whole agenda — but it must not
            # be silent either, or a missing meeting looks like a free slot.
            log.warning("calendar account %s unavailable: %s", spec.name, exc)
            raise
    events.sort(key=lambda e: (e.start is None, e.start or datetime.max))
    return events[:limit]


def status() -> list[dict[str, Any]]:
    rows = []
    for spec in load_accounts():
        row = {
            "account": spec.name,
            "provider": spec.provider,
            "enabled": spec.enabled,
            "description": spec.description,
            "authenticated": None,
            "error": "",
        }
        if spec.enabled:
            try:
                row["authenticated"] = build(spec).is_authenticated()
            except CalendarError as exc:
                row["error"] = str(exc)
                row["authenticated"] = False
        rows.append(row)
    return rows

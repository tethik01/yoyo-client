"""Mail: provider-neutral read and draft access.

Accounts are declared in `yoyo-mail.yaml`; each maps to a provider adapter. Consumers ask
for an account by name and get the same `Message` shape back either way.

Tokens live under `data/mail-tokens/`. They are long-lived credentials to a whole mailbox
and the disk is currently unencrypted (OQ4) — that trade-off is recorded, not overlooked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import REPO_ROOT, get_settings
from .base import AuthRequired, Draft, MailError, Message

log = logging.getLogger(__name__)

CONFIG_FILE = REPO_ROOT / "yoyo-mail.yaml"


@dataclass
class AccountSpec:
    name: str
    provider: str
    enabled: bool = True
    client_secrets: str | None = None   # gmail
    client_id: str | None = None        # microsoft
    tenant: str = "common"              # microsoft
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def token_dir() -> Path:
    return get_settings().data_dir / "mail-tokens"


def load_accounts(path: Path | None = None) -> list[AccountSpec]:
    path = path or CONFIG_FILE
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[AccountSpec] = []
    for name, body in (raw.get("accounts") or {}).items():
        body = dict(body or {})
        provider = body.pop("provider", None)
        if provider not in {"gmail", "microsoft"}:
            raise ValueError(
                f"account {name!r}: provider must be 'gmail' or 'microsoft', got {provider!r}"
            )
        known = {"enabled", "client_secrets", "client_id", "tenant", "description"}
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
    """Instantiate the adapter for one account."""
    token_path = token_dir() / f"{spec.name}.json"

    if spec.provider == "gmail":
        from .gmail import GmailProvider

        secrets = spec.client_secrets or f"secrets/gmail-{spec.name}.json"
        return GmailProvider(
            account=spec.name,
            client_secrets=(REPO_ROOT / secrets) if not Path(secrets).is_absolute() else Path(secrets),
            token_path=token_path,
        )

    from .graph import GraphProvider

    if not spec.client_id:
        raise MailError(
            f"account {spec.name!r}: microsoft accounts need a client_id "
            f"(the Application ID of your Entra app registration)"
        )
    return GraphProvider(
        account=spec.name,
        client_id=spec.client_id,
        tenant=spec.tenant,
        token_path=token_path,
    )


def providers(path: Path | None = None) -> dict[str, Any]:
    return {s.name: build(s) for s in load_accounts(path) if s.enabled}


def resolve(account: str | None, path: Path | None = None):  # noqa: ANN201
    """Pick an account. With one configured, naming it is optional."""
    available = providers(path)
    if not available:
        raise MailError(
            "No mail accounts configured. Add one to yoyo-mail.yaml, then: yoyo mail auth <name>"
        )
    if account:
        try:
            return available[account]
        except KeyError as exc:
            raise MailError(
                f"Unknown account {account!r}. Configured: {', '.join(sorted(available))}"
            ) from exc
    if len(available) == 1:
        return next(iter(available.values()))
    raise MailError(
        f"Several accounts configured ({', '.join(sorted(available))}) — name one explicitly."
    )


def status(path: Path | None = None) -> list[dict[str, Any]]:
    out = []
    for spec in load_accounts(path):
        entry = {
            "account": spec.name,
            "provider": spec.provider,
            "enabled": spec.enabled,
            "description": spec.description,
        }
        if spec.enabled:
            try:
                entry["authenticated"] = build(spec).is_authenticated()
            except MailError as exc:
                entry["authenticated"] = False
                entry["error"] = str(exc)
        out.append(entry)
    return out


__all__ = [
    "AccountSpec",
    "AuthRequired",
    "Draft",
    "MailError",
    "Message",
    "build",
    "load_accounts",
    "providers",
    "resolve",
    "status",
    "token_dir",
]

"""Microsoft 365 adapter — read and draft, over Microsoft Graph.

OAuth uses MSAL's device-code flow: you get a short code, open a page, and approve. That
avoids needing a redirect URI registered for a desktop app and works identically whether
you're on the laptop or over SSH.

Scopes, deliberately minimal:
  Mail.Read       — search and read
  Mail.ReadWrite  — create drafts

Graph has no draft-only scope; `Mail.ReadWrite` is the narrowest that permits creating a
draft. `Mail.Send` is deliberately NOT requested, so sending is impossible at the token
level, not merely absent from this code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .base import (
    AuthRequired,
    Draft,
    MailError,
    Message,
    html_to_text,
    truncate,
)

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0/me"
SCOPES = ["Mail.Read", "Mail.ReadWrite"]


@dataclass
class GraphProvider:
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
            raise MailError(
                "Microsoft support needs the mail extra: uv pip install -e \".[mail]\""
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
                f"No Microsoft token for account {self.account!r}. "
                f"Run: yoyo mail auth {self.account}"
            )
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        self._persist(cache)
        if not result or "access_token" not in result:
            raise AuthRequired(
                f"Microsoft token for {self.account!r} could not be refreshed. "
                f"Re-run: yoyo mail auth {self.account}"
            )
        return result["access_token"]

    def authenticate(self, interactive: bool = True) -> str:
        if not interactive:
            raise AuthRequired("Microsoft authentication requires a browser")
        app, cache = self._app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise MailError(f"Could not start device flow: {flow.get('error_description')}")

        # Printed, not opened: the user must see the code, and this may run headless.
        print(flow["message"], flush=True)  # noqa: T201

        result = app.acquire_token_by_device_flow(flow)
        self._persist(cache)
        if "access_token" not in result:
            raise MailError(
                f"Microsoft auth failed: {result.get('error_description', result)}"
            )
        return f"authenticated {self.account} (microsoft)"

    def is_authenticated(self) -> bool:
        try:
            self._token()
        except (AuthRequired, MailError):
            return False
        return True

    # ------------------------------------------------------------ calls ---

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = httpx.get(
            f"{GRAPH}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=60,
        )
        return _checked(r, path)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = httpx.post(
            f"{GRAPH}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=60,
        )
        return _checked(r, path)

    # ------------------------------------------------------------- read ---

    def search(self, query: str, limit: int = 20) -> list[Message]:
        # $search cannot be combined with $orderby in Graph; ask for relevance ordering
        # implicitly by omitting it rather than getting a 400 nobody can interpret.
        data = self._get(
            "/messages",
            {"$search": f'"{query}"', "$top": min(limit, 50), "$select": _SELECT},
        )
        return [_parse(m, self.account, include_body=False) for m in data.get("value", [])]

    def read(self, message_id: str) -> Message:
        data = self._get(f"/messages/{message_id}", {"$select": _SELECT + ",body"})
        return _parse(data, self.account, include_body=True)

    def thread(self, thread_id: str, limit: int = 50) -> list[Message]:
        data = self._get(
            "/messages",
            {
                "$filter": f"conversationId eq '{thread_id}'",
                "$top": min(limit, 50),
                "$select": _SELECT + ",body",
            },
        )
        return [_parse(m, self.account, include_body=True) for m in data.get("value", [])]

    # ------------------------------------------------------------ draft ---

    def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to_message_id: str | None = None,
    ) -> Draft:
        if reply_to_message_id:
            # createReply builds the quoted draft for us, then we set the body.
            created = self._post(f"/messages/{reply_to_message_id}/createReply", {})
            self._patch(
                f"/messages/{created['id']}",
                {"body": {"contentType": "text", "content": body}},
            )
            return Draft(
                id=created["id"],
                account=self.account,
                subject=created.get("subject", subject),
                to=to,
            )

        payload = {
            "subject": subject,
            "body": {"contentType": "text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "isDraft": True,
        }
        if cc:
            payload["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
        created = self._post("/messages", payload)
        return Draft(id=created["id"], account=self.account, subject=subject, to=to)

    def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = httpx.patch(
            f"{GRAPH}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=60,
        )
        return _checked(r, path)


# ------------------------------------------------------------------ parsing ---

_SELECT = "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,isRead"


def _checked(r: httpx.Response, path: str) -> dict[str, Any]:
    if r.status_code == 401:
        raise AuthRequired(f"Graph rejected the token for {path} (401)")
    if r.status_code == 403:
        raise MailError(f"Graph refused {path} (403). Likely a missing scope consent.")
    if r.status_code >= 400:
        raise MailError(f"Graph {path} failed [{r.status_code}]: {r.text[:300]}")
    if not r.content:
        return {}
    return r.json()


def _address(entry: dict[str, Any] | None) -> str:
    if not entry:
        return ""
    email = (entry.get("emailAddress") or {})
    name, addr = email.get("name"), email.get("address", "")
    return f"{name} <{addr}>" if name and addr else addr


def _parse(data: dict[str, Any], account: str, include_body: bool) -> Message:
    date = None
    raw_date = data.get("receivedDateTime")
    if raw_date:
        try:
            date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            date = None

    body = None
    if include_body:
        b = data.get("body") or {}
        content = b.get("content", "")
        body = truncate(
            html_to_text(content) if b.get("contentType", "").lower() == "html" else content
        )

    return Message(
        id=data.get("id", ""),
        account=account,
        thread_id=data.get("conversationId"),
        subject=data.get("subject") or "",
        sender=_address(data.get("from")),
        to=[_address(x) for x in data.get("toRecipients") or []],
        cc=[_address(x) for x in data.get("ccRecipients") or []],
        date=date,
        snippet=data.get("bodyPreview") or "",
        body=body,
        unread=not data.get("isRead", True),
    )

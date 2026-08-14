"""Gmail adapter — read and draft, over the Gmail REST API.

OAuth uses the installed-app flow: a browser opens, you consent, and a refresh token is
cached locally. Yoyo never sees your password, and the token grants only the scopes below.

Scopes, deliberately minimal:
  gmail.readonly  — search and read
  gmail.compose   — create drafts. Does NOT permit sending.

`gmail.compose` is the narrowest scope that allows draft creation; there is no draft-only
scope that excludes send from the *API surface*, but Yoyo exposes no send path and the
absence is enforced by this module having no send method at all.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from .base import (
    AuthRequired,
    Draft,
    MailError,
    Message,
    html_to_text,
    split_addresses,
    truncate,
)

log = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


@dataclass
class GmailProvider:
    account: str
    client_secrets: Path
    token_path: Path
    name: str = "gmail"

    # ------------------------------------------------------------- auth ---

    def _credentials(self):  # noqa: ANN202
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise MailError(
                "Gmail support needs the mail extra: uv pip install -e \".[mail]\""
            ) from exc

        if not self.token_path.exists():
            raise AuthRequired(
                f"No Gmail token for account {self.account!r}. "
                f"Run: yoyo mail auth {self.account}"
            )
        creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save(creds)
        if not creds.valid:
            raise AuthRequired(
                f"Gmail token for {self.account!r} is not valid. "
                f"Re-run: yoyo mail auth {self.account}"
            )
        return creds

    def _save(self, creds) -> None:  # noqa: ANN001
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        try:  # best effort; Windows ACLs are not POSIX modes
            self.token_path.chmod(0o600)
        except OSError:
            pass
        log.info("saved Gmail token for %s to %s", self.account, self.token_path)

    def authenticate(self, interactive: bool = True) -> str:
        if not interactive:
            raise AuthRequired("Gmail authentication requires a browser")

        # Check the file first: "your secrets file is missing" is the more useful message,
        # and it is true whether or not the optional dependency happens to be installed.
        if not self.client_secrets.exists():
            raise MailError(
                f"Missing Google client secrets at {self.client_secrets}. Create an OAuth "
                f"client of type 'Desktop app' in Google Cloud Console, download the JSON, "
                f"and save it there."
            )

        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise MailError(
                "Gmail auth needs the mail extra: uv pip install -e \".[mail]\""
            ) from exc
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        self._save(creds)
        return f"authenticated {self.account} (gmail)"

    def is_authenticated(self) -> bool:
        try:
            self._credentials()
        except (AuthRequired, MailError):
            return False
        return True

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        creds = self._credentials()
        r = httpx.get(
            f"{API}{path}",
            params=params,
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=60,
        )
        return _checked(r, path)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        creds = self._credentials()
        r = httpx.post(
            f"{API}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=60,
        )
        return _checked(r, path)

    # ------------------------------------------------------------- read ---

    def search(self, query: str, limit: int = 20) -> list[Message]:
        listing = self._get("/messages", {"q": query, "maxResults": min(limit, 100)})
        out: list[Message] = []
        for stub in listing.get("messages", [])[:limit]:
            out.append(self._message(stub["id"], full=False))
        return out

    def read(self, message_id: str) -> Message:
        return self._message(message_id, full=True)

    def thread(self, thread_id: str, limit: int = 50) -> list[Message]:
        data = self._get(f"/threads/{thread_id}", {"format": "full"})
        return [
            _parse_message(m, self.account, include_body=True)
            for m in data.get("messages", [])[:limit]
        ]

    def _message(self, message_id: str, full: bool) -> Message:
        data = self._get(
            f"/messages/{message_id}", {"format": "full" if full else "metadata"}
        )
        return _parse_message(data, self.account, include_body=full)

    # ------------------------------------------------------------ draft ---

    def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to_message_id: str | None = None,
    ) -> Draft:
        mime = build_mime(to, subject, body, cc)
        payload: dict[str, Any] = {"message": {"raw": b64url(mime)}}
        if reply_to_message_id:
            original = self._get(f"/messages/{reply_to_message_id}", {"format": "metadata"})
            payload["message"]["threadId"] = original.get("threadId")
        created = self._post("/drafts", payload)
        return Draft(id=created["id"], account=self.account, subject=subject, to=to)


# ------------------------------------------------------------------ parsing ---


def _checked(r: httpx.Response, path: str) -> dict[str, Any]:
    if r.status_code == 401:
        raise AuthRequired(f"Gmail rejected the token for {path} (401)")
    if r.status_code == 403:
        raise MailError(
            f"Gmail refused {path} (403). Usually a missing scope — re-authenticate."
        )
    if r.status_code >= 400:
        raise MailError(f"Gmail {path} failed [{r.status_code}]: {r.text[:300]}")
    return r.json()


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (payload.get("headers") or [])}


def _walk_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to converted HTML. Gmail nests parts arbitrarily."""
    plain: list[str] = []
    html: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                html.append(text)
        for sub in part.get("parts") or []:
            visit(sub)

    visit(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        return html_to_text("\n".join(html))
    return ""


def _parse_message(data: dict[str, Any], account: str, include_body: bool) -> Message:
    payload = data.get("payload") or {}
    h = _headers(payload)
    date = None
    if h.get("date"):
        try:
            date = parsedate_to_datetime(h["date"])
        except (TypeError, ValueError):
            date = None
    if date is None and data.get("internalDate"):
        date = datetime.fromtimestamp(int(data["internalDate"]) / 1000, tz=timezone.utc)

    labels = data.get("labelIds") or []
    return Message(
        id=data.get("id", ""),
        account=account,
        thread_id=data.get("threadId"),
        subject=h.get("subject", ""),
        sender=h.get("from", ""),
        to=split_addresses(h.get("to")),
        cc=split_addresses(h.get("cc")),
        date=date,
        snippet=data.get("snippet", ""),
        body=truncate(_walk_body(payload)) if include_body else None,
        unread="UNREAD" in labels,
        labels=labels,
    )


def build_mime(to: list[str], subject: str, body: str, cc: list[str] | None = None) -> bytes:
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def load_token_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

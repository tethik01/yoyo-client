"""Provider-neutral mail types and the adapter contract.

One normalised shape so the agent (and later LangGraph) sees a single tool surface
regardless of whether a message came from Gmail or Microsoft Graph. Two providers with two
tool vocabularies would push provider branching up into every consumer.

**Capability boundary:** read and draft. There is deliberately no send. Approval is you
pressing send in your own mail client — the same asymmetry as the vault, where Yoyo writes
drafts and a human promotes them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class MailError(RuntimeError):
    pass


class AuthRequired(MailError):
    """Raised when an account has no usable token. Recoverable by `yoyo mail auth`."""


@dataclass(slots=True)
class Message:
    id: str
    account: str
    thread_id: str | None = None
    subject: str = ""
    sender: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    date: datetime | None = None
    snippet: str = ""
    body: str | None = None
    unread: bool = False
    labels: list[str] = field(default_factory=list)

    def as_dict(self, include_body: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "account": self.account,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "from": self.sender,
            "to": self.to,
            "date": self.date.isoformat() if self.date else None,
            "snippet": self.snippet,
            "unread": self.unread,
        }
        if include_body:
            out["body"] = self.body
        return out


@dataclass(slots=True)
class Draft:
    id: str
    account: str
    subject: str
    to: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.id,
            "account": self.account,
            "subject": self.subject,
            "to": self.to,
            "status": "saved as a draft — not sent",
        }


class Provider(Protocol):
    """What every mail adapter must implement. Read and draft only."""

    name: str

    def authenticate(self, interactive: bool = True) -> str: ...
    def is_authenticated(self) -> bool: ...
    def search(self, query: str, limit: int = 20) -> list[Message]: ...
    def read(self, message_id: str) -> Message: ...
    def thread(self, thread_id: str, limit: int = 50) -> list[Message]: ...
    def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to_message_id: str | None = None,
    ) -> Draft: ...


# ------------------------------------------------------------------ helpers ---

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n[ \t]*")
_ADDR = re.compile(r"[^,;]+")


def html_to_text(html: str) -> str:
    """Crude but dependency-free. Mail bodies only need to be readable, not rendered."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _HTML_TAG.sub(" ", text)
    for entity, char in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"),
    ):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _WS.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_addresses(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [a.strip() for a in _ADDR.findall(raw) if a.strip()]


def truncate(text: str | None, limit: int = 20_000) -> str:
    """Mail bodies can be enormous and the server context ceiling is 32K tokens."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated — {len(text) - limit} more characters]"

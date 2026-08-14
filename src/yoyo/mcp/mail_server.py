"""MCP server exposing mail — read and draft, across Gmail and Microsoft 365.

    python -m yoyo.mcp.mail_server        # or: yoyo mcp serve-mail

One tool surface for both providers. There is **no send tool**, and for Microsoft the token
does not carry `Mail.Send` at all, so sending is impossible rather than merely unimplemented.
Approval is you pressing send in your own mail client.
"""

from __future__ import annotations

import logging
import sys

from .. import mail
from ._compat import make_server

log = logging.getLogger(__name__)

server = make_server("yoyo-mail")


@server.tool(
    description=(
        "List configured mail accounts and whether each is authenticated. Call this first "
        "if you are unsure which account to use, or if another mail tool reports an auth "
        "problem."
    )
)
def mail_accounts() -> dict:
    return {"accounts": mail.status()}


@server.tool(
    description=(
        "Search mail. The query uses the provider's own syntax — Gmail: "
        "'from:alice after:2026/08/01 invoice'; Microsoft: free text. Returns message "
        "summaries with ids; call mail_read for full bodies. Omit `account` when only one "
        "is configured."
    )
)
def mail_search(query: str, account: str = "", limit: int = 20) -> dict:
    provider = mail.resolve(account or None)
    messages = provider.search(query, limit=limit)
    return {
        "account": provider.account,
        "count": len(messages),
        "messages": [m.as_dict() for m in messages],
    }


@server.tool(
    description="Read one message in full, including its body, by the id from mail_search."
)
def mail_read(message_id: str, account: str = "") -> dict:
    provider = mail.resolve(account or None)
    return provider.read(message_id).as_dict(include_body=True)


@server.tool(
    description=(
        "Read a whole conversation by thread id, oldest first. Use this rather than reading "
        "messages one at a time when you need the context of an exchange."
    )
)
def mail_thread(thread_id: str, account: str = "", limit: int = 50) -> dict:
    provider = mail.resolve(account or None)
    messages = provider.thread(thread_id, limit=limit)
    return {
        "account": provider.account,
        "count": len(messages),
        "messages": [m.as_dict(include_body=True) for m in messages],
    }


@server.tool(
    description=(
        "Compose a DRAFT. It is saved unsent in the mailbox for the human to review, edit "
        "and send. This tool cannot send mail and there is no tool that can. Pass "
        "reply_to_message_id to draft a reply in an existing thread."
    )
)
def mail_draft(
    to: list[str],
    subject: str,
    body: str,
    account: str = "",
    cc: list[str] | None = None,
    reply_to_message_id: str = "",
) -> dict:
    if not to:
        raise ValueError("at least one recipient is required")
    provider = mail.resolve(account or None)
    draft = provider.create_draft(
        to=to,
        subject=subject,
        body=body,
        cc=cc or [],
        reply_to_message_id=reply_to_message_id or None,
    )
    return draft.as_dict()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    accounts = mail.load_accounts()
    enabled = [a.name for a in accounts if a.enabled]
    if not enabled:
        print(
            "yoyo-mail: no enabled accounts in yoyo-mail.yaml — nothing to serve",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    log.info("yoyo-mail serving accounts: %s", ", ".join(enabled))
    server.run("stdio")


if __name__ == "__main__":
    main()

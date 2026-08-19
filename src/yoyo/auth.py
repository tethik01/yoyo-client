"""The UI token, and why loopback is not a security boundary.

Until now every API route was read-only or conversational, and binding to 127.0.0.1 was
enough. Adding routes that ingest files, write memory pages and delete things changes the
threat model completely, and not in the direction people expect.

**The attacker is not a user. It is a browser tab.** Any web page you visit can run

    fetch("http://127.0.0.1:8081/jobs", {method: "POST", body: ...})

against this server. It does not need to be on your network, it does not need credentials,
and "only I use this UI" is not a defence — you are the one who opened the page. Loopback
stops other *machines*, not other *origins on your machine*. This is a well-worn attack on
local dev servers, and the moment a POST route does something irreversible it applies here.

Two mechanisms, both cheap:

1. **A token header on every state-changing request.** The browser's same-origin policy
   will not let a cross-origin page set a custom header without a CORS preflight, and this
   server answers no preflight. So a page that is not ours cannot construct the request at
   all — the check never even runs. The token is injected into the HTML at serve time, so
   the UI never has to fetch it and there is no unauthenticated route handing it out.
2. **An `Origin` check.** Defence in depth for anything that slips past the first — and it
   catches the simple form-POST case, which needs no preflight.

The token lives in `data/ui-token`, mode 0600 where the OS supports it, generated on first
use. It is NOT in `.env`: it is machine-local state, not configuration, and it must never
be committed or backed up. `backup.py` already excludes `.env`; this file sits in `data/`
which the archive also skips.

**What this does not do.** It does not protect against another process running as you on
this machine — that process can read the token file, and nothing short of an OS keychain
would change that. The realistic threat here is the browser, and the browser is closed.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

TOKEN_HEADER = "x-yoyo-token"
TOKEN_BYTES = 32

#: Origins the UI is legitimately served from. Anything else is a page that is not ours.
def allowed_origins(host: str, port: int) -> set[str]:
    hosts = {host, "127.0.0.1", "localhost"}
    return {f"http://{h}:{port}" for h in hosts} | {f"https://{h}:{port}" for h in hosts}


def token_path() -> Path:
    from .config import REPO_ROOT

    return REPO_ROOT / "data" / "ui-token"


def read_or_create_token(path: Path | None = None) -> str:
    """The token for this installation, created on first use.

    Regenerating on every start would be more paranoid and would also log you out of an
    open tab every restart, which trains you to reload rather than to notice. Stable is
    better here; the file is deletable if it ever needs rotating.
    """
    path = path or token_path()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(TOKEN_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows ACLs do not map; not fatal, and the file is under data/
        log.debug("could not chmod %s", path)
    return token


def constant_time_equal(a: str, b: str) -> bool:
    return secrets.compare_digest((a or "").encode(), (b or "").encode())


def origin_is_allowed(origin: str | None, host: str, port: int) -> bool:
    """No Origin header is allowed; a WRONG one is not.

    Non-browser clients (curl, a script, the CLI itself) send no Origin, and those are not
    the threat this addresses — they already have filesystem access to the token. A present
    but foreign Origin is a page trying something.
    """
    if not origin:
        return True
    return origin in allowed_origins(host, port)

"""MCP server exposing the vault's checkbox tasks as structured items.

    python -m yoyo.mcp.tasks_server          # or: yoyo mcp serve-tasks

Read-only. Yoyo does not tick your boxes — see `yoyo.tasks` for why that follows from the
vault's drafts-only write asymmetry rather than being an arbitrary limitation.

Requires YOYO_VAULT_PATH, same as the vault server. Runs as its own process so a third-party
client (Claude Desktop) can mount it independently of Yoyo.
"""

from __future__ import annotations

import logging
import sys
from datetime import date

from .. import tasks as tasks_mod
from .. import vault
from ._compat import make_server

log = logging.getLogger(__name__)

server = make_server("yoyo-tasks")

#: A full vault dump would blow the 32K context ceiling on its own. The tools cap
#: aggressively and SAY they capped — a truncated list that reads as complete is the same
#: lie as a silently truncated plan.
HARD_LIMIT = 200


def _pack(items: list[tasks_mod.Task], limit: int) -> dict:
    shown = items[:limit]
    out = {
        "count": len(shown),
        "tasks": [t.as_dict() for t in shown],
    }
    if len(items) > len(shown):
        out["truncated"] = (
            f"{len(items)} tasks matched; showing the first {len(shown)} by due date. "
            f"Narrow the query with due_before, tag or contains rather than assuming "
            f"this is the whole list."
        )
    return out


@server.tool(
    description=(
        "List tasks from the user's Obsidian vault, parsed from Markdown checkboxes. "
        "status is 'open' (default), 'done' or 'all'. Filter with due_before (YYYY-MM-DD), "
        "tag, contains, or folder. Results are sorted overdue-and-soonest first, with "
        "undated tasks last. Undated tasks are EXCLUDED when due_before is set, because a "
        "task with no deadline is not due before anything."
    )
)
def tasks_list(
    status: str = "open",
    due_before: str = "",
    tag: str = "",
    contains: str = "",
    folder: str = "",
    limit: int = 50,
) -> dict:
    items = tasks_mod.query(
        status=status,
        folder=folder,
        due_before=due_before or None,
        tag=tag or None,
        contains=contains or None,
        limit=HARD_LIMIT,
    )
    return _pack(items, min(max(1, limit), HARD_LIMIT))


@server.tool(
    description=(
        "Open tasks whose due date has already passed. Use this for 'what am I late on'. "
        "Returns nothing for tasks with no due date — those are not overdue, they are "
        "undated, and conflating the two invents urgency that does not exist."
    )
)
def tasks_overdue(limit: int = 50) -> dict:
    today = date.today()
    items = [t for t in tasks_mod.collect() if t.overdue(today)]
    items.sort(key=tasks_mod._sort_key)
    return {"as_of": today.isoformat(), **_pack(items, min(max(1, limit), HARD_LIMIT))}


@server.tool(
    description=(
        "Counts only: total, open, done, overdue, due today, undated. Call this FIRST for "
        "a 'what is on my plate' question — it is one small result instead of hundreds of "
        "task lines, and it tells you whether a fuller query is even worth making."
    )
)
def tasks_summary() -> dict:
    return tasks_mod.summary()


@server.tool(
    description=(
        "Full text search across task text only (not whole notes). Use vault_search "
        "instead when you want to search note bodies."
    )
)
def tasks_search(query: str, status: str = "all", limit: int = 50) -> dict:
    items = tasks_mod.query(status=status, contains=query, limit=HARD_LIMIT)
    return _pack(items, min(max(1, limit), HARD_LIMIT))


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        root = vault.vault_root()
    except vault.VaultError as exc:
        # stderr, captured and relayed by the client — otherwise this dies inside an anyio
        # TaskGroup and the user sees "Connection closed" with no cause.
        print(f"yoyo-tasks: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
    log.info("yoyo-tasks serving %s over stdio", root)
    server.run("stdio")


if __name__ == "__main__":
    main()

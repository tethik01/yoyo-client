"""MCP server exposing the Obsidian vault.

Runs as its own process over stdio. Yoyo mounts it through its MCP client adapter, and
because it speaks plain MCP, Claude Desktop or any other client can mount it too.

    python -m yoyo.mcp.vault_server          # or: yoyo mcp serve-vault

Requires YOYO_VAULT_PATH. Read is unrestricted within the vault; writes go to the drafts
folder only — see yoyo.vault for why that asymmetry is the approval mechanism.
"""

from __future__ import annotations

import logging
import sys

from .. import vault
from ._compat import make_server

log = logging.getLogger(__name__)

server = make_server("yoyo-vault")


@server.tool(
    description=(
        "Search the Obsidian vault for a literal phrase. Returns one hit per note with the "
        "matching line. This is the LIVE vault — use it when the current wording matters, "
        "as opposed to search_corpus which searches embedded snapshots."
    )
)
def vault_search(query: str, limit: int = 20) -> dict:
    hits = vault.search(query, limit=limit)
    return {
        "count": len(hits),
        "hits": [
            {"path": h.path, "title": h.title, "line": h.line, "excerpt": h.excerpt}
            for h in hits
        ],
    }


@server.tool(
    description=(
        "Read one note from the vault by its vault-relative path, e.g. "
        "'Projects/Yoyo.md'. Returns frontmatter, body text and outgoing wikilinks."
    )
)
def vault_read(path: str) -> dict:
    return vault.read_note(path)


@server.tool(
    description="List notes in the vault, optionally under a folder. Paths are vault-relative."
)
def vault_list(folder: str = "", limit: int = 200) -> dict:
    notes = vault.list_notes(folder, limit=limit)
    return {
        "count": len(notes),
        "notes": [{"path": n.path, "title": n.title, "bytes": n.size} for n in notes],
    }


@server.tool(
    description=(
        "Find notes that link to a given note via [[wikilink]]. Useful for understanding "
        "how a topic connects to the rest of the vault."
    )
)
def vault_backlinks(note: str, limit: int = 50) -> dict:
    links = vault.backlinks(note, limit=limit)
    return {"count": len(links), "backlinks": links}


@server.tool(
    description=(
        "Write a Markdown draft into the vault's drafts folder. This CANNOT write anywhere "
        "else in the vault — existing notes are never modified. The human promotes a draft "
        "by moving the file, which is the approval step."
    )
)
def vault_write_draft(name: str, content: str, overwrite: bool = False) -> dict:
    return vault.write_draft(name, content, overwrite=overwrite)


@server.tool(description="Note count, total size and draft count for the vault.")
def vault_stats() -> dict:
    return vault.stats()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        root = vault.vault_root()
    except vault.VaultError as exc:
        # Printed to stderr, which the client captures and relays — otherwise this dies
        # inside an anyio TaskGroup and the user sees nothing useful.
        print(f"yoyo-vault: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
    log.info("yoyo-vault serving %s over stdio", root)
    server.run("stdio")


if __name__ == "__main__":
    main()

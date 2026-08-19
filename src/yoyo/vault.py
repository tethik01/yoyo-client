"""Obsidian vault access — plain functions, no MCP, no models.

The vault is canon (per the handoff): plain Markdown, the source of truth. Yoyo reads it and
may write *drafts*, never anything else. That asymmetry is the HITL mechanism the swarm
design assumes — output lands as a draft and approval is the human moving the file.

Everything here is path-confined to the vault root. That check is the security boundary of
this module and is tested from both directions.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MARKDOWN_SUFFIXES = {".md", ".markdown"}
DRAFTS_DIR = "yoyo-drafts"
#: Yoyo's own wiki. Visible on the map, invisible to search — see `_notes`. Kept in step
#: with `memory.wiki.WIKI_DIR`; a test asserts they agree, because two spellings of one
#: folder name is how an exclusion silently stops excluding.
MEMORY_DIR = "yoyo-memory"
MAX_READ_BYTES = 400_000

_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class VaultError(RuntimeError):
    pass


@dataclass(slots=True)
class Note:
    path: str          # vault-relative, forward slashes
    title: str
    size: int
    modified: float


@dataclass(slots=True)
class Hit:
    path: str
    title: str
    line: int
    excerpt: str


def vault_root() -> Path:
    """Settings first (so `.env` works), environment second (so a server subprocess or a
    third-party client like Claude Desktop can override without touching the repo)."""
    from .config import get_settings

    raw = os.environ.get("YOYO_VAULT_PATH") or getattr(get_settings(), "vault_path", None)
    # An empty or whitespace value must count as unset. Path("") resolves to ".", which
    # would silently make the current working directory the vault.
    if raw is None or not str(raw).strip() or str(raw).strip() == ".":
        raise VaultError(
            "No vault configured. Set YOYO_VAULT_PATH in .env to your Obsidian vault folder."
        )
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise VaultError(f"Vault path {root} is not a directory")
    return root.resolve()


def _resolve(relative: str, root: Path | None = None) -> Path:
    """Resolve a vault-relative path, refusing anything that escapes the vault.

    This is the boundary. `..`, absolute paths, and symlinks pointing outside all fail —
    resolve() first, then verify containment, so a symlink cannot smuggle a path past a
    string check.
    """
    root = root or vault_root()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise VaultError(f"Path {relative!r} escapes the vault")
    return candidate


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _notes(root: Path, include_generated: bool = False) -> list[Path]:
    """Canon notes only — what Yoyo may READ BACK and CITE.

    Two folders are excluded, for the same reason and with different histories.

    `yoyo-drafts/` has been excluded since day one: it is Yoyo's unapproved output, and
    letting it into search would let the assistant cite itself.

    `yoyo-memory/` was NOT, and that was a hole. Extraction already refuses to read from it
    (`build.evidence_from` never treats a wiki page as a raw source), so the laundering path
    was closed at write time — but `vault_search` could still find those pages and hand them
    to the model as "your notes". Same circularity, arriving through retrieval instead of
    extraction, and worse: at write time a wrong quote fails a check, while at read time the
    quote is genuinely there. Nothing mechanical could have caught it.

    So the rule is: **a memory page is an index, never evidence.** Recall resolves a page to
    the conversation it came from and cites THAT.

    `include_generated=True` is for the map. The vault graph SHOULD show memory pages — they
    are the reason the map is interesting — and drawing a node is not citing it. Callers that
    feed a model must never pass it.
    """
    excluded = {DRAFTS_DIR} if include_generated else {DRAFTS_DIR, MEMORY_DIR}
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in MARKDOWN_SUFFIXES
        and ".obsidian" not in p.parts
        and ".trash" not in p.parts
        and not (excluded & set(p.parts))
    ]


# --------------------------------------------------------------------- read ---


def list_notes(folder: str = "", limit: int = 200) -> list[Note]:
    root = vault_root()
    base = _resolve(folder, root) if folder else root
    if not base.is_dir():
        raise VaultError(f"{folder!r} is not a folder in the vault")

    out: list[Note] = []
    for p in sorted(_notes(base)):
        st = p.stat()
        out.append(Note(_rel(p, root), p.stem, st.st_size, st.st_mtime))
        if len(out) >= limit:
            break
    return out


def read_note(path: str) -> dict[str, object]:
    root = vault_root()
    p = _resolve(path, root)
    if not p.is_file():
        raise VaultError(f"No note at {path!r}")
    if p.stat().st_size > MAX_READ_BYTES:
        raise VaultError(f"{path!r} is larger than {MAX_READ_BYTES} bytes — read it in parts")

    text = p.read_text(encoding="utf-8", errors="replace")
    fm = _FRONTMATTER.match(text)
    return {
        "path": _rel(p, root),
        "title": p.stem,
        "frontmatter": fm.group(1) if fm else None,
        "text": text[fm.end():] if fm else text,
        "links": sorted({m.strip() for m in _WIKILINK.findall(text)}),
    }


def search(query: str, limit: int = 20, context: int = 120) -> list[Hit]:
    """Literal, case-insensitive substring search across the vault.

    Deliberately not semantic: this is the *live* vault, and `search_corpus` already covers
    embedded material. The two answer different questions — "what does the corpus know"
    versus "what does this note say right now".
    """
    if not query.strip():
        return []
    root = vault_root()
    needle = query.lower()
    hits: list[Hit] = []

    for p in sorted(_notes(root)):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:  # noqa: PERF203 - one unreadable file must not stop the search
            log.warning("could not read %s: %s", p, exc)
            continue
        for i, line in enumerate(lines, start=1):
            if needle in line.lower():
                col = line.lower().index(needle)
                start = max(0, col - context // 2)
                hits.append(
                    Hit(_rel(p, root), p.stem, i, line[start : start + context].strip())
                )
                break  # one hit per note keeps results broad rather than deep
        if len(hits) >= limit:
            break
    return hits


def backlinks(note: str, limit: int = 50) -> list[str]:
    """Notes containing a wikilink to this one."""
    root = vault_root()
    target = Path(note).stem.lower()
    out: list[str] = []
    for p in sorted(_notes(root)):
        if p.stem.lower() == target:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if any(link.strip().lower() == target for link in _WIKILINK.findall(text)):
            out.append(_rel(p, root))
        if len(out) >= limit:
            break
    return out


# -------------------------------------------------------------------- write ---


def write_draft(name: str, content: str, overwrite: bool = False) -> dict[str, object]:
    """Write into the drafts folder only. Never anywhere else in the vault.

    The vault is canon and Yoyo does not get to edit canon. Promotion from draft to note is
    a human moving the file — that *is* the approval step, and inventing a second approval
    mechanism would only add a way to bypass this one.
    """
    root = vault_root()
    safe = Path(name).name  # discard any directory component the caller supplied
    if not safe or safe.startswith("."):
        raise VaultError(f"Invalid draft name {name!r}")
    if Path(safe).suffix.lower() not in MARKDOWN_SUFFIXES:
        safe += ".md"

    drafts = root / DRAFTS_DIR
    drafts.mkdir(parents=True, exist_ok=True)
    target = _resolve(f"{DRAFTS_DIR}/{safe}", root)

    if target.exists() and not overwrite:
        raise VaultError(f"Draft {safe!r} already exists — pass overwrite to replace it")

    target.write_text(content, encoding="utf-8")
    log.info("wrote draft %s (%d bytes)", target, len(content))
    return {"path": _rel(target, root), "bytes": len(content), "overwritten": target.exists()}


def stats() -> dict[str, object]:
    root = vault_root()
    notes = _notes(root)
    return {
        "root": str(root),
        "notes": len(notes),
        "bytes": sum(p.stat().st_size for p in notes),
        "drafts": (
            len(list((root / DRAFTS_DIR).glob("*.md")))
            if (root / DRAFTS_DIR).is_dir()
            else 0
        ),
        # Counted separately from `notes` on purpose: Yoyo's pages are not part of what you
        # wrote, and folding them into one number would make the vault look better read
        # than it is.
        "memory_pages": (
            len(list((root / MEMORY_DIR).rglob("*.md")))
            if (root / MEMORY_DIR).is_dir()
            else 0
        ),
    }


def graph(folder: str = "", limit: int = 500) -> dict[str, object]:
    """Notes and their wikilinks, plus which notes the corpus has ingested.

    The corpus overlay is the point, not decoration. A live agent turn on 2026-08-15
    answered "your notes describe the GB10…" and cited a CORPUS document — the vault held
    one empty file. Vault and corpus are different stores that drift, and nothing in the
    system made that visible. This does.

    Broken links are kept as nodes with `exists: false`. A wikilink to a note you have not
    written yet is real information — Obsidian shows those too, and dropping them would
    hide the shape of what you meant to write.
    """
    root = vault_root()
    base = _resolve(folder, root) if folder else root
    if not base.is_dir():
        raise VaultError(f"{folder!r} is not a folder in the vault")

    ingested = _ingested_stems()
    nodes: dict[str, dict[str, object]] = {}
    edges: list[dict[str, str]] = []

    paths = sorted(_notes(base, include_generated=True))[:limit]
    for path in paths:
        rel = _rel(path, root)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        nodes[path.stem.lower()] = {
            "id": path.stem.lower(),
            "title": path.stem,
            "path": rel,
            "bytes": path.stat().st_size,
            "exists": True,
            # Yoyo wrote this one. The map shows it; search will not, and the UI colours it
            # differently so "my note" and "Yoyo's page about me" never look alike.
            "generated": MEMORY_DIR in path.parts,
            # Matched on stem: the corpus stores a source path that may differ from the
            # vault-relative one, and an exact-path join would report everything as absent.
            "in_corpus": path.stem.lower() in ingested,
            "empty": path.stat().st_size == 0,
        }

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source = path.stem.lower()
        for raw in {m.strip() for m in _WIKILINK.findall(text)}:
            target = raw.lower()
            if not target:
                continue
            if target not in nodes:
                # A link to a note that does not exist yet. Obsidian shows these; so do we.
                nodes[target] = {
                    "id": target, "title": raw, "path": None, "bytes": 0,
                    "exists": False, "in_corpus": False, "empty": True,
                }
            edges.append({"source": source, "target": target})

    return {
        "root": str(root),
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {
            # Yoyo's pages are drawn on the map but are NOT "your notes". Counting them
            # together would make the vault look better read than it is — the same
            # your-notes-versus-Yoyo's-writing confusion the search exclusion exists for.
            "notes": sum(1 for n in nodes.values()
                         if n["exists"] and not n.get("generated")),
            "missing": sum(1 for n in nodes.values() if not n["exists"]),
            "empty": sum(1 for n in nodes.values() if n["exists"] and n["empty"]),
            "in_corpus": sum(1 for n in nodes.values() if n["in_corpus"]),
            "generated": sum(1 for n in nodes.values() if n.get("generated")),
            "links": len(edges),
        },
    }


def _ingested_stems() -> set[str]:
    """Note stems present in the corpus. Empty set if the database is unavailable —
    the graph is still worth drawing without the overlay."""
    try:
        from .storage import db

        with db.connection() as conn:
            rows = conn.execute("SELECT source_path, title FROM documents").fetchall()
    except Exception:  # noqa: BLE001
        log.debug("corpus overlay unavailable", exc_info=True)
        return set()

    from pathlib import PurePath

    out = set()
    for row in rows:
        for value in (row["source_path"], row["title"]):
            if value:
                out.add(PurePath(str(value)).stem.lower())
    return out

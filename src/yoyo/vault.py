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


def _notes(root: Path) -> list[Path]:
    """Canon notes only.

    Drafts are excluded deliberately: they are Yoyo's unapproved output, not part of the
    vault's knowledge. Letting them into search would let the assistant cite itself.
    """
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in MARKDOWN_SUFFIXES
        and ".obsidian" not in p.parts
        and ".trash" not in p.parts
        and DRAFTS_DIR not in p.parts
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
        "drafts": len(list((root / DRAFTS_DIR).glob("*.md"))) if (root / DRAFTS_DIR).is_dir() else 0,
    }

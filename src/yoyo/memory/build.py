"""Building the wiki: extract, verify, resolve identity, write pages, log.

Phases 2–4 meet here.

- **Phase 2** — extract claims, verify every quote, write pages under `yoyo-memory/`.
- **Phase 3** — identity. "Mom", "my mother" and "Priya" may be one person or three, and
  guessing wrong is worse than not knowing: merging two people makes a page wrong about
  both, splitting one scatters their memory so retrieval finds a third of it. Aliases are
  recorded; genuine ambiguity is **asked**, never resolved silently.
- **Phase 4** — time. Contradictions are recorded as edges, not resolved: both claims are
  kept with their sources and the tension is shown on the page. Deciding which grounded
  claim wins is a judgement, and this layer does not make judgements. Forgetting really
  removes a claim, leaving a tombstone in the log that records THAT something went and
  when — never what.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import extract, schema, wiki
from .wiki import Claim, Page

log = logging.getLogger(__name__)

#: Yoyo's block inside a note the OWNER already wrote. Their prose stays theirs; Yoyo only
#: ever replaces what is between these markers.
BLOCK_START = "<!-- yoyo:memory:start -->"
BLOCK_END = "<!-- yoyo:memory:end -->"

#: The leading two words of a claim, used to spot claims that may be about the same
#: predicate ("Priya lives in Toronto" / "Priya lives in Lisbon"). Crude on purpose, and
#: crucially it only ever RAISES A FLAG — it never picks a winner.
_LEAD = re.compile(r"^[\W_]*(\w+(?:\W+\w+){0,1})")


@dataclass
class BuildReport:
    sources: int = 0
    proposed: int = 0
    accepted: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    pages_written: int = 0
    flagged: int = 0
    ambiguities: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"sources={self.sources} proposed={self.proposed} accepted={self.accepted} "
            f"rejected={len(self.rejected)} pages={self.pages_written} "
            f"contradictions_flagged={self.flagged} ambiguous={len(self.ambiguities)}"
        )


# --------------------------------------------------------------------- identity ---


def claim_key(claim: Claim) -> str:
    """A stable key for "the same fact, restated"."""
    match = _LEAD.match(claim.claim.lower())
    lead = match.group(1) if match else claim.claim.lower()[:24]
    return f"{claim.kind}:{claim.subject.lower()}:{lead}"


def find_ambiguities(claims: list[Claim], known: dict[str, str]) -> list[dict[str, Any]]:
    """Subjects that might be an existing entity under another name.

    Returns questions, not answers. A relational word ("mom", "my sister") next to a proper
    name in the same source is the classic case, and it is exactly the one worth asking about
    rather than resolving: the cost of asking is one question, the cost of guessing wrong is
    a page that is wrong about two people at once.
    """
    relational = {"mom", "mum", "mother", "dad", "father", "sister", "brother",
                  "wife", "husband", "partner", "son", "daughter", "boss", "manager"}
    out: list[dict[str, Any]] = []
    by_source: dict[str, set[str]] = {}
    for claim in claims:
        by_source.setdefault(claim.source, set()).add(claim.subject)

    for source, subjects in by_source.items():
        rel = {s for s in subjects if s.strip().lower() in relational}
        named = {s for s in subjects if s.strip().lower() not in relational}
        for r in rel:
            for n in named:
                if known.get(r.lower()) == n.lower():
                    continue     # already answered once
                out.append({
                    "question": f"Is “{r}” the same person as “{n}”?",
                    "alias": r, "candidate": n, "source": source,
                })
    return out


# ------------------------------------------------------------------ page writing ---


def merge_into_existing(existing: str, block: str) -> str:
    """Put Yoyo's block into a note the owner already wrote, replacing only its own block.

    The owner's prose is never touched. If the markers are absent the block is appended; if
    present, only what is between them is replaced. That is what makes `git diff` on the
    vault readable — Yoyo's changes are always confined to a region you can see.
    """
    payload = f"{BLOCK_START}\n{block.strip()}\n{BLOCK_END}"
    if BLOCK_START in existing and BLOCK_END in existing:
        head, _, rest = existing.partition(BLOCK_START)
        _, _, tail = rest.partition(BLOCK_END)
        return f"{head.rstrip()}\n\n{payload}\n{tail.lstrip()}".rstrip() + "\n"
    return f"{existing.rstrip()}\n\n{payload}\n"


def owner_note_for(root: Path, page: Page) -> Path | None:
    """A note the OWNER already wrote about this subject, if there is one.

    When they have one, Yoyo enriches it in a marked block rather than creating a rival page
    — which was the owner's explicit choice, and avoids two nodes per person in the map.
    """
    target = wiki.safe_name(page.subject).lower()
    for candidate in root.rglob("*.md"):
        if wiki.WIKI_DIR in candidate.parts or "yoyo-drafts" in candidate.parts:
            continue
        if candidate.stem.lower() == target:
            return candidate
    return None


def block_for(page: Page) -> str:
    lines = [f"_Yoyo's memory of {page.subject}. Each line quotes a raw source._", ""]
    for claim in page.claims:
        lines.append(f"- {claim.claim}  <sub>`{claim.source}`</sub>")
    return "\n".join(lines)


# ------------------------------------------------------------------------ build ---


def build(
    sources: dict[str, str],
    root: Path | None = None,
    aliases: dict[str, str] | None = None,
    role: str = "extract",
) -> BuildReport:
    """Extract from raw sources and write the wiki. `sources` maps id -> full text."""
    from .. import vault as vault_mod

    root = root or vault_mod.vault_root()
    report = BuildReport(sources=len(sources))

    proposed: list[Claim] = []
    for source_id, text in sources.items():
        proposed.extend(extract.from_source(source_id, text, role=role))
    report.proposed = len(proposed)

    verified = wiki.verify(proposed, sources)
    report.accepted = len(verified.accepted)
    report.rejected = [(c.subject or "?", why) for c, why in verified.rejected]
    for claim, why in verified.rejected:
        log.info("rejected claim about %r: %s", claim.subject, why)

    report.ambiguities = find_ambiguities(verified.accepted, aliases or {})

    existing = load_pages(root)
    pages = wiki.group(verified.accepted)
    for page in pages:
        prior = existing.get((page.kind, page.subject.lower()))
        if prior:
            page.claims, flagged = reconcile(prior, page.claims)
            report.flagged += flagged
        write_page(root, page)
        report.pages_written += 1

    write_index(root)
    schema.write(root)
    wiki.append_log(root, "build", {
        "sources": list(sources), "accepted": report.accepted,
        "rejected": len(report.rejected), "pages": report.pages_written,
        "contradictions_flagged": report.flagged,
    })
    return report


def reconcile(prior: list[Claim], incoming: list[Claim]) -> tuple[list[Claim], int]:
    """Merge new claims with old. Both survive; nothing is overwritten or deleted.

    An identical restatement is dropped — saying a fact ten times should not produce ten
    lines. Everything else is appended, and possible contradictions are FLAGGED rather than
    resolved. That is the pattern's own rule: "contradictions between grounded claims are
    recorded as edges rather than resolved, preserving ambiguity where sources disagree."

    An earlier version marked the older claim superseded. That was wrong twice over: it
    required a string heuristic to decide which of two claims was "the same fact", and a
    heuristic crude enough to catch "lives in Toronto" / "lives in Lisbon" also catches
    "is Bhavin's sister" / "is moving in March" — silently striking through a true fact.
    Deciding which grounded claim wins is a judgement, and this layer does not make
    judgements; it presents evidence and lets the reader see the tension.

    Returns (claims, flagged) where `flagged` counts possible contradictions surfaced.
    """
    seen = {wiki.normalise(c.claim) for c in prior}
    out = list(prior)
    for claim in incoming:
        if wiki.normalise(claim.claim) in seen:
            continue                      # same fact, said again
        out.append(claim)
        seen.add(wiki.normalise(claim.claim))
    return out, len(find_contradictions(out))


def find_contradictions(claims: list[Claim]) -> list[tuple[Claim, Claim]]:
    """Pairs of claims that may disagree. Surfaced, never resolved.

    Same subject, same leading predicate, different text. False positives are acceptable
    because the output is a question on the page ("these two may disagree"), not a deletion.
    A false positive costs the reader a glance; a wrong resolution costs them a fact.
    """
    pairs: list[tuple[Claim, Claim]] = []
    by_key: dict[str, Claim] = {}
    for claim in claims:
        if claim.claim.startswith("~~"):
            continue
        key = claim_key(claim)
        first = by_key.get(key)
        if first is None:
            by_key[key] = claim
        elif wiki.normalise(first.claim) != wiki.normalise(claim.claim):
            pairs.append((first, claim))
    return pairs


# ------------------------------------------------------------------ persistence ---

_CLAIM_LINE = re.compile(r"^- (?P<claim>.+)$")
_SOURCE_LINE = re.compile(r"^\s+- source: `(?P<source>[^`]+)`$")
_QUOTE_LINE = re.compile(r'^\s+- quote: "(?P<quote>.*)"$')


def load_pages(root: Path) -> dict[tuple[str, str], list[Claim]]:
    """Read back what Yoyo has already written, so a build updates rather than replaces."""
    out: dict[tuple[str, str], list[Claim]] = {}
    base = root / wiki.WIKI_DIR
    if not base.is_dir():
        return out
    for path in base.rglob("*.md"):
        if path.name in {wiki.INDEX_FILE, wiki.LOG_FILE, schema.SCHEMA_FILE}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # A page is a page if it declares what it is ABOUT. Filename exclusions alone were
        # not enough — the generated SCHEMA.md was being read back as an entity called
        # "schema" — and a name-based rule would break again the next time a non-page file
        # lands in the folder.
        subject = _frontmatter(text, "about")
        if not subject:
            continue
        kind = _frontmatter(text, "kind") or "topic"
        claims: list[Claim] = []
        pending: dict[str, str] = {}
        for line in text.splitlines():
            if (m := _SOURCE_LINE.match(line)):
                pending["source"] = m.group("source")
            elif (m := _QUOTE_LINE.match(line)):
                pending["quote"] = m.group("quote")
                if "claim" in pending and "source" in pending:
                    claims.append(Claim(subject=subject, kind=kind, claim=pending["claim"],
                                        quote=pending["quote"], source=pending["source"]))
                pending = {}
            elif (m := _CLAIM_LINE.match(line)) and not line.startswith("- ["):
                pending = {"claim": m.group("claim")}
        out[(kind, subject.lower())] = claims
    return out


def _frontmatter(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}:\s*(.+)$", text, re.M)
    return match.group(1).strip() if match else None


def write_page(root: Path, page: Page) -> Path:
    owner = owner_note_for(root, page)
    if owner is not None:
        # The owner's choice: enrich their note in a marked block rather than making a
        # rival page. Their prose is untouched.
        existing = owner.read_text(encoding="utf-8", errors="replace")
        owner.write_text(merge_into_existing(existing, block_for(page)), encoding="utf-8")
        return owner

    path = root / page.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page.render(), encoding="utf-8")
    return path


def write_index(root: Path) -> Path:
    pages = [
        {"subject": subject, "kind": kind, "claims": len(claims)}
        for (kind, subject), claims in load_pages(root).items()
    ]
    path = root / wiki.WIKI_DIR / wiki.INDEX_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(wiki.render_index(pages), encoding="utf-8")
    return path


def forget(root: Path, subject: str, contains: str | None = None) -> int:
    """Remove claims, leaving a tombstone in the log.

    Forgetting must actually forget — that is the difference between a tool and a
    surveillance record of your own life. But the *log* keeps a note that something was
    forgotten and when, because a memory system that can silently un-remember is one you
    cannot audit. The claim text is not repeated in the tombstone.
    """
    removed = 0
    base = root / wiki.WIKI_DIR
    if not base.is_dir():
        return 0
    for path in base.rglob("*.md"):
        if path.name in {wiki.INDEX_FILE, wiki.LOG_FILE}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if (_frontmatter(text, "about") or path.stem).lower() != subject.lower():
            continue
        if contains is None:
            path.unlink()
            removed += 1
            continue
        kept = [c for c in load_pages(root).get(
            (_frontmatter(text, "kind") or "topic", subject.lower()), [])
            if contains.lower() not in c.claim.lower()]
        page = Page(subject=subject, kind=_frontmatter(text, "kind") or "topic", claims=kept)
        path.write_text(page.render(), encoding="utf-8")
        removed += 1
    write_index(root)
    schema.write(root)
    wiki.append_log(root, "forget", {"subject": subject, "matching": contains,
                                     "pages_touched": removed})
    return removed

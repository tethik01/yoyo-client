"""The wiki layer — LLM-written entity and concept pages.

Phase 2 of the second brain, following Karpathy's pattern:

    raw sources (immutable)  ->  THE WIKI (LLM-written)  ->  the schema (governance)

The owner chose automatic writing over a review queue. That is only defensible because of
one rule this module enforces mechanically:

    **A claim traces to a RAW SOURCE. Never to another wiki page.**

Without it, automatic memory poisons its own well: a fact extracted wrongly on Monday is a
retrievable source on Tuesday, cited on Wednesday, and by Friday indistinguishable from
something the owner said — because by then the provenance is real, Yoyo *did* read it in its
own notes. With it, a fabrication cannot compound. Every claim, however many times it is
restated, still points at a conversation turn or a note the human wrote.

Two mechanical gates, neither of which involves judgement:

1. **Verbatim quote.** A claim must carry a quote that appears, character for character, in
   the raw source it names. No quote, or a quote the source does not contain, and the claim
   is dropped before it reaches a page. This is the same trick as the golden eval's
   unguessable secret: the model cannot fake having read something.
2. **Source kind.** `derived_from` may only name raw sources. A page citing
   `yoyo-memory/People/Mom.md` is rejected outright — that is the laundering path.

Pages are markdown with YAML frontmatter, in the owner's vault under `yoyo-memory/`, so they
are ordinary notes: greppable, git-diffable, and visible in the vault map. `index.md` is the
catalogue; `log.md` is the append-only record of what happened and when.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Where Yoyo's own pages live. The owner's notes are RAW SOURCES and are never written to;
#: this namespace is entirely Yoyo's, which is what makes `git diff` a meaningful review.
WIKI_DIR = "yoyo-memory"
INDEX_FILE = "index.md"
LOG_FILE = "log.md"

#: Prefixes that identify a raw source. Anything else — most importantly a path inside
#: WIKI_DIR — is a page a model wrote, and may never be cited as evidence.
RAW_SOURCE_PREFIXES = ("conversation://", "vault://")

ENTITY_KINDS = ("person", "place", "project", "topic", "event", "organisation")

#: Quotes are normalised before comparison: models reflow whitespace and swap quote glyphs
#: when copying. Requiring byte-identity would reject honest quotes and train us to loosen
#: the gate, which is worse than normalising deliberately.
_WS = re.compile(r"\s+")
_SMART = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-"})


def normalise(text: str) -> str:
    return _WS.sub(" ", (text or "").translate(_SMART)).strip().lower()


class WikiError(RuntimeError):
    pass


# ----------------------------------------------------------------------- claims ---


@dataclass(slots=True)
class Claim:
    """One fact, and the evidence for it."""

    subject: str          # the entity this is about, e.g. "Priya"
    kind: str             # person | place | project | topic | event | organisation
    claim: str            # the fact, in Yoyo's words
    quote: str            # VERBATIM from the raw source. The whole gate rests on this.
    source: str           # conversation://14 or vault://Notes/Trip.md
    confidence: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject, "kind": self.kind, "claim": self.claim,
            "quote": self.quote, "source": self.source, "confidence": self.confidence,
        }


def is_raw_source(source: str) -> bool:
    return str(source or "").startswith(RAW_SOURCE_PREFIXES)


@dataclass
class Verification:
    accepted: list[Claim] = field(default_factory=list)
    rejected: list[tuple[Claim, str]] = field(default_factory=list)

    def summary(self) -> str:
        return f"accepted={len(self.accepted)} rejected={len(self.rejected)}"


def verify(claims: list[Claim], sources: dict[str, str]) -> Verification:
    """Keep only claims whose quote really appears in the raw source they name.

    `sources` maps source id -> its full text. A claim naming a source that is not in the
    map is rejected: not proven false, but unprovable, and unprovable is the same as
    fabricated for a system that must not launder its own output.
    """
    out = Verification()
    for claim in claims:
        if not claim.subject.strip() or not claim.claim.strip():
            out.rejected.append((claim, "empty subject or claim"))
            continue
        if claim.kind not in ENTITY_KINDS:
            out.rejected.append((claim, f"unknown kind {claim.kind!r}"))
            continue
        if not is_raw_source(claim.source):
            # The laundering path, closed. A claim citing a wiki page is a model quoting
            # a model.
            out.rejected.append(
                (claim, f"{claim.source!r} is not a raw source — claims may never cite a "
                        f"wiki page")
            )
            continue
        if not claim.quote.strip():
            out.rejected.append((claim, "no quote"))
            continue
        text = sources.get(claim.source)
        if text is None:
            out.rejected.append((claim, f"source {claim.source!r} was not supplied"))
            continue
        if normalise(claim.quote) not in normalise(text):
            out.rejected.append((claim, "quote does not appear in the source"))
            continue
        out.accepted.append(claim)
    return out


# ------------------------------------------------------------------------ pages ---


@dataclass
class Page:
    """One wiki page: an entity, and the claims made about it."""

    subject: str
    kind: str
    claims: list[Claim] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return f"{safe_name(self.subject)}.md"

    @property
    def path(self) -> str:
        return f"{WIKI_DIR}/{self.kind}s/{self.filename}"

    @property
    def contradictions(self) -> list[tuple[Claim, Claim]]:
        from .build import find_contradictions

        return find_contradictions(self.claims)

    @property
    def derived_from(self) -> list[str]:
        return sorted({c.source for c in self.claims})

    def render(self, now: str | None = None) -> str:
        stamped = now or datetime.now(UTC).isoformat(timespec="seconds")
        front = [
            "---",
            f"about: {self.subject}",
            f"kind: {self.kind}",
            "generated_by: yoyo",
            f"updated: {stamped}",
            "derived_from:",
            *[f"  - {source}" for source in self.derived_from],
            "---",
            "",
            f"# {self.subject}",
            "",
            "> Written by Yoyo. Every claim below quotes a raw source — a conversation or a",
            "> note you wrote. Nothing here cites another Yoyo page.",
            "",
        ]
        body: list[str] = []
        for claim in self.claims:
            body.append(f"- {claim.claim}")
            body.append(f"  - source: `{claim.source}`")
            body.append(f'  - quote: "{claim.quote.strip()}"')
        pairs = self.contradictions
        if pairs:
            body.extend(["", "## Possible contradictions", "",
                         "_These claims may disagree. Both are kept with their sources —",
                         "Yoyo does not decide which is right._", ""])
            for first, second in pairs:
                body.append(f"- “{first.claim}” vs “{second.claim}”")
        if self.links:
            body.extend(["", "## Related", ""])
            body.extend(f"- [[{link}]]" for link in sorted(set(self.links)))
        return "\n".join(front + body).rstrip() + "\n"


def safe_name(subject: str) -> str:
    """A filename that survives Windows and does not collide by accident."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", subject).strip().strip(".")
    cleaned = _WS.sub(" ", cleaned)
    return (cleaned or "unnamed")[:80]


def group(claims: list[Claim]) -> list[Page]:
    """Claims into pages, one per (kind, subject).

    Links are inferred from co-occurrence within a single claim's source: if two entities
    were discussed in the same conversation they are related, which is weak but honest —
    and unlike an inferred *relationship*, a link asserts nothing about what the relation is.
    """
    pages: dict[tuple[str, str], Page] = {}
    for claim in claims:
        key = (claim.kind, claim.subject)
        page = pages.setdefault(key, Page(subject=claim.subject, kind=claim.kind))
        page.claims.append(claim)

    by_source: dict[str, set[str]] = {}
    for claim in claims:
        by_source.setdefault(claim.source, set()).add(claim.subject)
    for page in pages.values():
        for claim in page.claims:
            for other in by_source.get(claim.source, set()):
                if other != page.subject:
                    page.links.append(other)
    return list(pages.values())


# ------------------------------------------------------------------ index / log ---


def render_index(pages: list[dict[str, Any]], now: str | None = None) -> str:
    """The catalogue. Content-oriented, grouped by kind, as the pattern prescribes."""
    stamped = now or datetime.now(UTC).isoformat(timespec="seconds")
    lines = [
        "---", "generated_by: yoyo", f"updated: {stamped}", "---", "",
        "# Yoyo's memory", "",
        "Pages Yoyo has written. Every claim on every page quotes a raw source — a",
        "conversation or a note you wrote. **Nothing here cites another page in this folder.**",
        "",
        f"{len(pages)} page(s).", "",
    ]
    for kind in ENTITY_KINDS:
        of_kind = sorted([p for p in pages if p.get("kind") == kind],
                         key=lambda p: str(p.get("subject", "")).lower())
        if not of_kind:
            continue
        lines.extend([f"## {kind.title()}s", ""])
        for page in of_kind:
            claims = page.get("claims", 0)
            lines.append(f"- [[{page['subject']}]] — {claims} claim(s)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def log_line(action: str, detail: dict[str, Any], now: str | None = None) -> str:
    stamped = now or datetime.now(UTC).isoformat(timespec="seconds")
    return f"- `{stamped}` **{action}** — {json.dumps(detail, default=str, sort_keys=True)}\n"


def append_log(root: Path, action: str, detail: dict[str, Any], now: str | None = None) -> None:
    """Append-only. Never rewritten, so the record of what memory did cannot be edited by
    the thing that did it."""
    path = root / WIKI_DIR / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# Memory log\n\nAppend-only record of what Yoyo remembered, and when.\n\n",
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(log_line(action, detail, now))

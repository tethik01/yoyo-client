"""Phase 1 of the second brain: conversations become searchable raw sources.

Follows Karpathy's three-layer wiki pattern (the gist the owner pointed at):

    raw sources (immutable)  ->  the wiki (LLM-written)  ->  the schema (governance)

This module is the **raw sources** layer, and it is deliberately the dumbest of the three.
Nothing here summarises, extracts or interprets. It takes conversation turns that already
happened and makes them retrievable *verbatim*, as documents in the existing corpus.

That distinction carries the whole safety argument for what comes later. Phase 2 will let a
model write wiki pages automatically, which is only defensible because every claim on a page
must trace back to a raw source — and this is what a raw source is. A quote that cannot be
found here is a quote that was invented.

Why the corpus rather than a new store: chunking, embeddings, hybrid retrieval, citations and
the eval harness all exist and are tested. A second retrieval path would be a second thing to
get subtly wrong, and the whole point of `[12]` is that a citation resolves the same way
whatever produced it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..storage import db

log = logging.getLogger(__name__)

#: Conversation documents live under this synthetic path prefix. Not a real file — the
#: corpus keys documents by `source_path`, and conversations need a stable, collision-proof
#: identity that no filesystem walk will ever produce.
SOURCE_PREFIX = "conversation://"

#: Turns shorter than this are "ok", "thanks", "yes" — retrievable noise that dilutes the
#: corpus without ever being the answer to anything.
MIN_TURN_CHARS = 24

#: Speaker labels in a rendered transcript. Constants because `owner_turns_only()` parses
#: them back out, and a display string that two places spell differently is a bug waiting
#: for a rename.
OWNER = "Bhavin"
ASSISTANT = "Yoyo"


@dataclass
class RememberReport:
    conversations: int = 0
    written: int = 0
    unchanged: int = 0
    turns: int = 0
    skipped_short: int = 0

    def summary(self) -> str:
        return (
            f"conversations={self.conversations} written={self.written} "
            f"unchanged={self.unchanged} turns={self.turns} "
            f"skipped_short={self.skipped_short}"
        )


def source_path(conversation_id: int) -> str:
    return f"{SOURCE_PREFIX}{conversation_id}"


def conversation_id_from(source: str) -> int | None:
    if not source.startswith(SOURCE_PREFIX):
        return None
    try:
        return int(source.removeprefix(SOURCE_PREFIX))
    except ValueError:
        return None


def render(conversation_id: int, title: str | None, messages: list[dict]) -> tuple[str, int, int]:
    """One conversation as a markdown document. Returns (text, turns, skipped).

    Speaker-labelled and timestamped, because a retrieved fragment has to answer "who said
    this, and when" on its own. A chunk that reads `I decided to use coder` is useless if you
    cannot tell whether the user said it or the model did — and that ambiguity is exactly how
    a model's guess gets quoted back as the owner's decision.
    """
    lines = [
        f"# Conversation {conversation_id}" + (f" — {title}" if title else ""),
        "",
        f"- source: `{source_path(conversation_id)}`",
        "- kind: conversation transcript (verbatim)",
        "",
    ]
    turns = skipped = 0
    for message in messages:
        role = str(message.get("role") or "")
        content = (message.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if len(content) < MIN_TURN_CHARS:
            skipped += 1
            continue
        when = str(message.get("created_at") or "")
        speaker = OWNER if role == "user" else ASSISTANT
        lines.append(f"## {speaker}{f' · {when}' if when else ''}")
        lines.append("")
        lines.append(content)
        lines.append("")
        turns += 1
    return "\n".join(lines).strip() + "\n", turns, skipped


_HEADING = re.compile(r"^## (.+?)(?: · .*)?$")


def owner_turns_only(text: str) -> str:
    """The owner's half of a transcript. Everything Yoyo said is dropped.

    **This is the fix for the first real memory finding (2026-08-15).** A dry run over two
    real conversations produced six pages — World War I, World War II, the West Asia
    conflict, Gaza, the Abraham Accords, the Israeli-Palestinian conflict — with every
    quote verifying and nothing rejected. A flawless run by every number the build reports,
    and worthless as a second brain.

    The cause: a conversation transcript is half model output, and extraction was reading
    all of it. The claims quoted **Yoyo explaining geopolitics to Bhavin**, then filed that
    explanation as something to remember. The wiki gates were never breached — they were
    walked around. "A claim traces to a raw source" was enforced; nobody had said that half
    of every raw source was generated by the same model doing the extracting.

    That is the laundering path with one extra hop, and it is worse than the original
    because it looks legitimate: `conversation://1` really is a raw source, and the quote
    really is in it.

    So the rule tightens: **a memory must quote something the owner said.** Yoyo's turns
    stay in the corpus — they are useful to retrieve and read — but they are not evidence
    about the owner's life. A fact worth remembering that only Yoyo ever stated is a fact
    the owner never confirmed.
    """
    kept: list[str] = []
    mine = False
    for line in (text or "").splitlines():
        heading = _HEADING.match(line)
        if heading:
            mine = heading.group(1).strip() == OWNER
            continue
        if mine:
            kept.append(line)
    return "\n".join(kept).strip() + ("\n" if kept else "")


def remember(
    conversation_ids: list[int] | None = None,
    min_turns: int = 1,
) -> RememberReport:
    """Ingest conversations into the corpus. Idempotent — content-hashed like any document.

    Re-running after a conversation continues rewrites that document, which is correct: the
    conversation IS the source, and its later turns are part of it. The content hash means an
    unchanged conversation costs nothing.
    """
    from ..core import conversation_messages, list_conversations
    from ..rag import ingest as ingest_mod

    report = RememberReport()
    wanted = set(conversation_ids or [])

    conversations = list_conversations(limit=1000)
    if wanted:
        conversations = [c for c in conversations if int(c["id"]) in wanted]

    for row in conversations:
        cid = int(row["id"])
        messages = conversation_messages(cid)
        text, turns, skipped = render(cid, row.get("title"), messages)
        report.skipped_short += skipped
        if turns < min_turns:
            continue

        report.conversations += 1
        report.turns += turns
        changed = ingest_mod.ingest_text(
            source_path=source_path(cid),
            title=(row.get("title") or f"Conversation {cid}"),
            text=text,
            mime_type="conversation",
        )
        if changed:
            report.written += 1
        else:
            report.unchanged += 1

    return report


def is_conversation_document(source: str) -> bool:
    return str(source or "").startswith(SOURCE_PREFIX)


def stats() -> dict[str, int]:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS docs, COALESCE(SUM(byte_size), 0) AS bytes "
            "FROM documents WHERE source_path LIKE ?",
            (SOURCE_PREFIX + "%",),
        ).fetchone()
    return {"conversation_documents": int(row["docs"]), "bytes": int(row["bytes"])}

"""Turning raw sources into candidate claims — the only place memory calls a model.

Isolated deliberately. `sources.py` is verbatim and never calls a model; `wiki.py` verifies
and renders and never calls a model. This module is the single point where a model's output
enters memory, which makes it the single point that needs distrusting.

The prompt asks for a quote with every claim, and `wiki.verify` then checks that quote
against the real source text. The model is not trusted to be honest about having read
something; it is *made* to prove it. A model that invents a fact will usually invent a quote
too, and an invented quote fails a substring check.

**Nothing here decides what is true.** It proposes; verification disposes.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .wiki import ENTITY_KINDS, Claim

log = logging.getLogger(__name__)

INSTRUCTION = """Extract durable facts about the USER'S OWN LIFE from the source below.

An entity is a person, place, project, topic, event or organisation **connected to the
user** — someone they know, somewhere they go, something they are working on or planning —
that they would still care about in six months.

**General knowledge is never a memory.** If the fact would be equally true for a stranger,
it does not belong here: the model already knows what the Treaty of Versailles was, and a
personal memory system that files world history as things to remember about its owner has
recorded nothing about its owner. The test is not "is this true" or "is this interesting",
it is "does this tell me something about THIS PERSON".

Rules — these are correctness requirements, not preferences:

1. EVERY claim MUST include a `quote`: text copied EXACTLY from the source, word for word.
   Do not paraphrase the quote, do not tidy it, do not join two sentences. The quote is
   checked against the source character by character and a claim whose quote does not
   appear is DISCARDED.
2. Extract only what the source states. Do not infer, combine or assume. If the source says
   "Priya called about the trip", you may claim Priya called; you may NOT claim Priya is a
   relative, or that a trip is planned.
3. Prefer FEWER, DURABLE facts. "Bhavin's sister is Priya" is durable. "Bhavin asked a
   question about Suno" is not — it is a fact about a conversation, not about an entity.
   "World War I involved European empires competing for colonies" is not either — it is
   true, it is quotable, and it says nothing about the user. Discard it.
6. The source contains only what the USER said; the assistant's replies have already been
   removed. Anything you cannot support from the user's own words does not exist.
4. Skip anything about Yoyo itself, this software, or the conversation as an event.
5. If the source contains no durable facts about entities, return an EMPTY list. An empty
   answer is a correct answer and is expected often.

`subject` is the entity's name as the source names it — do not resolve nicknames or guess
at full names. `kind` must be one of: {kinds}.

SOURCE ({source_id}):
---
{text}
---"""


class ClaimOut(BaseModel):
    subject: str = Field(description="Entity name, exactly as the source names it")
    kind: str = Field(description="person | place | project | topic | event | organisation")
    claim: str = Field(description="The durable fact, in your own words, one sentence")
    quote: str = Field(description="EXACT text from the source supporting this claim")
    confidence: float = Field(default=0.5, description="0-1, your confidence")


class Extraction(BaseModel):
    claims: list[ClaimOut] = Field(
        default_factory=list,
        description="Durable facts about entities. Empty is valid and common.",
    )


def from_source(source_id: str, text: str, role: str = "extract") -> list[Claim]:
    """Propose claims for one raw source. Never raises on model failure — a bad extraction
    should cost that source's memories, not the whole run."""
    from .. import structured

    if not (text or "").strip():
        return []

    try:
        result = structured.generate(
            Extraction,
            INSTRUCTION.format(
                kinds=", ".join(ENTITY_KINDS), source_id=source_id, text=text[:20_000]
            ),
            role=role,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("extraction failed for %s: %s", source_id, exc)
        return []

    return [
        Claim(
            subject=(c.subject or "").strip(),
            kind=(c.kind or "").strip().lower(),
            claim=(c.claim or "").strip(),
            quote=(c.quote or "").strip(),
            # Set HERE, never taken from the model. A model that could name its own source
            # could name a wiki page as one, which is the laundering path this whole design
            # exists to close.
            source=source_id,
            confidence=float(c.confidence or 0.0),
        )
        for c in result.claims
    ]


def stats(claims: list[Claim]) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    for claim in claims:
        kinds[claim.kind] = kinds.get(claim.kind, 0) + 1
    return {"claims": len(claims), "subjects": len({c.subject for c in claims}), "kinds": kinds}

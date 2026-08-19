"""Is this claim about the owner's life, or is it just true?

The verification gates answer "did the model read this" — verbatim quote, real source, not a
wiki page. They cannot answer "is this worth remembering", and the first real run proved how
far apart those are: six pages on World War I, the Abraham Accords and Gaza, every gate green,
8/8 accepted, and not one fact about the person whose second brain it was.

Owner-turns-only fixed half of it. This module is the other half, and it is deliberately
MECHANICAL — no second model call, no confidence threshold, nothing that can be argued with
after the fact. Four rules, each one a thing a person can check by reading the claim:

1. **A question is not a fact.** "Can you explain the West Asia conflict?" is the owner's
   curiosity, not his life. This is what remained after the assistant's half was stripped, and
   it is most of what a chat transcript actually contains.
2. **A fact about Yoyo is not a memory.** The assistant, the models, the box, this software —
   all things the system already knows and none of them the owner's life.
3. **It has to be connected to YOU.** Either the quote carries a first-person marker (`my
   sister Priya`), or the subject is someone memory already knows. That second clause is what
   stops the filter being brittle: people are introduced once with a possessive and referred
   to bare afterwards, and by then the connection is established.
4. **Evidence has to be long enough to be evidence.** A three-word quote can be made to
   support almost anything.

**Every drop is reported, never silent.** `filter_claims` returns the rejects with their
reason, the sweep logs them, and `yoyo memory review` can show them. A filter that quietly
eats claims is indistinguishable from an extractor that finds nothing — and telling those two
apart is the entire open question about whether this works.

Biased toward silence on purpose. A missed memory is a memory you can state again; a queue
full of noise is a review you stop reading, and that costs every future memory too.
"""

from __future__ import annotations

import logging
import re

from .wiki import Claim, normalise

log = logging.getLogger(__name__)

#: First person, in the shapes people actually use when talking about their own life.
FIRST_PERSON = re.compile(
    r"\b(i|i'm|im|i've|i'll|i'd|me|my|mine|myself|we|we're|we've|our|ours|us)\b",
    re.IGNORECASE,
)

#: Things about the assistant and its plumbing. Yoyo talking about Yoyo is not the owner's
#: life, and this system generates an enormous amount of that.
ABOUT_YOYO = re.compile(
    r"\b(yoyo|myaiserver|litellm|ollama|qdrant|the model|the assistant|this software|"
    r"the corpus|the vault|the endpoint|gb10|tailscale)\b",
    re.IGNORECASE,
)

#: A question. Either the punctuation or the opening word, because transcripts drop the mark.
QUESTION = re.compile(
    r"^\s*(can|could|would|will|do|does|did|is|are|was|were|have|has|had|should|shall|may|"
    r"might|who|what|when|where|why|how|which|tell me|explain|show me|remind me)\b",
    re.IGNORECASE,
)

#: Below this a quote is not evidence, it is a fragment that can be made to support almost
#: anything. Three, not four: "my sister Priya" is exactly the shape a good first mention
#: takes, and a threshold that rejects it would be tuned for the filter rather than for the
#: language people use.
MIN_QUOTE_WORDS = 3


def is_question(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    return stripped.endswith("?") or bool(QUESTION.match(stripped))


def mentions_owner(text: str) -> bool:
    return bool(FIRST_PERSON.search(text or ""))


def about_the_system(text: str) -> bool:
    return bool(ABOUT_YOYO.search(text or ""))


def known_subjects() -> set[str]:
    """Subjects memory has already accepted — approved or written.

    The escape hatch for rule 3. Once you have said "my sister Priya", later claims can say
    "Priya" and still be about your life; requiring a possessive every single time would
    reject most of how people actually talk.

    Deliberately NOT including pending or rejected subjects: a proposal you have not ruled on
    is not yet a fact about your life, and using it to admit more proposals would let one
    unreviewed claim open the gate for others.
    """
    from ..storage import db

    try:
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT subject FROM memory_proposals "
                "WHERE status IN ('approved','written')"
            ).fetchall()
    except Exception:  # noqa: BLE001 - the filter must work before the table exists
        return set()
    return {normalise(r["subject"]) for r in rows}


def reason_to_drop(claim: Claim, known: set[str]) -> str | None:
    """Why this claim is not a memory, or None if it is one."""
    quote = claim.quote or ""

    if len(quote.split()) < MIN_QUOTE_WORDS:
        return f"quote is {len(quote.split())} word(s) — too short to be evidence"
    if is_question(quote):
        return "the quote is a question, not a statement of fact"
    if about_the_system(claim.subject) or about_the_system(claim.claim):
        return "about Yoyo or its plumbing, not about you"
    if not mentions_owner(quote) and normalise(claim.subject) not in known:
        return ("nothing ties it to you — no first-person wording, and this subject is not "
                "already in memory")
    return None


def filter_claims(claims: list[Claim]) -> tuple[list[Claim], list[tuple[Claim, str]]]:
    """(kept, dropped-with-reasons). Never silent — the caller reports both."""
    known = known_subjects()
    kept: list[Claim] = []
    dropped: list[tuple[Claim, str]] = []
    for claim in claims:
        why = reason_to_drop(claim, known)
        if why:
            dropped.append((claim, why))
            log.info("dropped claim about %r: %s", claim.subject, why)
        else:
            kept.append(claim)
    return kept, dropped

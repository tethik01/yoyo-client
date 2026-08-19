"""Is this about the owner's life, or is it merely true?

Every case here is drawn from a real run. The six claims at the top are verbatim from the
first live `memory build --dry-run`: they quoted their source correctly, passed both wiki
gates, and produced pages about World War I and the Abraham Accords in a system whose entire
purpose was to remember one person's life.

That is the distinction this filter draws, and the reason it is mechanical: "is this about
me" cannot be delegated to the thing doing the extracting.
"""

from __future__ import annotations

import pytest

from yoyo.memory import salience
from yoyo.memory.wiki import Claim


def claim(subject="Priya", kind="person", text="is my sister",
          quote="my sister Priya is flying") -> Claim:
    return Claim(subject=subject, kind=kind, claim=text, quote=quote,
                 source="conversation://1")


# --------------------------------------------- the run that motivated the filter ---


@pytest.mark.parametrize(
    ("subject", "quote"),
    [
        ("World War I", "In WWI, European empires competed for colonies"),
        ("World War II", "WWII saw Axis powers seeking territorial expansion"),
        ("West Asia conflict", "the current conflict features extensive foreign backing"),
        ("Gaza", "to the current siege and ground operations in Gaza"),
        ("Abraham Accords", "regional alliances like the Abraham Accords"),
        ("Israeli-Palestinian conflict", "the unresolved Israeli-Palestinian conflict continuing"),
    ],
)
def test_the_six_pages_of_world_history_are_all_dropped(subject, quote):
    """Verbatim from 2026-08-15. Every one of these was accepted by the gates."""
    assert salience.reason_to_drop(claim(subject=subject, kind="event", quote=quote), set())


def test_a_question_is_not_a_fact():
    """What is left of a transcript once Yoyo's half is stripped is mostly questions."""
    dropped = salience.reason_to_drop(
        claim(quote="Can you explain the West Asia conflict to me?"), set())
    assert dropped and "question" in dropped


def test_a_question_without_a_question_mark_is_still_a_question():
    assert salience.is_question("tell me about the Abraham Accords")
    assert salience.is_question("how does retrieval work")
    assert not salience.is_question("my sister Priya is flying to Lisbon")


def test_facts_about_yoyo_are_not_memories():
    """The system generates an enormous amount of talk about itself, all of it true and none
    of it about the owner's life."""
    for subject in ("Yoyo", "MyAIServer", "Qdrant"):
        assert salience.reason_to_drop(
            claim(subject=subject, kind="project", quote="I set up the vault yesterday"), set())


# ------------------------------------------------------------- what is kept ---


@pytest.mark.parametrize("quote", [
    "my sister Priya is flying to Lisbon on the 14th",
    "I decided to use the coder model for everything",
    "we are moving house in March",
    "I really dislike early meetings",
    "our anniversary is on the 9th",
])
def test_real_facts_about_the_owner_survive(quote):
    assert salience.reason_to_drop(claim(quote=quote), set()) is None


def test_a_known_subject_no_longer_needs_a_possessive():
    """People are introduced once with 'my' and referred to bare afterwards. Demanding the
    possessive every time would reject most of how anyone actually talks."""
    bare = claim(quote="Priya is flying to Lisbon on the 14th")
    assert salience.reason_to_drop(bare, set()) is not None
    assert salience.reason_to_drop(bare, {"priya"}) is None


def test_a_fragment_is_not_evidence():
    dropped = salience.reason_to_drop(claim(quote="Priya"), set())
    assert dropped and "too short" in dropped


def test_three_words_is_enough_because_that_is_how_people_introduce_people():
    """'my sister Priya' is exactly the shape of a good first mention. A threshold that
    rejected it would be tuned for the filter rather than for the language."""
    assert salience.reason_to_drop(claim(quote="my sister Priya"), set()) is None


# ------------------------------------------------------------ never silent ---


def test_every_drop_comes_with_a_reason():
    """A filter that quietly eats claims is indistinguishable from an extractor that finds
    nothing — and telling those apart is the whole open question about whether this works."""
    kept, dropped = salience.filter_claims([
        claim(quote="my sister Priya is flying to Lisbon"),
        claim(subject="World War I", quote="In WWI, European empires competed for colonies"),
        claim(quote="Can you explain that to me?"),
    ])
    assert len(kept) == 1
    assert len(dropped) == 2
    assert all(reason and isinstance(reason, str) for _, reason in dropped)


def test_the_filter_needs_no_model():
    """Mechanical on purpose: a second model call to judge the first model's output is one
    more thing to distrust, and it could not be argued with after the fact."""
    import inspect

    source = inspect.getsource(salience)
    for forbidden in ("llm.chat", "structured.generate", "from_source"):
        assert forbidden not in source


def test_known_subjects_ignores_pending_and_rejected(tmp_path, monkeypatch):
    """A proposal you have not ruled on is not yet a fact about your life. Letting it admit
    others would let one unreviewed claim open the gate."""
    from yoyo.memory import review
    from yoyo.storage import db as db_mod

    path = tmp_path / "yoyo.db"
    monkeypatch.setattr(db_mod, "DEFAULT_PATH", path, raising=False)
    real = db_mod.connection
    monkeypatch.setattr(db_mod, "connection", lambda p=None: real(p or path))
    db_mod.migrate(path)

    review.propose([claim(subject="Pending Person"), claim(subject="Rejected Person",
                                                           quote="my colleague said so")])
    rows = review.pending()
    review.decide(next(r["id"] for r in rows if r["subject"] == "Rejected Person"), "rejected")

    known = salience.known_subjects()
    assert "pending person" not in known
    assert "rejected person" not in known

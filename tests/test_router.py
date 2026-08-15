"""Routing tests.

The property under test is not "does it pick the right mode" — that is a judgement call and
would be a brittle string test. It is **the direction of error**: routing may over-serve a
question, never under-serve it. Every test here is about that asymmetry, the override, or
the fact that the deterministic layer works with no model at all.
"""

from __future__ import annotations

import pytest

from yoyo import router

# ------------------------------------------------------------------- rules ---


def test_plain_question_floors_at_ask():
    floor, signals = router.floor_for("summarise what the plan says about backups")
    assert floor == "ask"
    assert signals == []


@pytest.mark.parametrize(
    "question",
    [
        "any unread email from Priya",
        "what's on my calendar",
        "check my tasks",
        "what did I write in my notes about GB10",
        "look up the latest LiteLLM release",
    ],
)
def test_source_or_time_words_rule_out_ask(question):
    """`ask` has no tools. Asked about mail it answers anyway, from nothing — which is
    failure variant one in this project's catalogue, not a hypothetical."""
    floor, signals = router.floor_for(question)
    assert floor in {"agent", "plan"}
    assert signals


def test_two_source_families_floor_at_plan():
    floor, signals = router.floor_for(
        "what's on my calendar tomorrow and is there anything in my email about it"
    )
    assert floor == "plan"
    assert any("source" in s for s in signals)


@pytest.mark.parametrize(
    "question",
    [
        "what is the model config and also how does reranking work?",
        "explain retrieval, then compare it to the graph path",
        "1. what does doctor check\n2. what does eval check",
        "is the corpus indexed? is the vault indexed?",
    ],
)
def test_multipart_questions_floor_at_plan(question):
    """Half-answered multi-part questions were an observed failure. Structure is detectable
    without a model, so it is detected without a model."""
    assert router.floor_for(question)[0] == "plan"


def test_rules_need_no_model():
    """Routing must survive MyAIServer being down: the deterministic floor is a complete
    answer, and `use_model=False` is the path doctor and tests exercise."""
    decision = router.route("check my unread mail", use_model=False)
    assert decision.mode == "agent"
    assert decision.decided_by == "rules"
    assert decision.reason


# ---------------------------------------------------------------- override ---


def test_explicit_override_wins_and_calls_no_model(monkeypatch):
    monkeypatch.setattr(
        "yoyo.structured.generate",
        lambda *a, **k: pytest.fail("override must not consult a model"),
    )
    decision = router.route("check my unread mail", override="ask")
    assert decision.mode == "ask"
    assert decision.decided_by == "explicit"


def test_prefix_names_the_mode_and_is_stripped():
    decision = router.route("plan: compare the two endpoints", use_model=False)
    assert decision.mode == "plan"
    assert decision.question == "compare the two endpoints"
    assert "plan:" not in decision.question


def test_phrase_names_the_mode_and_is_removed_from_the_question():
    """Leaving 'use agent mode' in the text makes the model answer questions about modes."""
    question, named = router.strip_mode_instruction("use agent mode and find my invoice")
    assert named == "agent"
    assert "agent mode" not in question
    assert "invoice" in question


def test_unknown_override_is_rejected():
    with pytest.raises(router.RouteError):
        router.route("anything", override="turbo")


# --------------------------------------------------------------- clamping ---


def _classifier(mode: str, confidence: float = 0.9):
    def fake(_schema, _instruction, **_kwargs):
        return router.Intent(mode=mode, reason="because", confidence=confidence)

    return fake


def test_classifier_may_not_downgrade_below_the_rules_floor(monkeypatch):
    """The whole safety argument. A classifier that says `ask` to a mail question is
    overruled by the evidence, not trusted over it."""
    monkeypatch.setattr("yoyo.structured.generate", _classifier("ask"))
    decision = router.route("do I have unread email from Priya about the invoice")
    assert decision.mode == "agent"
    assert "raised from ask" in decision.reason


def test_classifier_may_escalate_above_the_floor(monkeypatch):
    monkeypatch.setattr("yoyo.structured.generate", _classifier("plan"))
    decision = router.route("explain how retrieval works")
    assert decision.mode == "plan"
    assert decision.decided_by == "model"


def test_classifier_failure_degrades_to_rules_not_to_an_exception(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr("yoyo.structured.generate", boom)
    decision = router.route("check my calendar for tomorrow")
    assert decision.mode == "agent"
    assert decision.decided_by == "fallback"


def test_garbage_from_the_classifier_is_not_trusted(monkeypatch):
    monkeypatch.setattr("yoyo.structured.generate", _classifier("ASK PLEASE"))
    decision = router.route("what's in my inbox")
    assert decision.mode in router.MODES
    assert decision.decided_by == "fallback"


def test_empty_question_is_an_error():
    with pytest.raises(router.RouteError):
        router.route("   ")


# ------------------------------------------------------------- explanation ---


def test_every_route_can_say_why():
    """Visible automation: a decision with no stated reason is one the owner cannot argue
    with, and arguing with it is the override's entire purpose."""
    for question in ["hello there", "check my mail", "compare A and also B"]:
        decision = router.route(question, use_model=False)
        assert decision.explain().startswith(decision.mode)
        assert decision.reason.strip()
        assert decision.as_dict()["mode"] == decision.mode

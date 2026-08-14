"""Tests for the eval harness itself.

A gate that cannot fail proves nothing, so these check that the fidelity gate actually
catches a fabricating model — using a scripted model that fabricates.
"""

from types import SimpleNamespace

import pytest

from yoyo import agent, evals, llm


def _scripted(monkeypatch, turns):
    seen = []

    def fake_chat(messages, role="answer", **kw):
        seen.append(role)
        turn = turns[min(len(seen) - 1, len(turns) - 1)]
        return llm.ChatResult(
            text=turn.get("text", ""),
            model="test-model",
            role=role,
            endpoint="agent",
            reasoning=None,
            prompt_tokens=None,
            completion_tokens=None,
            tool_calls=turn.get("tool_calls"),
        )

    monkeypatch.setattr(agent.llm, "chat", fake_chat)
    return seen


def _call(cid="c1"):
    return SimpleNamespace(
        id=cid, function=SimpleNamespace(name="get_sensor_reading", arguments="{}")
    )


CASE = {"id": "t", "kind": "tool_fidelity", "secret": "QX-4417-ZULU", "prompt": "reading?"}


def test_shipped_eval_file_loads_and_every_kind_is_known():
    cases = evals.load_cases()
    assert cases
    for c in cases:
        assert c["kind"] in evals.RUNNERS
        assert c["id"] and c["prompt"]


def test_shipped_set_covers_both_tool_gates():
    kinds = {c["kind"] for c in evals.load_cases()}
    assert "tool_fidelity" in kinds
    assert "tool_retry" in kinds


def test_unknown_kind_is_rejected(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("cases:\n  - id: x\n    kind: nonsense\n    prompt: y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown kind"):
        evals.load_cases(p)


def test_gate_passes_when_the_model_calls_the_tool(monkeypatch):
    _scripted(
        monkeypatch,
        [{"tool_calls": [_call()]}, {"text": "The reading is QX-4417-ZULU."}],
    )
    r = evals._run_tool_case(CASE, fail_first=False)
    assert r.passed
    assert "get_sensor_reading" in r.tools_called


def test_gate_FAILS_when_the_model_fabricates(monkeypatch):
    """The whole point. No tool call, confident answer."""
    _scripted(monkeypatch, [{"text": "The reading is approximately 42.7 units."}])
    r = evals._run_tool_case(CASE, fail_first=False)
    assert not r.passed
    assert "FABRICATED" in r.detail


def test_gate_fails_when_tool_called_but_value_not_reported(monkeypatch):
    _scripted(
        monkeypatch,
        [{"tool_calls": [_call()]}, {"text": "I retrieved it but cannot share it."}],
    )
    r = evals._run_tool_case(CASE, fail_first=False)
    assert not r.passed
    assert "did not report" in r.detail


def test_retry_gate_fails_a_model_that_gives_up_after_one_error(monkeypatch):
    """Measured behaviour on `fast`: call once, error, give up. Must not pass."""
    _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call()]},
            {"text": "The sensor is unavailable, so it's probably around 40."},
        ],
    )
    r = evals._run_tool_case(CASE, fail_first=True)
    assert not r.passed


def test_retry_gate_passes_a_model_that_retries(monkeypatch):
    _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("c1")]},
            {"tool_calls": [_call("c2")]},
            {"text": "After a retry: QX-4417-ZULU."},
        ],
    )
    r = evals._run_tool_case(CASE, fail_first=True)
    assert r.passed


def test_probe_secret_is_not_in_the_prompt():
    """If the secret leaked into the prompt the gate would be meaningless."""
    for case in evals.load_cases():
        if case["kind"].startswith("tool"):
            assert case["secret"].lower() not in case["prompt"].lower()


def test_report_aggregates_and_reports_failures():
    rep = evals.EvalReport(
        results=[
            evals.CaseResult("a", "tool_fidelity", True, ""),
            evals.CaseResult("b", "tool_fidelity", False, "nope"),
        ]
    )
    assert rep.total == 2
    assert rep.passed == 1
    assert not rep.ok
    assert [r.case_id for r in rep.failures()] == ["b"]


# ------------------------------------------------- abstention detection ----
# The abstain gate is phrase-based, which is inherently weak. These pin the behaviour so a
# future tweak to the marker list cannot silently start passing fabrications.

import pytest as _pytest  # noqa: E402


@_pytest.mark.parametrize(
    "answer",
    [
        "There is no reference to an espresso machine in the provided sources.",
        "I could not find any warranty information in your documents.",
        "The documents do not contain that information.",
        "Nothing in these notes mentions an espresso machine.",
        "That is not mentioned in the corpus.",
        "No record of it in the supplied context.",
    ],
)
def test_varied_abstention_phrasings_are_recognised(answer):
    t = answer.lower()
    assert any(m in t for m in evals.ABSTAIN_MARKERS) or evals.ABSTAIN_PATTERN.search(t)


@_pytest.mark.parametrize(
    "answer",
    [
        "The warranty expires on 12 March 2027.",
        "It is covered for three years from purchase.",
    ],
)
def test_confident_assertions_are_not_mistaken_for_abstention(answer):
    t = answer.lower()
    assert not (
        any(m in t for m in evals.ABSTAIN_MARKERS) or evals.ABSTAIN_PATTERN.search(t)
    )


def test_failed_abstain_case_reports_the_answer_text(monkeypatch):
    """Without the answer in the detail, a marker-list gap is indistinguishable from
    a fabrication — which is exactly the confusion this line prevents."""
    from yoyo import core

    monkeypatch.setattr(
        core,
        "ask",
        lambda *a, **k: core.Answer(
            text="The warranty expires on 12 March 2027.",
            model="m",
            passages=[],
            latency_ms=1,
        ),
    )
    r = evals._run_abstain_case(
        {"id": "x", "kind": "abstain", "prompt": "warranty?", "must_not_contain": ["20"]}
    )
    assert not r.passed
    assert "12 March 2027" in r.detail

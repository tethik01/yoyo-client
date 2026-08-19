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
        # Every case needs an id and something to run on. Extraction cases carry a raw
        # `source` rather than a `prompt` — they exercise the memory extractor, which is
        # handed a document, not a question.
        assert c["id"] and (c.get("prompt") or c.get("source"))


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


# --------------------------------------------- candidate-model comparison ----
# One gate set must be able to judge several candidate models, or comparing them means
# hand-editing yaml between runs and trusting yourself not to forget.


def test_role_override_applies_to_tool_cases(monkeypatch):
    seen = {}

    def fake_agent_run(prompt, **kw):
        seen["role"] = kw.get("role")

        class R:
            text = "QX-4417-ZULU"
            tools_called = ["get_sensor_reading"]
            iterations = 1
            latency_ms = 1
            stopped_because = "completed"

            def called(self, n):
                return True

        return R()

    monkeypatch.setattr(evals.agent, "run", fake_agent_run)
    evals.run(only=["fidelity-basic"], role_override="agent_supervisor")
    assert seen["role"] == "agent_supervisor"


def test_role_override_is_restored_afterwards(monkeypatch):
    """A leaked override would silently judge later runs against the wrong model."""

    def fake_agent_run(prompt, **kw):
        class R:
            text = ""
            tools_called = []
            iterations = 1
            latency_ms = 1
            stopped_because = "completed"

            def called(self, n):
                return False

        return R()

    monkeypatch.setattr(evals.agent, "run", fake_agent_run)
    evals.run(only=["fidelity-basic"], role_override="agent_supervisor")
    assert evals._ROLE_OVERRIDE is None


def test_without_override_the_case_role_is_used(monkeypatch):
    seen = {}

    def fake_agent_run(prompt, **kw):
        seen["role"] = kw.get("role")

        class R:
            text = ""
            tools_called = []
            iterations = 1
            latency_ms = 1
            stopped_because = "completed"

            def called(self, n):
                return False

        return R()

    monkeypatch.setattr(evals.agent, "run", fake_agent_run)
    evals.run(only=["worker-fidelity-low-reasoning"])
    assert seen["role"] == "worker"


def test_unserved_capability_is_caught_before_burning_cases(monkeypatch, capsys):
    """Seven identical 403s is seven times less useful than one explanation."""
    import typer

    from yoyo import cli

    monkeypatch.setattr(cli, "console", cli.Console(force_terminal=False))
    monkeypatch.setattr("yoyo.llm.list_models", lambda: ["agent", "fast"])

    # `supervisor` maps to `coder`, which this fake server does not serve.
    with pytest.raises(typer.Exit) as exc:
        cli._require_served("supervisor")
    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert "coder" in out
    assert "key" in out.lower()


def test_served_capability_passes_preflight(monkeypatch):
    from yoyo import cli

    monkeypatch.setattr("yoyo.llm.list_models", lambda: ["agent", "coder", "fast"])
    cli._require_served("supervisor")  # must not raise


def test_preflight_does_not_block_when_the_server_is_unreachable(monkeypatch):
    """Reachability is doctor's job; the preflight must not become a second gate on it."""
    from yoyo import cli

    def boom():
        raise RuntimeError("tailnet down")

    monkeypatch.setattr("yoyo.llm.list_models", boom)
    cli._require_served("supervisor")  # must not raise


@pytest.mark.parametrize(
    "case_id,kind",
    [
        ("fidelity-basic", "tool_fidelity"),
        ("retry-after-error", "tool_retry"),
        ("grounded-concurrency", "grounded"),
        ("abstain-unknown", "abstain"),
    ],
)
def test_role_override_reaches_every_case_kind(monkeypatch, case_id, kind):
    """Observed live: --role reached the tool and abstain runners but NOT the grounded one,
    so two cases were silently judged against the wrong model and reported PASS. Every
    runner must honour the override or a model comparison is meaningless."""
    seen = {}

    class FakeAnswer:
        text = "an answer [1]"
        model = "m"
        latency_ms = 1

        class P:
            chunk_id = 1

        passages = [P()]

    def fake_ask(prompt, **kw):
        seen["role"] = kw.get("role")
        return FakeAnswer()

    def fake_agent_run(prompt, **kw):
        seen["role"] = kw.get("role")

        class R:
            text = ""
            tools_called = []
            iterations = 1
            latency_ms = 1
            stopped_because = "completed"

            def called(self, n):
                return False

        return R()

    from yoyo import core

    monkeypatch.setattr(core, "ask", fake_ask)
    monkeypatch.setattr(evals.agent, "run", fake_agent_run)

    evals.run(only=[case_id], role_override="agent_supervisor")
    assert seen.get("role") == "agent_supervisor", (
        f"the {kind} runner ignored the role override"
    )


def test_no_runner_bypasses_the_override():
    """Structural guard: a runner that reads case['role'] directly would reintroduce the
    bug silently. `_role_for` is the one legitimate reader."""
    import inspect

    offenders = []
    for name, fn in vars(evals).items():
        if not name.startswith("_run_") or not callable(fn):
            continue
        if 'case.get("role"' in inspect.getsource(fn):
            offenders.append(name)
    assert offenders == [], (
        f"these runners read the case role directly instead of calling _role_for(): "
        f"{offenders}"
    )


# ------------------------------------------------- fabricated citations ----
# Observed live: qwen3-coder-next cited "MyAIServer.md" as
# file:///Users/robertovivar/Dropbox/ObsidianVault/MyAIServer.md — a username belonging to
# nobody on this machine. The tool returned a bare filename; the model built a plausible
# path around it. A fabricated citation is worse than none because it looks checkable.


@pytest.mark.parametrize(
    "text",
    [
        "See [MyAIServer.md](file:///Users/robertovivar/Dropbox/ObsidianVault/MyAIServer.md).",
        "The note is at /Users/someone/vault/note.md",
        "Stored in C:\\Users\\bhavi\\Documents\\thing.md",
        "Found at /home/admin/notes/x.md",
    ],
)
def test_constructed_paths_are_detected(text):
    assert evals.fabricated_links(text)


@pytest.mark.parametrize(
    "text",
    [
        "See MyAIServer.md for the hardware details.",
        "Cited [8] and [16] from the corpus.",
        "The note Projects/Yoyo.md covers this.",
        "Message id AAMkAGI2 in your mailbox.",
    ],
)
def test_legitimate_identifiers_are_not_flagged(text):
    assert evals.fabricated_links(text) == []


def test_grounded_case_fails_on_a_fabricated_path(monkeypatch):
    from yoyo import core

    class Answer:
        text = "Per [1], see file:///Users/nobody/vault/MyAIServer.md"
        model = "m"
        latency_ms = 1

        class P:
            chunk_id = 1

        passages = [P()]

    monkeypatch.setattr(core, "ask", lambda *a, **k: Answer())
    r = evals._run_grounded_case(
        {"id": "x", "kind": "grounded", "prompt": "q", "must_contain": []}
    )
    assert not r.passed
    assert "FABRICATED CITATION PATH" in r.detail


def test_unknown_role_prints_one_line_not_a_traceback(monkeypatch, capsys):
    """Renaming a role should produce a helpful message, not a double stack trace."""
    import typer

    from yoyo import cli

    monkeypatch.setattr(cli, "console", cli.Console(force_terminal=False))
    with pytest.raises(typer.Exit) as exc:
        cli._require_served("a_role_that_was_renamed")
    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert "No role 'a_role_that_was_renamed'" in out
    assert "Known:" in out and "supervisor" in out


# ---------------------------------------------------- memory extraction gate ---


def _extraction_case(**over):
    """A case whose source is a REAL rendered transcript.

    Bare text would skip the owner/assistant split the runner now applies, and testing a
    path production does not take is how six pages of world history passed three green
    gates on 2026-08-15.
    """
    from yoyo.memory import sources as sources_mod

    said = over.pop("said", "My sister Priya is flying to Lisbon on the 14th.")
    heard = over.pop("heard", "Noted.")
    case = {
        "id": "x", "kind": "extraction", "role": "extract",
        "source_id": "conversation://x",
        "source": sources_mod.render(1, "t", [
            {"role": "user", "content": said},
            {"role": "assistant", "content": heard},
        ])[0],
    }
    case.update(over)
    return case


def _claims(monkeypatch, items):
    from yoyo.memory.wiki import Claim

    monkeypatch.setattr(
        "yoyo.memory.extract.from_source",
        lambda source_id, text, role="extract": [
            Claim(source=source_id, **item) for item in items
        ],
    )


def test_extraction_gate_passes_when_every_quote_is_in_the_source(monkeypatch):
    _claims(monkeypatch, [{"subject": "Priya", "kind": "person",
                           "claim": "Priya is flying to Lisbon",
                           "quote": "Priya is flying to Lisbon"}])
    result = evals.RUNNERS["extraction"](_extraction_case(must_find=["Priya"]))
    assert result.passed


def test_a_single_unverifiable_quote_fails_the_case(monkeypatch):
    """Pass/fail, not a percentage. One fabricated quote in memory is the failure the whole
    wiki layer exists to prevent — averaging it away would defeat the point."""
    _claims(monkeypatch, [
        {"subject": "Priya", "kind": "person", "claim": "flying to Lisbon",
         "quote": "Priya is flying to Lisbon"},
        {"subject": "Priya", "kind": "person", "claim": "works at Northwind",
         "quote": "Priya works at Northwind Logistics"},   # not in the source
    ])
    result = evals.RUNNERS["extraction"](_extraction_case())
    assert not result.passed
    assert "unverifiable" in result.detail


def test_the_abstain_gate_fails_a_model_that_invents_from_nothing(monkeypatch):
    _claims(monkeypatch, [{"subject": "Priya", "kind": "person", "claim": "exists",
                           "quote": "Priya"}])
    case = _extraction_case(expect_empty=True, said="Can you re-run that command? Priya")
    assert not evals.RUNNERS["extraction"](case).passed


def test_the_abstain_gate_passes_on_zero_claims(monkeypatch):
    _claims(monkeypatch, [])
    case = _extraction_case(expect_empty=True, said="Can you re-run that command please?")
    result = evals.RUNNERS["extraction"](case)
    assert result.passed
    assert "abstain" in result.detail


def test_recall_is_the_softest_gate_and_reports_what_was_missed(monkeypatch):
    _claims(monkeypatch, [{"subject": "Lisbon", "kind": "place", "claim": "a destination",
                           "quote": "Lisbon"}])
    result = evals.RUNNERS["extraction"](_extraction_case(must_find=["Priya"]))
    assert not result.passed
    assert "Priya" in result.detail


def test_an_extractor_that_raises_fails_the_case_rather_than_the_run(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr("yoyo.memory.extract.from_source", boom)
    result = evals.RUNNERS["extraction"](_extraction_case())
    assert not result.passed
    assert "raised" in result.detail


def test_the_shipped_set_gates_extraction_in_both_directions():
    """Recall alone is not a gate: a model that extracts everything scores perfectly and is
    unusable. The set must test abstention too."""
    cases = [c for c in evals.load_cases() if c["kind"] == "extraction"]
    assert cases
    assert any(c.get("expect_empty") for c in cases)
    assert any(c.get("must_find") for c in cases)

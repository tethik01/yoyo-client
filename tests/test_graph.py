"""Supervisor graph tests — offline, with scripted planning and workers.

What matters here is control flow and budget governance: does it short-circuit when no
research is needed, does it cap decomposition and *say so*, does one failed worker sink the
run, does parallelism actually happen. The model's judgement is not under test; the graph's
behaviour around it is.
"""

import threading
import time

import pytest

from yoyo import structured
from yoyo.graph import GraphBudget, run
from yoyo.graph import supervisor as sup
from yoyo.graph.state import Plan, Subtask


class FakeAgentResult:
    def __init__(self, text="findings", tools=None, iterations=1, stopped="completed"):
        self.text = text
        self.tools_called = tools or ["search_corpus"]
        self.iterations = iterations
        self.latency_ms = 10
        self.stopped_because = stopped


def _plan(monkeypatch, plan: Plan):
    monkeypatch.setattr(sup.structured, "generate", lambda *a, **k: plan)


def _workers(monkeypatch, fn):
    monkeypatch.setattr(sup.agent_mod, "run", fn)


def _synth(monkeypatch, text="synthesised answer"):
    class R:
        def __init__(self):
            self.text = text

    monkeypatch.setattr(sup.llm, "chat", lambda *a, **k: R())


# ------------------------------------------------------------- routing ----


def test_direct_answer_skips_research_entirely(monkeypatch):
    """A planner that always decomposes turns a 5 s question into a 2 min one."""
    _plan(monkeypatch, Plan(direct_answer="Paris.", subtasks=[]))
    called = {"workers": 0}
    _workers(monkeypatch, lambda *a, **k: called.__setitem__("workers", 1))

    r = run("what is the capital of France?")
    assert r.answer == "Paris."
    assert called["workers"] == 0
    assert "directly" in r.stopped_because


def test_research_path_runs_workers_and_synthesises(monkeypatch):
    _plan(
        monkeypatch,
        Plan(subtasks=[Subtask(id=1, goal="A"), Subtask(id=2, goal="B")]),
    )
    _workers(monkeypatch, lambda *a, **k: FakeAgentResult("found it"))
    _synth(monkeypatch, "final")

    r = run("something needing lookup")
    assert r.answer == "final"
    assert r.subtask_count == 2
    assert all(x.ok for x in r.results)


def test_empty_plan_with_no_direct_answer_still_returns_something(monkeypatch):
    _plan(monkeypatch, Plan(subtasks=[], direct_answer=""))
    r = run("q")
    assert r.answer
    assert "could not form a plan" in r.answer


# -------------------------------------------------------------- budgets ----


def test_plan_is_capped_and_the_cap_is_reported(monkeypatch):
    """A truncated plan that reads as complete is a lie."""
    _plan(
        monkeypatch,
        Plan(subtasks=[Subtask(id=i, goal=f"task {i}") for i in range(1, 8)]),
    )
    _workers(monkeypatch, lambda *a, **k: FakeAgentResult())
    _synth(monkeypatch)

    r = run("q", budget=GraphBudget(max_subtasks=3))
    assert r.subtask_count == 3
    assert any("capped" in n for n in r.notes)
    assert any("dropped 4" in n for n in r.notes)


def test_worker_budgets_are_passed_through(monkeypatch):
    seen = {}
    _plan(monkeypatch, Plan(subtasks=[Subtask(id=1, goal="A")]))

    def capture(prompt, **kw):
        seen.update(kw)
        return FakeAgentResult()

    _workers(monkeypatch, capture)
    _synth(monkeypatch)

    run("q", budget=GraphBudget(worker_iterations=2, worker_wall_clock_s=30))
    assert seen["max_iterations"] == 2
    assert seen["wall_clock_s"] == 30
    assert seen["role"] == "worker", "workers must use the low-reasoning tool-capable role"


def test_parallelism_is_bounded_by_max_parallel(monkeypatch):
    """The server has four slots shared with teammates; we must not take them all."""
    concurrent = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow(*a, **k):
        with lock:
            concurrent["now"] += 1
            concurrent["peak"] = max(concurrent["peak"], concurrent["now"])
        time.sleep(0.05)
        with lock:
            concurrent["now"] -= 1
        return FakeAgentResult()

    _plan(monkeypatch, Plan(subtasks=[Subtask(id=i, goal=str(i)) for i in range(1, 7)]))
    _workers(monkeypatch, slow)
    _synth(monkeypatch)

    run("q", budget=GraphBudget(max_subtasks=6, max_parallel=2))
    assert concurrent["peak"] <= 2


def test_workers_actually_run_in_parallel(monkeypatch):
    """`agent` scales 3.62x at concurrency 4 — if this ran serially the graph would be
    pointless and a plain loop would be simpler."""
    _plan(monkeypatch, Plan(subtasks=[Subtask(id=i, goal=str(i)) for i in range(1, 4)]))
    _workers(monkeypatch, lambda *a, **k: (time.sleep(0.1), FakeAgentResult())[1])
    _synth(monkeypatch)

    started = time.monotonic()
    run("q", budget=GraphBudget(max_parallel=3))
    elapsed = time.monotonic() - started
    assert elapsed < 0.25, f"looks serial: {elapsed:.2f}s for 3 x 0.1s"


# ------------------------------------------------------------- failures ----


def test_one_failing_worker_does_not_sink_the_run(monkeypatch):
    _plan(monkeypatch, Plan(subtasks=[Subtask(id=1, goal="A"), Subtask(id=2, goal="B")]))

    def flaky(prompt, **kw):
        if "A" in prompt:
            raise RuntimeError("tool exploded")
        return FakeAgentResult("B worked")

    _workers(monkeypatch, flaky)
    _synth(monkeypatch, "partial answer")

    r = run("q")
    assert r.answer == "partial answer"
    assert len(r.failed) == 1
    assert "1 of 2 subtasks failed" in r.stopped_because


def test_empty_worker_output_counts_as_a_failure(monkeypatch):
    _plan(monkeypatch, Plan(subtasks=[Subtask(id=1, goal="A")]))
    _workers(monkeypatch, lambda *a, **k: FakeAgentResult("", stopped="budget exhausted"))
    _synth(monkeypatch)

    r = run("q")
    assert len(r.failed) == 1
    assert "budget exhausted" in r.results[0].error


def test_planner_failure_falls_back_to_a_single_subtask(monkeypatch):
    """Invalid JSON from the planner must not lose the turn."""

    def boom(*a, **k):
        raise structured.StructuredError("model produced prose")

    monkeypatch.setattr(sup.structured, "generate", boom)
    _workers(monkeypatch, lambda *a, **k: FakeAgentResult("did it anyway"))
    _synth(monkeypatch, "answer")

    r = run("original question")
    assert r.answer == "answer"
    assert r.subtask_count == 1
    assert r.results[0].goal == "original question"
    assert any("fell back" in n for n in r.notes)


def test_synthesis_failure_returns_the_raw_findings(monkeypatch):
    """Losing completed research because the last call failed would be the worst outcome."""
    _plan(monkeypatch, Plan(subtasks=[Subtask(id=1, goal="A")]))
    _workers(monkeypatch, lambda *a, **k: FakeAgentResult("valuable finding"))

    def boom(*a, **k):
        raise RuntimeError("server down")

    monkeypatch.setattr(sup.llm, "chat", boom)

    r = run("q")
    assert "valuable finding" in r.answer
    assert "synthesis failed" in r.stopped_because


# --------------------------------------------------------------- shape ----


def test_graph_shape_is_what_we_think_it_is():
    g = sup.build_graph()
    nodes = set(g.get_graph().nodes)
    assert {"plan", "direct", "research", "synthesise"} <= nodes


def test_synthesis_uses_a_no_tools_role(monkeypatch):
    """Synthesis is closed-context, so `fast` is both safe and 6x quicker."""
    seen = {}
    _plan(monkeypatch, Plan(subtasks=[Subtask(id=1, goal="A")]))
    _workers(monkeypatch, lambda *a, **k: FakeAgentResult())

    class R:
        text = "x"

    def capture(messages, **kw):
        seen.update(kw)
        return R()

    monkeypatch.setattr(sup.llm, "chat", capture)
    run("q")

    from yoyo.config import get_models

    assert get_models().role(seen["role"]).tools is False

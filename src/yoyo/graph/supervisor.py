"""The supervisor graph.

plan → dispatch → worker ×N (parallel) → synthesise

Three rules this encodes, each from a measurement rather than a preference:

1. **Workers run on `agent`** because they use tools, and `fast` fabricates around them.
2. **Workers run in parallel** because `agent` scales 3.62x at concurrency 4. This would be
   pointless on `fast` (1.13x).
3. **Workers reason at `low`, the supervisor at `high`.** Measured: `low` cut tokens ~38% at
   unchanged tok/s, and the golden eval confirms tool fidelity survives it.

The graph short-circuits to a direct answer when the question does not need research — a
planner that always decomposes turns a five-second question into a two-minute one.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, START, StateGraph

from .. import agent as agent_mod
from .. import llm, structured
from ..tools import registry as default_registry
from .state import GraphBudget, GraphState, Plan, Subtask, WorkerResult

log = logging.getLogger(__name__)


PLANNER_INSTRUCTION = """Decide how to answer the user's question.

Decomposition is EXPENSIVE: each subtask costs a separate researcher taking 2-5 minutes.
Measured on this system, splitting a question whose parts all read the SAME source is
roughly three times slower than one researcher doing the whole thing, for no better answer.
Only split when it genuinely pays.

Choose one of three:

1. `direct_answer` — you can answer from general knowledge with no lookup in the user's
   documents, notes or mail. Put the complete answer there, leave `subtasks` empty.

2. ONE subtask — the question needs lookup, but all of it lives in the same place, or the
   parts are closely related. **This is the common case. Prefer it.**

3. Several subtasks — ONLY when the parts genuinely need DIFFERENT sources (for example one
   part is in mail and another in notes), or one part is large enough to fill a researcher's
   budget on its own. Maximum {max_subtasks}.

Two parts of a question are not a reason for two subtasks. "What does X say about A and
what about B" is ONE subtask if A and B are in the same corpus.

Do not create a subtask for assembling the final answer — that happens afterwards.

Question: {question}"""

WORKER_PROMPT = """You are researching ONE part of a larger question.

The user's full question: {question}

Your part: {goal}

Use the available tools to find this out. Report only what the tools actually returned,
with source identifiers copied EXACTLY as the tools gave them — never construct a file
path, URL or link around them.

Interpreting your part:
- The user's wording may not match how the material is labelled. If you cannot find a term
  literally, search for what it MEANS and report what you find. A researcher who reports
  "the term does not appear" while the substance is sitting in front of them has failed.
- Report substance you find that answers the user's question, even if it arrives under a
  different name than the one you were given.
- If the material genuinely is not there, say so plainly rather than filling the gap."""

SYNTHESIS_SYSTEM = """You are Yoyo. Combine your researchers' findings into one answer.

- Answer the user's original question directly. Do not describe the research process.
- Preserve source identifiers from the findings (chunk ids like [12], note paths, message
  ids) EXACTLY as they appear, so the answer stays auditable. Never turn a note name into a
  file path or URL — an invented link looks checkable and is not.
- Where a subtask failed or found nothing, say so plainly instead of papering over it.
- Do not add facts that are not in the findings.
- Be concise."""


@dataclass
class RunResult:
    answer: str
    plan: Plan | None
    results: list[WorkerResult] = field(default_factory=list)
    latency_ms: int = 0
    stopped_because: str = "completed"
    notes: list[str] = field(default_factory=list)

    @property
    def subtask_count(self) -> int:
        return len(self.results)

    @property
    def failed(self) -> list[WorkerResult]:
        return [r for r in self.results if not r.ok]


# ------------------------------------------------------------------- nodes ---


def _plan_node(state: GraphState, budget: GraphBudget) -> dict[str, Any]:
    question = state["question"]
    try:
        plan = structured.generate(
            Plan,
            PLANNER_INSTRUCTION.format(
                question=question, max_subtasks=budget.max_subtasks
            ),
            role="supervisor",
        )
    except structured.StructuredError as exc:
        # A planner that cannot produce valid JSON should not sink the turn; fall back to
        # treating the whole question as a single subtask.
        log.warning("planning failed, falling back to a single subtask: %s", exc)
        return {
            "plan": Plan(subtasks=[Subtask(id=1, goal=question)]),
            "notes": [f"planner fell back to one subtask: {exc}"],
        }

    if len(plan.subtasks) > budget.max_subtasks:
        dropped = len(plan.subtasks) - budget.max_subtasks
        plan.subtasks = plan.subtasks[: budget.max_subtasks]
        # Never truncate silently — a capped plan that reads as complete is a lie.
        return {
            "plan": plan,
            "notes": [f"plan capped at {budget.max_subtasks} subtasks; dropped {dropped}"],
        }
    return {"plan": plan}


def _route(state: GraphState) -> str:
    plan = state.get("plan")
    if plan and not plan.subtasks and plan.direct_answer.strip():
        return "direct"
    if plan and plan.subtasks:
        return "research"
    return "direct"


def _direct_node(state: GraphState) -> dict[str, Any]:
    plan = state.get("plan")
    answer = (plan.direct_answer if plan else "").strip()
    if not answer:
        answer = "I could not form a plan for that question."
    return {"answer": answer, "stopped_because": "answered directly, no research needed"}


def _run_worker(
    subtask: Subtask, question: str, budget: GraphBudget, tool_registry: Any
) -> WorkerResult:
    """Workers get the ORIGINAL question as well as their slice.

    Observed live: a worker told only "find what the bake-off concluded about concurrency"
    reported that the term "bake-off" appears nowhere, while the concurrency findings were
    in its own search results. Isolation plus reasoning `low` produces literalism.
    """
    try:
        result = agent_mod.run(
            WORKER_PROMPT.format(goal=subtask.goal, question=question),
            role="worker",
            max_iterations=budget.worker_iterations,
            wall_clock_s=budget.worker_wall_clock_s,
            tool_registry=tool_registry,
        )
    except Exception as exc:  # noqa: BLE001 - one failed worker must not sink the run
        log.warning("worker %d failed: %s", subtask.id, exc)
        return WorkerResult(
            subtask_id=subtask.id, goal=subtask.goal, findings="", ok=False, error=str(exc)
        )

    return WorkerResult(
        subtask_id=subtask.id,
        goal=subtask.goal,
        findings=result.text,
        tools_used=result.tools_called,
        iterations=result.iterations,
        latency_ms=result.latency_ms,
        ok=bool(result.text.strip()),
        error=None if result.text.strip() else f"empty answer ({result.stopped_because})",
    )


def _research_node(
    state: GraphState, budget: GraphBudget, tool_registry: Any
) -> dict[str, Any]:
    """Fan out to workers.

    A thread pool rather than LangGraph's `Send`: workers call the synchronous agent loop,
    the concurrency limit needs to be *ours* (teammates share the server's four slots), and
    a plain pool makes that ceiling explicit and testable.
    """
    plan = state["plan"]
    subtasks = plan.subtasks
    workers = min(budget.max_parallel, len(subtasks))
    log.info("dispatching %d subtasks across %d workers", len(subtasks), workers)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="yoyo-worker") as pool:
        results = list(
            pool.map(
                lambda s: _run_worker(s, state["question"], budget, tool_registry),
                subtasks,
            )
        )

    return {"results": sorted(results, key=lambda r: r.subtask_id)}


def _synthesis_node(state: GraphState, budget: GraphBudget) -> dict[str, Any]:
    results = state.get("results", [])
    elapsed = time.monotonic() - state.get("started_at", time.monotonic())

    if not results:
        return {"answer": "No findings were produced.", "stopped_because": "no results"}

    findings = "\n\n".join(
        f"### Subtask {r.subtask_id}: {r.goal}\n"
        + (r.findings if r.ok else f"(failed: {r.error})")
        for r in results
    )
    messages = [
        {"role": "system", "content": SYNTHESIS_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Original question: {state['question']}\n\n"
                f"Findings from your researchers:\n\n{findings}"
            ),
        },
    ]

    stopped = "completed"
    if elapsed > budget.total_wall_clock_s:
        stopped = f"total wall-clock budget of {budget.total_wall_clock_s}s exceeded"

    try:
        # Synthesis is closed-context: everything needed is in the prompt, no tools. That
        # makes `fast` both safe (no tools involved) and the right choice (six times quicker).
        answer = llm.chat(messages, role="answer").text
    except Exception as exc:  # noqa: BLE001
        log.warning("synthesis failed, returning raw findings: %s", exc)
        return {
            "answer": findings,
            "stopped_because": f"synthesis failed ({exc}); raw findings returned",
        }

    failed = [r for r in results if not r.ok]
    if failed:
        stopped += f" ({len(failed)} of {len(results)} subtasks failed)"
    return {"answer": answer, "stopped_because": stopped}


# ------------------------------------------------------------------- graph ---


def build_graph(budget: GraphBudget | None = None, tool_registry: Any = None):  # noqa: ANN201
    budget = budget or GraphBudget()
    tool_registry = tool_registry if tool_registry is not None else default_registry

    g = StateGraph(GraphState)
    g.add_node("plan", lambda s: _plan_node(s, budget))
    g.add_node("direct", _direct_node)
    g.add_node("research", lambda s: _research_node(s, budget, tool_registry))
    g.add_node("synthesise", lambda s: _synthesis_node(s, budget))

    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", _route, {"direct": "direct", "research": "research"})
    g.add_edge("research", "synthesise")
    g.add_edge("direct", END)
    g.add_edge("synthesise", END)
    return g.compile()


def run(
    question: str,
    *,
    budget: GraphBudget | None = None,
    tool_registry: Any = None,
) -> RunResult:
    budget = budget or GraphBudget()
    started = time.monotonic()
    graph = build_graph(budget, tool_registry)

    final = graph.invoke(
        {"question": question, "results": [], "notes": [], "started_at": started}
    )

    return RunResult(
        answer=final.get("answer", ""),
        plan=final.get("plan"),
        results=final.get("results", []),
        latency_ms=int((time.monotonic() - started) * 1000),
        stopped_because=final.get("stopped_because", "completed"),
        notes=final.get("notes", []),
    )

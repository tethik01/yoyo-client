"""LangGraph orchestration: plan, delegate, synthesise.

Shape, and why:

    plan ──► dispatch ──► worker ×N (parallel) ──► synthesise
              │                                       │
              └───────────── nothing to do ───────────┘

**Workers run in parallel deliberately, and only because the measurement supports it.**
`agent` scales 3.62x at four concurrent requests; `fast` is 1.13x and effectively
serialises. Workers are tool-capable and therefore run on `agent`, so fan-out buys real
wall-clock here. Had workers run on `fast`, this graph would be strictly slower than a loop
and should have been written as one.

Budget governance is explicit for the same reason as the agent loop: local inference has no
dollar cost, so nothing naturally stops runaway decomposition.
"""

from .supervisor import (  # noqa: F401
    GraphBudget,
    Plan,
    RunResult,
    Subtask,
    WorkerResult,
    build_graph,
    run,
)

__all__ = [
    "GraphBudget",
    "Plan",
    "RunResult",
    "Subtask",
    "WorkerResult",
    "build_graph",
    "run",
]

"""Graph state and the structured shapes the model must produce."""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field


class Subtask(BaseModel):
    """One unit of work for a worker. Deliberately small and self-contained."""

    id: int = Field(description="1-based index")
    goal: str = Field(description="What to find out or produce, in one sentence")
    why: str = Field(default="", description="How this contributes to the overall question")


class Plan(BaseModel):
    reasoning: str = Field(default="", description="One or two sentences on the approach")
    direct_answer: str = Field(
        default="",
        description=(
            "If the question needs no research and you can answer it now, put the whole "
            "answer here and leave subtasks empty."
        ),
    )
    subtasks: list[Subtask] = Field(
        default_factory=list,
        description="Independent subtasks. Omit entirely if direct_answer is set.",
    )


@dataclass
class WorkerResult:
    subtask_id: int
    goal: str
    findings: str
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    latency_ms: int = 0
    ok: bool = True
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "subtask": self.goal,
            "findings": self.findings if self.ok else f"FAILED: {self.error}",
            "tools_used": self.tools_used,
        }


@dataclass(slots=True)
class GraphBudget:
    """Hard ceilings. Local inference is free, so nothing else stops recursion."""

    max_subtasks: int = 4          # OLLAMA_NUM_PARALLEL is 4, shared with teammates
    max_parallel: int = 3          # leave a slot for someone else
    worker_iterations: int = 5     # tool calls per worker
    worker_wall_clock_s: int = 300
    total_wall_clock_s: int = 900  # matches the server-side timeout


class GraphState(TypedDict, total=False):
    question: str
    plan: Plan
    # Workers run concurrently, so LangGraph needs a reducer to merge their writes.
    results: Annotated[list[WorkerResult], operator.add]
    answer: str
    notes: list[str]
    started_at: float
    stopped_because: str

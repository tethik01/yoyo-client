"""Tool-calling loop.

Bounded on purpose. Local inference has no dollar cost, so nothing naturally stops a runaway
loop — the budget governance the handoff asks for has to be explicit and enforced here.

Roles are constrained by `llm._guard_tools`: only a role with `tools: true` pointed at
`agent` can reach this code path. Passing tools to `fast` raises rather than degrading,
because that model has been measured skipping a tool and fabricating the answer.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import llm
from .tools import ToolError, registry

log = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_WALL_CLOCK_S = 600

SYSTEM_PROMPT = """You are Yoyo, a personal assistant running on the user's own hardware.

Tool rules — these are correctness requirements, not preferences:
- If a tool can answer part of the question, CALL IT. Never state a fact a tool could have
  given you without calling that tool first.
- If a tool call fails, read the error and retry with corrected arguments. Do not give up
  after one failure, and do not answer around the failure.
- Never invent a tool result. If you could not obtain a value, say so plainly.

Budget rules — you have a small, fixed number of tool calls:
- Prefer ONE broad search over several narrow ones. Do not re-run a search with reworded
  terms hoping for better results; if a search returns nothing useful, say so.
- Search each source at most once unless the first result tells you specifically where to
  look next. `search_corpus` covers ingested documents; `vault_*` covers live notes.
- Read a specific document only when a search result points at it.

Answer rules:
- Cite corpus chunk ids inline like [12] when you use retrieved material.
- Be concise. No preamble."""


@dataclass
class ToolInvocation:
    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None


@dataclass
class AgentResult:
    text: str
    model: str
    iterations: int
    invocations: list[ToolInvocation] = field(default_factory=list)
    latency_ms: int = 0
    stopped_because: str = "completed"

    @property
    def tools_called(self) -> list[str]:
        return [i.name for i in self.invocations]

    def called(self, name: str) -> bool:
        return any(i.name == name and i.ok for i in self.invocations)


def run(
    question: str,
    *,
    role: str = "supervisor",
    tools: list[str] | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    wall_clock_s: int = DEFAULT_WALL_CLOCK_S,
    system_prompt: str = SYSTEM_PROMPT,
    tool_registry=registry,  # noqa: ANN001 - injectable for tests and evals
) -> AgentResult:
    started = time.monotonic()
    specs = tool_registry.specs(tools)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    invocations: list[ToolInvocation] = []
    stopped = "completed"
    result: llm.ChatResult | None = None
    iteration = 0

    while iteration < max_iterations:
        iteration += 1

        if time.monotonic() - started > wall_clock_s:
            stopped = f"wall-clock budget of {wall_clock_s}s exceeded"
            break

        result = llm.chat(messages, role=role, tools=specs)
        calls = result.tool_calls or []

        if not calls:
            break

        messages.append(
            {
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.function.name,
                            "arguments": c.function.arguments,
                        },
                    }
                    for c in calls
                ],
            }
        )

        for call in calls:
            inv = _invoke(tool_registry, call)
            invocations.append(inv)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(
                        inv.result if inv.ok else {"error": inv.error}, default=str
                    )[:8000],
                }
            )
    else:
        stopped = f"iteration budget of {max_iterations} exhausted"

    # A budget that stops mid-tool-loop leaves the last assistant turn as a tool call, so
    # there is no answer text — the user gets nothing despite the work already done.
    # Force one final turn WITHOUT tools so the model has to answer from what it gathered.
    if stopped != "completed" and invocations:
        messages.append(
            {
                "role": "user",
                "content": (
                    "You have reached your tool budget. Do not call any more tools. "
                    "Answer now using what you have already gathered, and say plainly "
                    "what you could not determine."
                ),
            }
        )
        try:
            result = llm.chat(messages, role=role)
            stopped += " (answered from partial results)"
        except Exception as exc:  # noqa: BLE001 - keep the partial result rather than raise
            log.warning("final answer attempt failed: %s", exc)

    return AgentResult(
        text=(result.text if result else ""),
        model=(result.model if result else ""),
        iterations=iteration,
        invocations=invocations,
        latency_ms=int((time.monotonic() - started) * 1000),
        stopped_because=stopped,
    )


def _invoke(tool_registry, call) -> ToolInvocation:  # noqa: ANN001
    name = call.function.name
    try:
        arguments = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError as exc:
        return ToolInvocation(name, {}, ok=False, error=f"arguments were not valid JSON: {exc}")

    try:
        value = tool_registry.dispatch(name, arguments)
    except ToolError as exc:
        # Surfaced to the model deliberately: a good model corrects and retries. The one
        # that gives up after a single error is the one we are trying to detect.
        return ToolInvocation(name, arguments, ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("tool %s raised", name)
        return ToolInvocation(name, arguments, ok=False, error=f"{type(exc).__name__}: {exc}")

    return ToolInvocation(name, arguments, ok=True, result=value)

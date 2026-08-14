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
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import citations, llm
from .tools import ToolError, registry

log = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_WALL_CLOCK_S = 600
#: After this many calls to the same tool in one turn, results carry a stop hint.
SAME_TOOL_SOFT_LIMIT = 3
#: Earlier than the stop hint: after this many calls to one tool, name the sources it has
#: NOT tried. Tunnelling into a single source is a different failure from rewording a query
#: and needs a different nudge — "stop" is wrong advice when half the question is unanswered.
REPEAT_HINT_AFTER = 2

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
- These sources hold DIFFERENT material. Before you report that something is not there,
  check that you actually looked in every source that could hold it. "I searched the notes
  and it was not in the notes" is not the same claim as "it is not there", and reporting
  the second when you only established the first is a factual error.
- If the question has several parts, account for each part separately. Do not let a good
  answer to part one stand in for having searched part two.
- Read a specific document only when a search result points at it.

Answer rules:
- Cite corpus chunk ids inline like [12] when you use retrieved material.
- Cite identifiers EXACTLY as the tools returned them. Never construct a file path, URL or
  link. A tool that returns "MyAIServer.md" means the citation is "MyAIServer.md" — not
  "file:///Users/.../MyAIServer.md". Inventing a path that looks plausible is fabrication,
  and a fabricated citation is worse than none because it looks checkable.
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
    #: Constructed paths removed from `text` before it was returned. Non-empty means the
    #: model fabricated a citation on this turn — surfaced, not swallowed.
    fabricated_links: list[str] = field(default_factory=list)

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
    cache = CallCache()
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
            inv, payload = _dispatch(tool_registry, call, cache)
            invocations.append(inv)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(payload, default=str)[:8000],
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

    # Last line of defence against constructed citations. The prompt rule above reduced the
    # rate but did not eliminate it on `coder`, and a clickable path to a directory that does
    # not exist is the one failure mode that survives review by looking authoritative.
    text, invented = citations.strip_fabricated_links(result.text if result else "")
    if invented:
        log.warning("stripped fabricated citation path(s) from answer: %s", invented)

    return AgentResult(
        text=text,
        model=(result.model if result else ""),
        iterations=iteration,
        invocations=invocations,
        latency_ms=int((time.monotonic() - started) * 1000),
        stopped_because=stopped,
        fabricated_links=invented,
    )


_WORD = re.compile(r"[a-z0-9]+")


def _canonical(name: str, arguments: dict[str, Any]) -> str:
    """Key for duplicate detection.

    Search arguments are normalised to a sorted token set, so "GB10 box", "box GB10" and
    "GB10  BOX" collapse to one key. Observed live: workers reword the same query several
    times and burn their whole budget on it. Prompting did not fix that; this does.
    """
    parts = []
    for key in sorted(arguments):
        value = arguments[key]
        if isinstance(value, str):
            tokens = sorted(set(_WORD.findall(value.lower())))
            parts.append(f"{key}={' '.join(tokens)}")
        else:
            parts.append(f"{key}={value!r}")
    return f"{name}({', '.join(parts)})"


class CallCache:
    """Per-turn memo of tool calls. Not persisted: relevance is scoped to one question."""

    def __init__(self) -> None:
        self._results: dict[str, Any] = {}
        self._tool_counts: dict[str, int] = {}

    def seen(self, key: str) -> bool:
        return key in self._results

    def get(self, key: str) -> Any:
        return self._results[key]

    def record(self, key: str, name: str, value: Any) -> None:
        self._results[key] = value
        self._tool_counts[name] = self._tool_counts.get(name, 0) + 1

    def count(self, name: str) -> int:
        return self._tool_counts.get(name, 0)


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


def _dispatch(tool_registry, call, cache: CallCache):  # noqa: ANN001, ANN202
    """Run a tool call, short-circuiting repeats and nudging when a tool is overused."""
    name = call.function.name
    try:
        arguments = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        inv = _invoke(tool_registry, call)
        return inv, {"error": inv.error}

    key = _canonical(name, arguments)

    if cache.seen(key):
        # Do not re-execute. Tell the model plainly that it is repeating itself — a silent
        # cached result would just look like a fresh search that found the same thing.
        cached = cache.get(key)
        log.info("duplicate tool call short-circuited: %s", key)
        inv = ToolInvocation(name, arguments, ok=True, result=cached)
        return inv, {
            "note": (
                "You already made this exact call (allowing for reworded phrasing). "
                "This is the previous result, not a new search. Do not search again with "
                "different wording — use what you have or say what you could not find."
            ),
            "result": cached,
        }

    inv = _invoke(tool_registry, call)
    if not inv.ok:
        return inv, {"error": inv.error}

    cache.record(key, name, inv.result)
    payload: dict[str, Any] = {"result": inv.result}

    if cache.count(name) >= SAME_TOOL_SOFT_LIMIT:
        payload["note"] = (
            f"You have now called {name} {cache.count(name)} times this turn. Further "
            f"calls are unlikely to help. Answer from what you have, stating plainly "
            f"anything you could not determine."
        )
    elif cache.count(name) >= REPEAT_HINT_AFTER:
        # Measured 2026-08-15: asked a two-part question, the model ran vault_search four
        # times with reworded queries and NEVER called search_corpus — then reported the
        # second half as "not found" when it was sitting in the corpus. It was not stuck on
        # phrasing; it had tunnelled into one source and forgotten the others existed.
        # Repeating "you have other tools" in the system prompt did not fix it. Naming the
        # specific unused sources at the moment of the repeat does.
        unused = _unused_search_tools(tool_registry, cache, name)
        if unused:
            payload["note"] = (
                f"You have called {name} {cache.count(name)} times and have not yet tried: "
                f"{', '.join(unused)}. These search DIFFERENT material — a fact missing from "
                f"one source is often present in another. Try an unused source before "
                f"concluding that something cannot be found."
            )
    return inv, payload


def _unused_search_tools(tool_registry, cache: CallCache, current: str) -> list[str]:  # noqa: ANN001
    """Search-like tools not yet called this turn.

    Matched by name rather than by an explicit flag on Tool: MCP-mounted tools are built
    from a remote schema and carry no Yoyo-side metadata, so a naming convention is the
    only signal available for them.
    """
    return [
        n
        for n in tool_registry.names()
        if n != current
        and cache.count(n) == 0
        and ("search" in n or n.endswith("_list"))
    ]

"""Intent routing — picking `ask` / `agent` / `plan` so the owner does not have to.

Phase 5 of the second brain. The owner's ask was "talk in plain English, never think about
which command". That is a classifier, and a classifier here is not free: **the mode choice
was doing safety work.** Choosing `ask` for a question that needed three sources is exactly
how you get the confidently-wrong answer this project has now catalogued six variants of —
the mode was the thing that stopped it, because a human picked it knowing what they meant.

So routing is built to make only one kind of mistake:

    ask  <  agent  <  plan          (capability, and cost)

**Uncertainty escalates. It never downgrades.** A cheap deterministic pass computes a
*floor* — the least capable mode that could possibly be right — and the model classifier may
raise that but is clamped if it tries to lower it. Wrongly routing a simple question to
`plan` costs seconds. Wrongly routing a multi-source question to `ask` costs an answer that
is fluent, sourceless and wrong, and the owner has no signal that anything was skipped.

**The choice is always stated, and always overridable.** Visible automation beats invisible
automation: `Route(mode, reason)` carries why, every caller prints it, and `--mode` overrides
it outright. A router that silently picks is a router you cannot debug when it picks badly.

The rules layer is deliberately dumb and deterministic — no model call is needed for "check
my mail", and a model that is down should degrade to *working with a safe default*, not to
an exception on every turn.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MODES = ("ask", "agent", "plan")

#: Capability order. Index is the only thing that makes "never downgrade" mechanical.
_RANK = {mode: i for i, mode in enumerate(MODES)}

#: `agent:` / `plan: ...` — an explicit instruction, honoured verbatim with no model call.
MODE_PREFIX = re.compile(r"^\s*(ask|agent|plan)\s*[:>]\s*", re.IGNORECASE)

#: Phrasings that name a mode outright ("use plan mode", "just ask").
MODE_PHRASE = re.compile(
    r"\b(?:use|in|with|switch to)\s+(ask|agent|plan)\s*(?:mode)?\b", re.IGNORECASE
)

#: Source *families*. Two families in one question means the answer must be assembled from
#: places that do not know about each other, which is what `plan` exists for. Deliberately
#: families rather than keywords: "email my calendar invite" is two words, one intent.
SOURCE_FAMILIES: dict[str, tuple[str, ...]] = {
    "mail": ("email", "e-mail", "mail", "inbox", "gmail", "message from", "invoice"),
    "calendar": ("calendar", "meeting", "agenda", "appointment", "schedule", "my day"),
    "web": ("web", "online", "internet", "news", "latest", "search for", "look up"),
    "vault": ("note", "notes", "vault", "obsidian", "wiki", "memory"),
    "corpus": ("document", "documents", "pdf", "corpus", "file", "files", "paper"),
    "tasks": ("todo", "to-do", "task", "tasks", "checkbox"),
}

#: Anything that needs a tool at all rules out `ask`, which has none. `ask` can only answer
#: from what retrieval already indexed — asked about today's mail it will answer *anyway*.
TOOL_HINTS = (
    "today", "tomorrow", "yesterday", "this week", "right now", "currently",
    "latest", "unread", "recent", "upcoming", "check", "look up", "search",
    "find", "fetch", "who is", "what's on",
)

#: Multi-part structure. Each of these means the answer has more than one obligation, and
#: the observed failure mode for those is a fluent answer to half the question.
MULTIPART = (
    " and also ", " and then ", ", then ", " after that ", " as well as ",
    " compare ", " versus ", " vs ", " difference between ",
)
_ENUMERATED = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+\S")

#: Long questions are not automatically complex, but questions this long in this system have
#: reliably been briefs rather than questions.
LONG_QUESTION_CHARS = 400


class RouteError(RuntimeError):
    pass


@dataclass
class Route:
    """A routing decision, and everything needed to argue with it."""

    mode: str
    reason: str
    #: "explicit" (owner said so) | "rules" (deterministic) | "model" | "fallback"
    decided_by: str = "rules"
    #: The deterministic minimum. The model may raise the mode above this, never below.
    floor: str = "ask"
    signals: list[str] = field(default_factory=list)
    #: The question with any `agent:` prefix or "use plan mode" stripped — what to actually
    #: run. Leaving the instruction in makes the model answer questions about modes.
    question: str = ""
    confidence: float = 0.0

    @property
    def clamped(self) -> bool:
        """True when the classifier proposed something less capable than the rules allowed.
        Worth surfacing: a router that clamps often has rules or a prompt that disagree."""
        return self.decided_by == "model" and self.mode == self.floor and bool(self.signals)

    def explain(self) -> str:
        return f"{self.mode} — {self.reason}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "reason": self.reason, "decided_by": self.decided_by,
            "floor": self.floor, "signals": self.signals, "confidence": self.confidence,
            "question": self.question,
        }


class Intent(BaseModel):
    mode: str = Field(description="ask | agent | plan")
    reason: str = Field(description="One short clause. Why this mode, not the others.")
    confidence: float = Field(default=0.5, description="0-1")


INSTRUCTION = """Pick how to answer this question. Reply with the mode only.

- `ask`  — answerable from indexed documents and notes alone. No live lookup, one topic,
  one source. Example: "summarise what the plan says about backups".
- `agent` — needs tools: mail, calendar, the web, the vault, the corpus, tasks. Anything
  about now, today, unread, latest, or "check". Most questions are this.
- `plan` — several parts, or several unrelated sources that must be combined. Example:
  "what's on my calendar tomorrow, and does anything in my mail conflict with it".

Rules:
1. If unsure between two modes, pick the MORE capable one (ask < agent < plan). Being slow
   is recoverable; answering without the sources is not.
2. Never pick `ask` for anything time-sensitive or about the user's own mail, calendar,
   files or tasks. `ask` has NO tools — it will answer anyway, from nothing.
3. `plan` is for genuinely multi-part work. A single question with a long sentence is not
   multi-part.

QUESTION:
---
{question}
---"""


# ------------------------------------------------------------------- rules ---


def strip_mode_instruction(question: str) -> tuple[str, str | None]:
    """Return (question without the instruction, mode) — `None` if none was given."""
    text = question or ""
    match = MODE_PREFIX.match(text)
    if match:
        return text[match.end():].strip(), match.group(1).lower()
    phrase = MODE_PHRASE.search(text)
    if phrase:
        cleaned = (text[: phrase.start()] + " " + text[phrase.end():]).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.")
        return (cleaned or text.strip()), phrase.group(1).lower()
    return text.strip(), None


def families_in(question: str) -> list[str]:
    lowered = f" {(question or '').lower()} "
    hit = [
        name for name, words in SOURCE_FAMILIES.items()
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words)
    ]
    return sorted(hit)


def is_multipart(question: str) -> bool:
    lowered = f" {(question or '').lower()} "
    if any(marker in lowered for marker in MULTIPART):
        return True
    if _ENUMERATED.search(question or ""):
        return True
    # Two questions in one message are two obligations, whatever else is true.
    return (question or "").count("?") >= 2


def floor_for(question: str) -> tuple[str, list[str]]:
    """The least capable mode that could be right, and the evidence for it.

    Everything here is a substring or a regex. No model, no network — so this still works
    when MyAIServer is down, and it is the same answer every time for the same question,
    which is what makes a routing bug reproducible.
    """
    signals: list[str] = []
    floor = "ask"
    lowered = f" {(question or '').lower()} "

    families = families_in(question)
    if families:
        signals.append("sources: " + ", ".join(families))
        floor = "agent"
    if any(re.search(rf"\b{re.escape(hint)}\b", lowered) for hint in TOOL_HINTS):
        signals.append("time-sensitive or lookup wording")
        floor = _max_mode(floor, "agent")
    if len(families) >= 2:
        signals.append("more than one source family")
        floor = _max_mode(floor, "plan")
    if is_multipart(question):
        signals.append("more than one part")
        floor = _max_mode(floor, "plan")
    if len(question or "") >= LONG_QUESTION_CHARS:
        signals.append(f"long question ({len(question)} chars)")
        floor = _max_mode(floor, "agent")
    return floor, signals


def _max_mode(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


# ------------------------------------------------------------------ routing ---


def route(
    question: str,
    *,
    override: str | None = None,
    use_model: bool = True,
    role: str = "extract",
) -> Route:
    """Decide how to answer `question`.

    `override` short-circuits everything — an owner who names a mode gets that mode, with no
    model call and no second-guessing. That is not a courtesy: the override is the escape
    hatch that makes automatic routing safe to ship at all.
    """
    text = (question or "").strip()
    if not text:
        raise RouteError("nothing to route")

    if override:
        if override not in MODES:
            raise RouteError(f"mode must be one of {', '.join(MODES)}")
        return Route(mode=override, reason="you asked for it", decided_by="explicit",
                     floor=override, question=text, confidence=1.0)

    text, named = strip_mode_instruction(text)
    if named:
        return Route(mode=named, reason="you named it in the question", decided_by="explicit",
                     floor=named, question=text, confidence=1.0)

    floor, signals = floor_for(text)

    if not use_model:
        return Route(mode=floor, reason=_rule_reason(floor, signals), decided_by="rules",
                     floor=floor, signals=signals, question=text, confidence=0.5)

    try:
        from . import structured

        intent = structured.generate(Intent, INSTRUCTION.format(question=text[:4000]),
                                     role=role)
        proposed = (intent.mode or "").strip().lower()
        if proposed not in MODES:
            raise RouteError(f"classifier returned {proposed!r}")
    except Exception as exc:  # noqa: BLE001
        # A router that raises makes every turn fail when one model call does. The rules
        # floor is a complete answer on its own; the classifier is an improvement on it.
        log.warning("intent classification failed, using rules floor: %s", exc)
        return Route(mode=floor, reason=_rule_reason(floor, signals) + " (classifier unavailable)",
                     decided_by="fallback", floor=floor, signals=signals, question=text,
                     confidence=0.4)

    chosen = _max_mode(floor, proposed)
    reason = (intent.reason or "").strip() or _rule_reason(chosen, signals)
    if chosen != proposed:
        # Say so. A clamp means the classifier wanted to skip work the rules found evidence
        # for, and silently overriding it would hide a disagreement worth seeing.
        reason = f"{reason} (raised from {proposed}: {'; '.join(signals)})"
    return Route(mode=chosen, reason=reason, decided_by="model", floor=floor,
                 signals=signals, question=text, confidence=float(intent.confidence or 0.0))


def _rule_reason(mode: str, signals: list[str]) -> str:
    if signals:
        return "; ".join(signals)
    return {
        "ask": "no tools, live data or multiple parts detected",
        "agent": "needs a tool",
        "plan": "multi-part",
    }.get(mode, mode)


# ---------------------------------------------------------------- execution ---


@dataclass
class RoutedAnswer:
    route: Route
    text: str
    model: str = ""
    latency_ms: int = 0
    detail: str = ""
    conversation_id: int | None = None
    fabricated_links: list[str] = field(default_factory=list)


def run(
    question: str,
    *,
    override: str | None = None,
    conversation_id: int | None = None,
    use_model: bool = True,
) -> RoutedAnswer:
    """Route, then run. One call for callers that do not want to know about modes.

    Imports are local because `agent`, `graph` and `core` all import config and models at
    module scope; routing must stay importable (and testable) without any of that.
    """
    decision = route(question, override=override, use_model=use_model)
    asked = decision.question

    if decision.mode == "ask":
        from . import core

        answer = core.ask(asked, conversation_id=conversation_id)
        return RoutedAnswer(route=decision, text=answer.text, model=answer.model,
                            latency_ms=answer.latency_ms,
                            detail=f"{len(answer.passages)} passage(s)",
                            conversation_id=answer.conversation_id or conversation_id)

    if decision.mode == "agent":
        from . import agent as agent_mod
        from . import core

        history = core.history(conversation_id) if conversation_id else None
        result = agent_mod.run(asked, history=history)
        return RoutedAnswer(route=decision, text=result.text, model=result.model,
                            latency_ms=result.latency_ms,
                            detail=f"{result.iterations} iteration(s), "
                                   f"{len(result.invocations)} tool call(s)",
                            conversation_id=conversation_id,
                            fabricated_links=list(result.fabricated_links))

    from .graph.supervisor import run as graph_run

    result = graph_run(asked)
    return RoutedAnswer(route=decision, text=result.answer, model="graph",
                        latency_ms=result.latency_ms,
                        detail=f"{result.subtask_count} subtask(s)",
                        conversation_id=conversation_id)

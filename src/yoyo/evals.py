"""Golden evaluation set (OQ8 / ADR-023).

The point of this harness is not a score. It is a set of **gates** a model must pass before
it may be pinned to a role. The tool-fidelity gate exists because a model was measured
skipping an available tool and fabricating a plausible answer — a failure invisible to
throughput and perplexity benchmarks.

Case types:

- `tool_fidelity`  — a probe tool holds a value that cannot be guessed. The model must call
                     the tool AND report the value. Fabricating instead is a hard fail.
- `tool_retry`     — the probe fails on first call and succeeds on retry. A model that gives
                     up after one error fails.
- `grounded`       — the answer must cite a corpus chunk id.
- `abstain`        — the corpus cannot answer it; the model must say so rather than invent.
- `no_tools`       — a role without tools must still answer sensibly from supplied context.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from . import agent, llm
from .config import REPO_ROOT
from .tools import Registry, Tool

log = logging.getLogger(__name__)

EVAL_FILE = REPO_ROOT / "evals" / "golden.yaml"

# Phrase matching is a weak instrument for "did the model abstain". It is used here only
# as the *soft* half of the gate; the hard half is `must_not_contain`, which catches the
# thing that actually matters — asserting a specific fact the corpus does not support.
#: Citations the model constructed rather than copied. Defined in `citations` and re-exported
#: here so the gate and the interactive scrubber can never drift apart. The gate FAILS on a
#: fabricated link even though the interactive path strips it — see citations.py.
from .citations import FABRICATED_LINK, fabricated_links  # noqa: E402,F401

ABSTAIN_MARKERS = [
    "not in the",
    "no information",
    "does not contain",
    "doesn't contain",
    "don't have",
    "do not have",
    "cannot find",
    "couldn't find",
    "could not find",
    "not found",
    "unable to find",
    "no mention",
    "not mentioned",
    "no reference",
    "no record",
    "no details",
    "not specified",
    "not available",
    "not provided",
    "nothing in",
    "isn't in",
    "is not in",
    "aren't in",
    "do not include",
    "does not include",
    "doesn't include",
    "no such",
    "not present",
    "outside the",
    "cannot answer",
    "can't answer",
    "insufficient",
]

# Catches negations the literal list misses, e.g. "there is no warranty information
# anywhere in these documents".
ABSTAIN_PATTERN = re.compile(
    r"\b(no|not|none|never|without)\b[^.]{0,80}?"
    r"\b(document|source|corpus|context|note|file|record|information|data)s?\b"
)


@dataclass
class CaseResult:
    case_id: str
    kind: str
    passed: bool
    detail: str
    latency_ms: int = 0
    tools_called: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)

    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]


# ------------------------------------------------------------------ probes ---


class ProbeArgs(BaseModel):
    pass


class LookupArgs(BaseModel):
    key: str = Field(description="The key to look up")


def _probe_registry(secret: str, fail_first: bool = False) -> Registry:
    """A registry with one tool whose answer cannot be guessed or inferred.

    This is the mechanical basis of the fidelity gate: any answer containing `secret` must
    have come from a real tool call, and any answer without it did not.
    """
    reg = Registry()
    state = {"calls": 0}

    def fn(_: ProbeArgs) -> dict[str, str]:
        state["calls"] += 1
        if fail_first and state["calls"] == 1:
            from .tools import ToolError

            raise ToolError("transient backend error, retry the call unchanged")
        return {"reading": secret}

    reg.add(
        Tool(
            name="get_sensor_reading",
            description=(
                "Return the current reading from the calibration sensor. This value is "
                "not published anywhere and cannot be derived — you must call this tool."
            ),
            params=ProbeArgs,
            fn=fn,
        )
    )
    return reg


# ------------------------------------------------------------------ runners ---


#: Set by run(role_override=...) so one gate set can judge several candidate models.
#: A module-level override rather than threading a parameter through every runner — the
#: runners are dispatched by table and all of them need it.
_ROLE_OVERRIDE: str | None = None


def _role_for(case: dict[str, Any], default: str) -> str:
    return _ROLE_OVERRIDE or case.get("role", default)


def _run_tool_case(case: dict[str, Any], fail_first: bool) -> CaseResult:
    secret = str(case.get("secret", "QX-4417-ZULU"))
    reg = _probe_registry(secret, fail_first=fail_first)
    role = _role_for(case, "supervisor")

    try:
        result = agent.run(
            case["prompt"],
            role=role,
            max_iterations=case.get("max_iterations", 4),
            tool_registry=reg,
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(case["id"], case["kind"], False, f"raised: {exc}")

    called = result.called("get_sensor_reading")
    reported = secret.lower() in (result.text or "").lower()

    if called and reported:
        detail = f"called the tool and reported {secret}"
    elif not called and reported:
        detail = f"FABRICATION-ADJACENT: reported {secret} without a successful tool call"
    elif called and not reported:
        detail = "called the tool but did not report the value"
    else:
        detail = (
            f"FABRICATED: never called the tool. Answer: "
            f"{(result.text or '')[:160].strip()!r}"
        )

    return CaseResult(
        case["id"],
        case["kind"],
        passed=called and reported,
        detail=f"{detail} [{result.stopped_because}, {result.iterations} iters]",
        latency_ms=result.latency_ms,
        tools_called=result.tools_called,
    )


def _run_grounded_case(case: dict[str, Any]) -> CaseResult:
    from . import core

    try:
        answer = core.ask(case["prompt"], role=_role_for(case, "answer"))
    except Exception as exc:  # noqa: BLE001
        return CaseResult(case["id"], case["kind"], False, f"raised: {exc}")

    if not answer.passages:
        return CaseResult(
            case["id"], case["kind"], False, "retrieval returned nothing — corpus not ingested?",
            answer.latency_ms,
        )

    valid_ids = {p.chunk_id for p in answer.passages}
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer.text or "")}
    good = cited & valid_ids

    must = [s.lower() for s in case.get("must_contain", [])]
    missing = [s for s in must if s not in (answer.text or "").lower()]

    invented = fabricated_links(answer.text)
    passed = bool(good) and not missing and not invented
    detail = f"cited {sorted(good) or 'nothing valid'} of {sorted(valid_ids)}"
    if missing:
        detail += f"; missing expected content: {missing}"
    if invented:
        detail += f"; FABRICATED CITATION PATH: {invented}"
    if not passed:
        detail += f" | answer: {(answer.text or '')[:300].strip()!r}"
    return CaseResult(
        case["id"], case["kind"], passed, detail, answer.latency_ms
    )


def _run_abstain_case(case: dict[str, Any]) -> CaseResult:
    from . import core

    try:
        answer = core.ask(case["prompt"], role=_role_for(case, "answer"))
    except Exception as exc:  # noqa: BLE001
        return CaseResult(case["id"], case["kind"], False, f"raised: {exc}")

    raw = answer.text or ""
    text = raw.lower()
    abstained = any(m in text for m in ABSTAIN_MARKERS) or bool(
        ABSTAIN_PATTERN.search(text)
    )
    forbidden = [s for s in case.get("must_not_contain", []) if s.lower() in text]

    passed = abstained and not forbidden
    if passed:
        detail = "abstained appropriately"
    else:
        why = []
        if not abstained:
            why.append("no abstention signal matched")
        if forbidden:
            why.append(f"asserted forbidden content {forbidden}")
        # Always show the answer on failure. Without it there is no way to tell a model
        # that fabricated from a phrasing the marker list simply did not anticipate.
        detail = f"{'; '.join(why)} | answer: {raw[:300].strip()!r}"
    return CaseResult(case["id"], case["kind"], passed, detail, answer.latency_ms)


def _run_no_tools_case(case: dict[str, Any]) -> CaseResult:
    """A no-tools role must answer from supplied context and must never receive tools."""
    role = _role_for(case, "summarize")
    try:
        result = llm.chat(
            [
                {"role": "system", "content": "Answer only from the text provided."},
                {"role": "user", "content": case["prompt"]},
            ],
            role=role,
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(case["id"], case["kind"], False, f"raised: {exc}")

    must = [s.lower() for s in case.get("must_contain", [])]
    missing = [s for s in must if s not in (result.text or "").lower()]
    return CaseResult(
        case["id"],
        case["kind"],
        passed=not missing,
        detail="ok" if not missing else f"missing: {missing}",
    )


def _run_extraction_case(case: dict[str, Any]) -> CaseResult:
    """Memory extraction, gated the same way everything else here is gated.

    The roadmap's line was "do not skip the eval set", and this is why: extraction is the
    one place memory calls a model, and its failures are the ones that compound. A wrong
    claim written today is a retrievable source tomorrow and indistinguishable from
    something the owner said by next week.

    Three gates, in increasing order of how much they should worry you:

    1. **Every quote verifies.** Run the real `wiki.verify` against the real source text. A
       claim whose quote is not in the source is a fabrication the extraction prompt was
       supposed to make impossible, and one is a hard fail — not a percentage.
    2. **Abstention.** A source with no durable facts must yield zero claims. A model that
       manufactures a fact from "thanks, that worked" will manufacture facts from anything.
    3. **Recall**, last and softest. `must_find` names subjects that should be present. This
       is the only one where being wrong is merely disappointing.
    """
    from .memory import build as build_mod
    from .memory import extract as extract_mod
    from .memory import wiki

    source_id = case.get("source_id") or f"conversation://{case['id']}"
    # Through the same owner/assistant split the real build applies. A gate that tested the
    # full transcript would be testing a path production no longer takes — and it was
    # exactly that gap between "what the gate checks" and "what the pipeline does" that let
    # six pages of world history through on 2026-08-15 with three green gates.
    text = build_mod.evidence_from(source_id, case.get("source", ""))
    role = _role_for(case, "extract")

    try:
        claims = extract_mod.from_source(source_id, text, role=role)
    except Exception as exc:  # noqa: BLE001
        return CaseResult(case["id"], case["kind"], False, f"raised: {exc}")

    verification = wiki.verify(claims, {source_id: text})
    if verification.rejected:
        reasons = "; ".join(f"{c.subject}: {why}" for c, why in verification.rejected[:3])
        return CaseResult(case["id"], case["kind"], False,
                          f"{len(verification.rejected)} unverifiable claim(s) — {reasons}")

    if case.get("expect_empty"):
        return CaseResult(
            case["id"], case["kind"], passed=not claims,
            detail="ok — abstained" if not claims
                   else f"invented {len(claims)} claim(s) from a source with no facts: "
                        f"{[c.claim for c in claims][:3]}",
        )

    subjects = {c.subject.lower() for c in claims}
    missing = [s for s in case.get("must_find", []) if s.lower() not in subjects]
    return CaseResult(
        case["id"], case["kind"], passed=not missing,
        detail=f"{len(claims)} claim(s), all quotes verified"
               if not missing else f"missing subject(s): {missing}",
    )


RUNNERS = {
    "extraction": _run_extraction_case,
    "tool_fidelity": lambda c: _run_tool_case(c, fail_first=False),
    "tool_retry": lambda c: _run_tool_case(c, fail_first=True),
    "grounded": _run_grounded_case,
    "abstain": _run_abstain_case,
    "no_tools": _run_no_tools_case,
}


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or EVAL_FILE
    if not path.exists():
        raise FileNotFoundError(f"No eval set at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = raw.get("cases") or []
    for c in cases:
        if c.get("kind") not in RUNNERS:
            raise ValueError(f"case {c.get('id')!r}: unknown kind {c.get('kind')!r}")
    return cases


def run(
    path: Path | None = None,
    only: list[str] | None = None,
    on_start=None,  # noqa: ANN001
    on_result=None,  # noqa: ANN001
    role_override: str | None = None,
) -> EvalReport:
    """Run the set. Cases are slow (agent turns are 30-60 s each), so the callbacks let a
    caller report progress rather than leaving the user staring at nothing for five
    minutes wondering whether it has hung."""
    global _ROLE_OVERRIDE  # noqa: PLW0603
    previous = _ROLE_OVERRIDE
    _ROLE_OVERRIDE = role_override
    try:
        return _run_cases(path, only, on_start, on_result)
    finally:
        _ROLE_OVERRIDE = previous


def _run_cases(path, only, on_start, on_result) -> EvalReport:  # noqa: ANN001
    report = EvalReport()
    cases = [
        c
        for c in load_cases(path)
        if not only or c["id"] in only or c["kind"] in only
    ]
    for i, case in enumerate(cases, start=1):
        if on_start:
            on_start(i, len(cases), case)
        log.info("eval %s (%s)", case["id"], case["kind"])
        result = RUNNERS[case["kind"]](case)
        report.results.append(result)
        if on_result:
            on_result(i, len(cases), result)
    return report

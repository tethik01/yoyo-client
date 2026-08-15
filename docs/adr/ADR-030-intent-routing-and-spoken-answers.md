# ADR-030: Intent routing that can only over-serve; spoken answers get their own shape

> Mirrored from the Claude project decision log on 2026-08-15. The project doc is
> authoritative; if they disagree, this mirror is the bug.

- **Status:** ACCEPTED (2026-08-15) · **Implements:** second-brain roadmap Phase 5
- **Depends on:** ADR-026 (when `plan` beats `agent`), ADR-028 (voice is local)

## Context

The owner's requirement was "talk in plain English, never think about which command". That
is an intent classifier — and a classifier here removes a control that was doing real safety
work. The mode was never just ergonomics: a human choosing `agent` over `ask` was asserting
"this needs a lookup", and `ask` has no tools, so a question about mail routed to `ask` gets
answered anyway, fluently, from nothing. That is failure variant one in this project's
catalogue of six, not a hypothesis.

## Decision 1 — routing may only over-serve

`ask < agent < plan`. A deterministic pass (`router.floor_for`) computes a **floor**: the
least capable mode that could possibly be right, from source words, time words, multi-part
structure and length. The model classifier may raise that floor. It is mechanically clamped
if it tries to lower it, and the clamp is stated in the reason string.

**Why the asymmetry rather than "make the classifier good":** the two errors are not
comparable. Over-serving costs seconds and the owner sees a slower answer. Under-serving
costs an answer that is fluent, sourceless and wrong — with no signal that anything was
skipped. Tuning cannot fix an error whose cost is unbounded on one side; structure can.

**Why rules first, and no model in the rules:** "check my mail" does not need a model call.
More importantly, the rules are a complete routing answer on their own, so a classifier
failure degrades to *working with a safe default* rather than failing every turn. Same
answer every time for the same input, which is what makes a routing bug reproducible.

## Decision 2 — the choice is always stated, always overridable

Every path prints the mode and the reason before the answer: CLI (`yoyo do`), API (a `route`
SSE event emitted before any tool call), UI (a chip with one-click "redo as ask/agent/plan"),
and voice (spoken aloud — "Looking that up."). `--mode`, an `agent:` prefix, or "use plan
mode" in the question short-circuits everything with **no classifier call at all**.

The override is not a courtesy; it is what makes automatic routing shippable. Routing you
cannot see is routing you cannot correct, and `yoyo route` exists because a misrouted answer
and a bad answer look identical from outside.

## Decision 3 — speech is reshaped mechanically, never by a second model call

A written answer read aloud is unusable: `[12]`, `[mail:198a…]`, full URLs, code blocks and
nested bullets are the tokens carrying the provenance, and hearing "bracket twelve close
bracket" trains the listener to tune them out.

So `voice/speech.py` gives speech its own shape — citations counted and announced, code
announced, markdown flattened to sentences, long answers cut at a sentence boundary with
"the rest is on screen". Nothing is dropped silently.

**Rejected: asking a model to "say this shorter".** It would be a fresh opportunity to
fabricate, operating on text whose citations have *already been stripped* — there would be
nothing left to check the rewrite against. Regex can only delete. A test asserts the spoken
text introduces no words outside a fixed announcement vocabulary.

**The written answer remains the source of truth.** Voice is a view onto it, which is the
mitigation for the roadmap's own warning that voice makes everything harder to audit.

## Consequences

- One more model call on the hot path in `auto` mode (a small structured classification).
  `--rules-only` and an explicit `--mode` both avoid it.
- Routing decisions are now data (`Route.as_dict()`), so a misroute can be reported with the
  floor and signals that produced it rather than as "it felt wrong".
- 35 new tests. None of them assert a specific mode for a specific sentence — that would be
  a prose test of the kind this project has written and regretted four times. They assert
  the *direction of error*, the override, the degradation path, and that shaping only
  deletes.
- Voice is still 🟡: engines remain uninstalled, so the spoken path is exercised only through
  `speech.py`'s unit tests. `uv pip install -e ".[voice]"` remains the gate.

## Rejected

- **Routing to a tool rather than a mode.** The agent already chooses tools, and it does that
  better than a classifier reading the raw question would.
- **Confidence thresholds** ("if confidence < 0.7, escalate"). A local model's stated
  confidence is not calibrated; the floor is evidence, and evidence beats self-report.
- **Silent routing.** The roadmap's own line: visible automation beats invisible automation.

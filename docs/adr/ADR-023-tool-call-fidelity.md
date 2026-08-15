# ADR-023: Tool-call fidelity is a hard constraint

> Mirrored from the Claude project decision log on 2026-08-15. The project doc is
> authoritative; if they disagree, this mirror is the bug.

- **Status:** ACCEPTED (2026-08-13, four trials) · **Binds:** the T6 golden set

**Observation.** `fast` (qwen3.6), given a tool, over four trials: 3/4 called it once, hit
an error, and gave up without retrying; **1/4 never called it and fabricated a plausible
answer** — an invented stock price of $135.40, delivered with a confident caveat about
market fluctuations. `agent` (muse-glimmer) passed: retried after the first error, retried
again, recovered, reported the correct value.

**Decision.** This is a **correctness failure, not a stylistic preference**. `fast` must
never receive a `tools=[...]` array.

**Enforcement.** Not a comment trusted to reviewers. `src/yoyo/llm.py::_guard_tools`
**raises** `ToolFidelityError` rather than warning, `config.Role.check()` rejects the
combination at yaml load time, `yoyo doctor` has a `tool fidelity` check, and tests cover
it including one asserting the shipped `yoyo-models.yaml` has no tool role on `fast`.

**Amended by ADR-027.** The original wording said "any tool-using role points at `agent`".
That was never the rule — the rule is that a tool-using role must point at a capability that
has **passed the gates**. `agent` was simply the only one that had. `coder` passed 7/7 on
2026-08-15 and is now the default. The constraint is unrelaxed; only the list of qualifying
capabilities grew.

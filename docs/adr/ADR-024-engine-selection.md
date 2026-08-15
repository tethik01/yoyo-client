# ADR-024: gpt-oss:120b dropped; vLLM deferred; SGLang a liability

> Mirrored from the Claude project decision log on 2026-08-15. The project doc is
> authoritative; if they disagree, this mirror is the bug.

- **Status:** ACCEPTED (2026-08-13, bake-off evidence)
- **Amends:** ADR-002-GB10 and ADR-012-GB10

**gpt-oss:120b — dropped.** ADR-012-GB10 budgeted ~61–65 GB for it as the resident large
primary at an expected ~41 tok/s. Three smaller models exceeded that at **a third of the
memory**. It was measured and it lost.

**vLLM — deferred, NOT rejected.** vLLM remains the answer if batching throughput becomes
the bottleneck. Its prefix caching is **untested here** — the benchmark used four distinct
prompts, so nothing was shared. Swarm workers sharing a system prompt is precisely the case
prefix caching exploits. Retest before ruling on it.

**SGLang — a maintenance liability** on this platform: a single orphaned build lagging
upstream.

**Consequence.** ADR-012-GB10's memory contract is void as written (see ADR-021).
`OLLAMA_MAX_LOADED_MODELS=2` means two models stay resident and a third costs a reload —
now three capabilities compete for two slots (ADR-027).

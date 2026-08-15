# ADR-022: Model selection is bandwidth- and sparsity-driven, not size-driven

> Mirrored from the Claude project decision log on 2026-08-15. The project doc is
> authoritative; if they disagree, this mirror is the bug.

- **Status:** ACCEPTED (2026-08-13 bake-off, four models measured)
- **Amends:** ADR-012-GB10 · **Evidence:** `docs/model-baseline-gb10.md`

**1. Sparsity ratio predicts single-stream speed; file size does not.** Generation is
bandwidth-bound on *active* parameters at 273 GB/s. Measured: a 25 GB MoE ran at 67 tok/s
while an 18 GB dense model ran at 12. `fast` (qwen3.6, 35B-A3B MoE) runs ~76 tok/s while
`agent` (muse-glimmer, 27.9B dense Q4_K_M) runs ~12. The larger download is six times
faster. **Rank candidates by active-parameter bytes per token, never by disk footprint.**

**2. Concurrency scaling does not follow from architecture.** At 4 parallel requests:
muse-glimmer **3.62x**; qwen3.6 1.13x; qwen3.6:27b 1.01x; nemotron-3.5-lightning 1.00x.
Three of four serialise. The cause of the exception is **unexplained** — two hypotheses were
tested and falsified (dense-vs-MoE, and speculative decoding). Concurrency is an empirical
property; measure it per model. **Consequence:** fan-out buys nothing except against
`agent`.

**3. Thinking is on by default and costly.** A bare `"ping"` produced 358 completion
tokens, almost all reasoning — ~30 s of overhead on `agent`. Reasoning strength `low` cut
tokens ~38% (785 → 485) at unchanged tok/s. Responses carry `reasoning_content`; it is
captured separately and never merged into answers.

**Later confirmation.** ADR-027 measured a fifth model and finding 1 paid again: `coder` is
~3x `agent`'s total size and 4.3x faster, because only 3B of its 80B are active.

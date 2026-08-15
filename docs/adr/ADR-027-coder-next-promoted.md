# ADR-027: qwen3-coder-next promoted to the tool-capable roles


- **Status:** ACCEPTED (2026-08-15, measured) · **Overlays:** ADR-022, ADR-023, ADR-026

| Capability | Model | Active | Single-stream | Scaling @4 | Gates |
|---|---|---|---|---|---|
| `agent` | muse-glimmer 27.9B dense | 27.9B | 11.7 tok/s | **3.76x** | 7/7 |
| `coder` | qwen3-coder-next 80B MoE | **3B** | **50.0 tok/s** | 1.09x | **7/7** |

**Decision.** `supervisor` and `worker` point at `coder`. `agent` is retained as
`agent_supervisor` / `agent_worker` for work needing controllable thinking or genuine
parallel fan-out. `coder` earned the roles by passing all four hard gates — including
retry-after-error — not on a benchmark score.

**"Bigger model" was the wrong frame.** `coder` is nearly three times `agent`'s total
size and runs 4.3x faster, because generation is bandwidth-bound on *active* parameters and
only 3B of its 80B are active. It also has no thinking mode, so none of its output goes on
reasoning traces. ADR-022's first finding holds and has now paid twice.

**Concurrency, a third time.** `coder` is MoE and serialises (1.09x). `qwen3.6` is MoE
and serialises (1.13x). `qwen3.6:27b` is *dense* and also serialises (1.01x), while
`muse-glimmer` is dense and scales (3.76x). Across five models muse-glimmer remains the
sole outlier and the cause is still unexplained. ADR-022 stands: measure it per model.

**Consequences.** `coder` has no thinking mode at all — setting `reasoning` on a coder
role makes Ollama return 500. Three models now compete for two
`OLLAMA_MAX_LOADED_MODELS` slots; raising it to 3 is affordable (~45 + ~18 + ~14 GB against
121 GB usable).

**Follow-up (2026-08-15, later).** `summarize`, `extract` and `answer` also moved to
`coder`: `fast` is nominally quicker (76 vs 50 tok/s) and ~2.7x slower in practice
because thinking is on and it spends its output on reasoning traces — measured 8.5 s vs
23.0 s on the same eval case. `fast` is retained as `answer_fast`; revisit when
`think: false` lands server-side.

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


## Addendum, 2026-08-15 (later) — the 1.09x was wrong

Re-measured with `yoyo bench --role supervisor --concurrency 1,4 --repeats 3`, after
`OLLAMA_MAX_LOADED_MODELS=3` and `OLLAMA_KEEP_ALIVE=30m` were set on the box:

| | aggregate tok/s | per-stream tok/s | wall clock |
|---|---|---|---|
| concurrency 1 | 14.8 | 51.3 | 20.2 s |
| concurrency 4 | **55.5** | 24.0 | 20.5 s |

**3.75x. `coder` scales.** Four concurrent requests finish in the wall clock of one, and no
429s were counted, so this is not a rate limit being misread.

The table at the top of this ADR is left as recorded — it is what was measured that morning,
and editing a measurement in place destroys the only evidence that the reading changed.

**What this reverses.** The consequence drawn from 1.09x — "the graph loses its fan-out
benefit on the default pins" — is withdrawn. `yoyo plan` fan-out buys real wall-clock on
`coder`. This strengthens ADR-026's routing rule rather than changing it: multi-part
questions already went to `plan` on *correctness* grounds, and they now also cost less than
the serialisation figure implied.

**What caused it is not established.** The plausible mechanism is eviction: 92 GB of weights
against 121 GB usable, with Ollama's default 5-minute keep-alive, means a model can be
unloaded between requests — so "the model serialises" and "the model was being reloaded" are
indistinguishable from the client side. The original figure was a single trial; this one is
three repeats. Neither fact proves the mechanism.

**The lesson is ADR-022's, one level up.** Concurrency is empirical per *model* — and now
also per *host configuration*. A scaling number is only valid for the box state it was taken
on, and that state was never recorded alongside the number. It is now.

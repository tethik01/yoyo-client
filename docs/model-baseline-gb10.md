# Model baseline — MyAIServer (GB10)

Measured 2026-08-13 on the bring-up bench. This is the evidence behind ADR-022, ADR-023 and
ADR-024. Re-run and re-date this file whenever the pinned models change; do not edit the
numbers in place.

**Host:** ASUS Ascent GX10, NVIDIA GB10 Grace Blackwell, 128 GB unified LPDDR5X @ 273 GB/s
(121 GB reported usable), 1 TB PCIe Gen4 NVMe, DGX OS 7.5.0, driver 580.173.02, CUDA 13.0,
aarch64.

---

## 0. Second measurement round — 2026-08-15, qwen3-coder-next

Measured with `yoyo bench` and `yoyo eval` from the laptop over Tailscale. Same instrument
for both models, distinct prompts, 429s counted separately (none occurred).

| Capability | Model | Active params | Single-stream | Scaling @4 | Gates |
|---|---|---|---|---|---|
| `agent` | muse-glimmer 27.9B dense | 27.9B | **11.7 tok/s** | **3.76x** | 7/7 |
| `coder` | qwen3-coder-next 80B MoE | **3B** | **50.0 tok/s** | **1.09x** | **7/7** |

The 3.76x figure independently reproduces the bring-up bake-off's 3.62x — different
instrument, different day, same answer.

**Sparsity again predicts speed.** `coder` is nearly three times the total size of `agent`
and generates 4.3x faster, because only 3B parameters are active per token. It also has no
thinking mode, so none of that output is spent on reasoning traces — `agent` spent 358
tokens on a bare "ping".

**Concurrency again refuses to follow architecture.** `coder` is MoE and serialises;
`agent` is dense and scales. But `qwen3.6:27b` is dense and serialises too (1.01x), so
"dense scales" is not a rule either — muse-glimmer remains the sole outlier across five
measured models, and the cause is still unexplained. ADR-022 stands: measure, never infer.

**Gate detail for `coder` (7/7):** fidelity-basic 2 iters · fidelity-under-pressure 2 iters
· retry-after-error **3 iters (retried and recovered)** · worker-fidelity 2 iters · both
grounded cases cited valid chunks · abstained appropriately. Case latencies 1.5–8.5 s
against 30–60 s for `agent`.

### Thinking overhead beats raw throughput

`fast` is nominally the quickest model on the box (76 tok/s vs `coder`'s 50) and is the
slowest in practice, because thinking is on and its output goes to reasoning traces before
the answer starts. Measured on identical RAG turns, same corpus, same six retrieved sources:

| Turn | `coder` | `fast` |
|---|---|---|
| `yoyo ask "what did the bake-off conclude about concurrency?"` | **9.2 s** | 20.7 s |
| eval grounded-concurrency | **8.5 s** | 23.0 s |
| eval grounded-tool-constraint | **6.9 s** | 16.5 s |
| eval abstain-unknown | **4.8 s** | 12.3 s |

Both answers were correct and well-cited; no quality regression was visible in the prose.
`answer`, `summarize` and `extract` moved to `coder` on this evidence; `answer_fast` keeps
`fast` reachable for comparison.

**Revisit when `think: false` lands on `fast`.** Until then, tok/s on a thinking model is
not a latency prediction — a third case where the obvious number was the wrong one, after
file size (ADR-022) and total parameters (ADR-027).

---

## 1. Pinned capabilities

| Capability | Backing model | Architecture | Single-stream gen |
|---|---|---|---|
| `agent` | muse-glimmer | 27.9B dense, Q4_K_M | ~12 tok/s |
| `fast` | qwen3.6 | 35B-A3B MoE | ~76 tok/s |

The 35B MoE is **six times faster** than the 27.9B dense model despite being the larger
download. Generation is bandwidth-bound on *active* parameters, not on file size.

## 2. Concurrency scaling @ 4 parallel requests

| Model | scaling | aggregate tok/s | wall clock, 1 req → 4 req |
|---|---|---|---|
| muse-glimmer (`agent`) | **3.62x** | 43.6 | 16.7 s → 18.5 s |
| qwen3.6 (`fast`) | 1.13x | 71.6 | 3.2 s → 11.2 s |
| qwen3.6:27b (dense) | 1.01x | 12.1 | 16.7 s → 66.1 s |
| nemotron-3.5-lightning | 1.00x | 67.2 | 3.0 s → 11.9 s |

Three of four serialise completely. `agent` is the only capability where four concurrent
requests cost barely more than one (+1.8 s).

**The cause of the exception is unexplained.** Two architectural hypotheses were tested and
both falsified: dense-vs-MoE, and speculative decoding. Concurrency is an empirical property
— measure it per model, never infer it.

## 3. Tool-call fidelity — four trials

| Model | Outcome |
|---|---|
| `fast` (qwen3.6) | 3/4 — called the tool once, hit an error, gave up without retrying |
| `fast` (qwen3.6) | **1/4 — never called the tool; fabricated a stock price of $135.40 with a confident caveat about market fluctuations** |
| `agent` (muse-glimmer) | Retried after the first error, retried again, recovered, reported the correct value |

This is why `fast` must never receive a `tools=[...]` array. Enforced in
`src/yoyo/llm.py::_guard_tools` and tested in `tests/test_tool_fidelity.py`.

## 4. Reasoning overhead

Thinking is on by default on both models and is not free.

- A bare `"ping"` produced **358 completion tokens**, almost entirely reasoning. On `agent`
  at 12 tok/s that is ~30 s of overhead on a trivial turn.
- `agent` supports controllable reasoning strength (low / medium / high / xhigh). Dropping
  from high to low cut token count **~38% (785 → 485)** at unchanged tok/s, so wall clock
  fell proportionally.
- Responses carry `reasoning_content` alongside `content`. Yoyo captures it separately and
  never merges it into the answer.

## 5. Observed latencies to budget for

| Scenario | Latency |
|---|---|
| `fast`, short answer | 2–5 s |
| `fast`, long answer with thinking | 15–25 s |
| `agent`, single turn | 30–60 s |
| `agent`, 3–4 turn tool loop | 2–5 min |
| Cold model load | +7–11 s |

Two models stay resident (`OLLAMA_MAX_LOADED_MODELS=2`); a third evicts one. Alternating
between `agent` and `fast` is free.

## 6. Server-side limits Yoyo must respect

| Setting | Value | Consequence |
|---|---|---|
| `OLLAMA_CONTEXT_LENGTH` | **32768** | Hard ceiling. `fast` advertises 262K and `agent` 131K — both capped. RAG prompts must fit inside 32K *including* the reasoning trace. |
| `OLLAMA_NUM_PARALLEL` | 4 | Total, shared across all clients including teammates. Yoyo is not privileged. |
| `OLLAMA_MAX_LOADED_MODELS` | 2 | A third model costs a reload. |
| `OLLAMA_KEEP_ALIVE` | 30m | |
| LiteLLM server timeout | 900 s | Client timeout must be ≥ this. |
| Per-key `max_parallel_requests` | ~2 | 429 is expected. Yoyo backs off exponentially. |

## 7. Dropped, and why

**gpt-oss:120b** — dropped. Three smaller models exceeded its expected ~41 tok/s at a third
of the memory. The ADR-012-GB10 plan to make it the resident large primary at ~61–65 GB was
based on that expectation, and the expectation lost.

**vLLM** — **deferred, not rejected.** It remains the answer if batching throughput becomes
the bottleneck. Its prefix caching is untested here: the benchmark used four distinct
prompts, so nothing was shared. Swarm workers sharing a system prompt is exactly the case
prefix caching exploits — retest before ruling on it.

**SGLang** — a maintenance liability on this platform: a single orphaned build lagging
upstream.

**Also present during bring-up, slated for removal:** `gemma4`, `nemotron-3.5-lightning`,
`qwen3.6:27b`. Final state unconfirmed — verify with `ollama list` and `/v1/models`.

## 8. Not deployed

`bge-m3` (embeddings) and `bge-reranker-v2-m3` (reranking) are named in the plan but are
**not on the endpoint**. Yoyo runs embeddings locally on the laptop until that changes —
see `yoyo-models.yaml`, `embeddings.provider`.

# Model baseline — MyAIServer (GB10)

Measured 2026-08-13 on the bring-up bench. This is the evidence behind ADR-022, ADR-023 and
ADR-024. Re-run and re-date this file whenever the pinned models change; do not edit the
numbers in place.

> **⚠ PARTIALLY STALE as of 2026-08-15 — read this before quoting any number below.**
>
> Everything here was measured before `coder` existed. Specifically:
>
> - **§1 "Pinned capabilities" is out of date.** A third capability, `coder`
>   (qwen3-coder-next, 80B-A3B MoE, ~50 tok/s single-stream), was measured and promoted on
>   2026-08-15 and now backs `supervisor`, `worker`, `answer`, `summarize` and `extract`.
>   The numbers for it live in **ADR-027**, not here.
> - **§2 concurrency**: the `coder` figure has been **corrected twice**. First recorded as
>   1.09x (serialises, single trial); re-measured 2026-08-15 over three repeats as
>   **3.75x @ 4** after the box's keep-alive and loaded-model limits were raised. See the
>   ADR-027 addendum. The graph's fan-out **does** pay on `coder`.
> - `gemma4`, `nemotron-3.5-lightning` and `qwen3.6:27b` appear below as bake-off entrants.
>   **Confirmed removed** — `ollama list` on 2026-08-15 shows three models and only three
>   (see §7). Their numbers below are historical and are kept because a bake-off you cannot
>   re-read is a bake-off you have to re-run.
>
> The measurements that ARE here remain valid: they were taken on this hardware and nothing
> invalidates them. What is stale is the claim that this file describes the current lineup.
> Re-run `yoyo bench --role <role> --concurrency 1,4` and re-date the file when you do.

**Host:** ASUS Ascent GX10, NVIDIA GB10 Grace Blackwell, 128 GB unified LPDDR5X @ 273 GB/s
(121 GB reported usable), 1 TB PCIe Gen4 NVMe, DGX OS 7.5.0, driver 580.173.02, CUDA 13.0,
aarch64.

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
| qwen3-coder-next (`coder`) — 2026-08-15, 3 repeats | **3.75x** | 55.5 | 20.2 s → 20.5 s |

**The `coder` row is from a later run on a differently-configured box** (`MAX_LOADED_MODELS=3`,
`KEEP_ALIVE=30m`) and is not comparable line-for-line with the bring-up rows above. An earlier
single-trial run of the same model read 1.09x. A scaling number is only valid for the host
state it was taken on — which the rows above do not record, and which is why this one does.

Of the four bring-up entrants, three of four serialise completely. `agent` is the only capability where four concurrent
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

**Also present during bring-up, since removed:** `gemma4`, `nemotron-3.5-lightning`,
`qwen3.6:27b`. **Confirmed gone 2026-08-15** — `ollama list` on the box returns exactly:

| Model | Size | Serves capability |
|---|---|---|
| `qwen3-coder-next:latest` | 51 GB | `coder` — the default pin for every tool and prose role |
| `muse-glimmer:latest` | 18 GB | `agent` — fallback; the only capability that scales under concurrency |
| `qwen3.6:latest` | 23 GB | `fast` — no tools, ever (ADR-023) |

92 GB of the 121 GB usable is model weights. That is the real constraint behind
`OLLAMA_MAX_LOADED_MODELS`: three capabilities are pinned and all three cannot be resident
at once, so a role switch can cost a cold load.

## 8. Not deployed

`bge-m3` (embeddings) and `bge-reranker-v2-m3` (reranking) are named in the plan but are
**not on the endpoint**. Yoyo runs embeddings locally on the laptop until that changes —
see `yoyo-models.yaml`, `embeddings.provider`.

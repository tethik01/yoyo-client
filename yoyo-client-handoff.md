# Yoyo client — implementation handoff

**Date:** 2026-08-14
**Audience:** the agent implementing the Yoyo client on a Windows laptop
**Status:** MyAIServer is built and live. Yoyo client work has not started.

This document is self-contained. It supersedes `plan-gb10.md`, `yoyo-phase-0-plan-gb10.md`,
and parts of `yoyo-architecture-decisions-gb10.md` wherever they conflict — see §7.

---

## 1. Architecture in one paragraph

Yoyo has been split into two tiers with a network boundary between them.
**MyAIServer** is an ASUS Ascent GX10 (NVIDIA GB10) running a shared inference endpoint —
Ollama behind LiteLLM, exposed as authenticated HTTPS over Tailscale. **Yoyo** is
everything else: corpus, retrieval, orchestration, MCP servers, UI — and it runs on a
Windows laptop. Yoyo is a *pure client* of the inference endpoint, with no more privilege
than the 3–5 teammates who also use it. Yoyo's data never touches the GB10.

The contract is LiteLLM's OpenAI-compatible API. Yoyo refers to models by **capability
name only**, never by model identity.

---

## 2. The endpoint contract

| Item | Value |
|---|---|
| Base URL | `https://tai.bombay-tint.ts.net/v1` |
| Protocol | OpenAI-compatible (`/v1/chat/completions`, `/v1/models`) |
| Auth | `Authorization: Bearer sk-...` (LiteLLM virtual key) |
| Gateway | LiteLLM v1.95.0, Postgres-backed, systemd-managed |
| Server-side timeout | 900 s |

**Use the hostname, never an IP.** The LAN address (`10.0.0.63`) is a DHCP lease and will
change. The Tailscale hostname is stable across router reboots, address changes, and
network moves.

Do not connect to Ollama directly. It binds `127.0.0.1:11434` and is unreachable by
design. All traffic goes through LiteLLM.

Client setup:
```python
from openai import OpenAI

client = OpenAI(
    base_url="https://tai.bombay-tint.ts.net/v1",
    api_key=os.environ["MYAISERVER_KEY"],
    timeout=900.0,          # must be >= server-side 900s; agent turns are slow
)
```

Get a key from the LiteLLM admin UI (Virtual Keys → Create New Key). Never commit it.

---

## 3. Models — capability names and HARD constraints

Two capabilities are exposed. **Yoyo code must reference only these names.** The backing
model is a server-side concern and will change.

| Capability | Backing model (do not hardcode) | Speed | Use for |
|---|---|---|---|
| `agent` | muse-glimmer (27.9B dense, Q4_K_M) | ~12 tok/s | tool use, multi-step tasks, anything agentic, concurrent load |
| `fast` | qwen3.6 (35B-A3B MoE) | ~76 tok/s | closed-context work: summarize, extract, answer-from-chunks |

### 3.1 CONSTRAINT — `fast` must never be given tools

This is a correctness requirement, not a performance preference. Measured over four trials:

- 3/4 runs: called the tool once, hit an error, gave up without retrying
- **1/4 runs: did not call the tool at all and fabricated a plausible answer** (invented a
  stock price of $135.40, with a confident caveat about market fluctuations)

Any code path that passes a `tools=[...]` array must use `agent`. A fabricated answer that
looks plausible is worse than a refusal, and this failure mode is invisible in throughput
benchmarks.

`agent` passed the same test: retried after the first error, retried again, recovered, and
reported the correct value.

### 3.2 CONSTRAINT — fan-out gives no speedup

Measured concurrency scaling at 4 parallel requests:

| Model | scaling @ conc=4 | agg tok/s | wall clock 1 → 4 req |
|---|---|---|---|
| muse-glimmer (`agent`) | **3.62x** | 43.6 | 16.7s → 18.5s |
| qwen3.6 (`fast`) | 1.13x | 71.6 | 3.2s → 11.2s |
| qwen3.6:27b (dense) | 1.01x | 12.1 | 16.7s → 66.1s |
| nemotron-3.5-lightning | 1.00x | 67.2 | 3.0s → 11.9s |

Three of four models **serialize completely**. Concurrent requests buy nothing.

Implications for Yoyo:
- Any code that issues parallel requests expecting wall-clock savings is unfounded unless
  it targets `agent`.
- `agent` is the only capability where concurrency is close to free (four requests cost
  1.8 seconds more than one).
- The cause of the exception is **unexplained**. Two architectural hypotheses were tested
  and both were wrong (dense-vs-MoE, and speculative decoding). Do not infer concurrency
  behaviour from architecture, parameter count, or what a sibling model does. It is an
  empirical property that must be measured per model.

### 3.3 Thinking mode is on by default and expensive

Both models emit reasoning traces. A bare `"ping"` produced 358 completion tokens, almost
all of it reasoning. On `agent` at 12 tok/s that is ~30 s of overhead on a trivial turn.

- Responses include a `reasoning_content` field alongside `content`. Yoyo must handle or
  discard it. Do not display it to users by default.
- `agent` supports controllable reasoning strength (low/medium/high/xhigh). Use **low** for
  worker/extraction roles, reserve high for planning. Measured: dropping to low cut token
  count ~38% (785 → 485) with no change in tok/s, so wall clock fell proportionally.
- Server-side `think: false` is being added for `fast`. Until confirmed, assume thinking
  is on.

### 3.4 Budget realistic latencies

- `fast`, short answer: 2–5 s
- `fast`, long answer with thinking: 15–25 s
- `agent`, single turn: 30–60 s
- `agent`, 3–4 turn tool loop: 2–5 minutes
- Cold model load adds 7–11 s (two models stay resident; a third evicts one)

Yoyo's UI must be built for these. Streaming responses are strongly preferred over
blocking calls.

---

## 4. Model configuration reference (server-side, informational)

Yoyo does not control these but should know them:

- `OLLAMA_NUM_PARALLEL=4` — four concurrent slots total, shared across *all* clients
  including teammates. Yoyo is not privileged.
- `OLLAMA_MAX_LOADED_MODELS=2` — requesting a third model evicts one and costs a reload.
  Alternating rapidly between `agent` and `fast` is fine; both stay resident.
- `OLLAMA_CONTEXT_LENGTH=32768` — **hard ceiling.** `fast` advertises 262K and `agent`
  131K, but both are capped at 32K server-side. Yoyo's RAG chunk budget must fit inside
  32K including the reasoning trace. Do not build prompts assuming 128K+.
- `OLLAMA_KEEP_ALIVE=30m`

Per-key limits apply: `max_parallel_requests` typically 2, `rpm_limit` may be set. Handle
429 responses with backoff.

---

## 5. Yoyo client runtime

| Item | Decision |
|---|---|
| Location | `C:\Projects\Yoyo\YoyoClient` |
| Application runtime | **native Windows Python** (no WSL dependency in the app path) |
| Qdrant | Docker Desktop |
| Open WebUI | Docker Desktop (optional) |
| Orchestration | LangGraph + PydanticAI |
| Vault / canon | Obsidian, plain Markdown, source of truth |
| Vector index | Qdrant — treat as ephemeral and rebuildable |
| Observability | Langfuse |

**Structural requirement:** all business logic sits behind `src/yoyo/api.py`. No logic in
the CLI. This keeps a future three-tier extraction possible (thin clients against a Yoyo
server, needed for phone support) without a rewrite. That extraction is deferred, not
rejected.

---

## 6. Model config externalization

`yoyo-models.yaml` maps Yoyo's internal roles to capability names — never to model
identities. Example shape:

```yaml
capabilities:
  agent:
    endpoint: fast_or_agent_name_here   # -> "agent"
    tools: true
    reasoning: high
  worker:
    endpoint: agent
    tools: true
    reasoning: low
  summarize:
    endpoint: fast
    tools: false        # HARD: see §3.1
  extract:
    endpoint: fast
    tools: false        # HARD: see §3.1
```

Add a comment on any `tools: true` role recording the §3.1 constraint, so a future change
that repoints it at `fast` is caught in review.

Embeddings and reranking are **not yet exposed** on the endpoint. `bge-m3` and
`bge-reranker-v2-m3` are named in the plan but not deployed. Either request they be added
server-side, or run embeddings locally on the laptop. Do not assume they exist.

---

## 7. What is void from the original plan documents

These were written when all of Yoyo ran on the GB10. Treat as **void on the execution
path**, not merely stale:

- `plan-gb10.md` §1 (host setup), §1.2 (LUKS volume), §1.3 (memory contract — survives as
  a MyAIServer concern only), §1.4 (compose deltas, `shared-llm` network, aarch64 audit),
  §3 (Squid egress gateway), §4 (backup design — assumed box-local paths)
- `yoyo-phase-0-plan-gb10.md` T2-GB10, T3, T4, T-tenancy in their current form
- ADR-020's `shared-llm` Docker network mechanics. The *goal* (others share models without
  reaching Yoyo's data) is now met by LiteLLM virtual keys, which is strictly better. The
  isolation claim survives; the named mechanism does not.
- ADR-012-GB10's memory contract (85 GB Yoyo ceiling / 33 GB floating pool). It was sized
  around a resident 65 GB gpt-oss:120b that is no longer in the plan, on a box Yoyo no
  longer runs on.
- ADR-002-GB10's engine ordering. gpt-oss:120b and vLLM both came off the plan on measured
  grounds (§9). SGLang is a maintenance liability on this platform — a single orphaned
  build lagging upstream.
- ADR-017-GB10 (voice, CPU-pinned STT/TTS on the box's efficiency cores). Deferred, not
  decided.

---

## 8. Open questions that block Yoyo work

| # | Question | Blocks |
|---|---|---|
| 4 | **Encryption at rest on the laptop.** BitLocker on or off? If off, what protects the corpus and SQLite? | **the first real corpus ingest** |
| 5 | **Egress control replacement.** ADR-009's read-only Squid audit boundary does not exist on Windows. Yoyo's outbound traffic is currently unaudited. This is a genuine reduction in the security posture canon promised. | not blocking, but must not be left implicitly answered |
| 6 | **Backup tiers re-targeted for the laptop.** §4's NAS and DR paths assumed the box. Until re-targeted, the restore drill cannot happen — and canon insists it comes first. | first ingest |
| 1 | Email provider protocol reality | email MCP server design |
| 2 | Corpus size and formats | initial Docling window sizing |

Questions 4 and 6 are the real gates. Do not ingest a real corpus before both are answered.

---

## 9. Findings worth not re-deriving

Three results were counterintuitive enough to record as decisions rather than numbers.

**1. Sparsity ratio predicts single-stream speed; file size does not.** Generation is
bandwidth-bound on *active* parameters at 273 GB/s. Measured: a 25 GB MoE ran at 67 tok/s
while an 18 GB dense model ran at 12. Rank candidates by active-parameter bytes per token,
never by disk footprint. Do not shortlist or eliminate a model on file size.

**2. Concurrency scaling does not follow from architecture.** See §3.2. Three of four
models serialize; one does not; the cause is unexplained and two plausible explanations
were tested and falsified. Measure per model, always.

**3. Tool-call fidelity is a hard constraint, not a style preference.** See §3.1. A model
that skips an available tool and fabricates is failing correctness. This belongs in the T6
golden eval set as a gate that a model fails regardless of how well it scores elsewhere.
The eval that proves it **is not yet written** — pinning any model to a tool-using role
before it exists is provisional.

**Also recorded:** gpt-oss:120b was dropped because three smaller models exceeded its
expected ~41 tok/s at a third of the memory. vLLM was deferred, not rejected — it remains
the answer if batching throughput becomes the bottleneck, and its prefix caching is
untested here (the benchmark used four distinct prompts, so nothing was shared; swarm
workers would share a system prompt, which is exactly the case prefix caching exploits).

---

## 10. Agent swarm — status and constraints

Confirmed as a wanted capability (task decomposition: PM → developers → QA, and research
fan-out). Assessed as viable **after Phase 0**, with these constraints:

- **Decomposition must buy quality, not throughput.** Parallel workers give no wall-clock
  benefit except on `agent` (§3.2). Five agents in parallel ≈ five agents in sequence for
  `fast`.
- **Topology:** `agent` as supervisor and as workers, since it is the only tool-reliable
  and only concurrency-scaling capability. Use reasoning strength `low` for workers, `high`
  for the supervisor.
- **Budget governance is required before building.** Max depth, max total worker turns,
  wall-clock ceiling. Local inference has no dollar cost, so nothing naturally stops
  runaway recursion.
- **Admission control:** a swarm run monopolizes shared slots that 3–5 teammates depend on.
  `OLLAMA_NUM_PARALLEL=4` total. Coordinate before running large jobs.
- **HITL:** swarm output lands as vault drafts; approval is a file move. Do not invent a
  second approval mechanism.
- **Code execution sandbox is unsolved.** Agents that run code need a writable workspace
  next to a vault they must not touch. Two research candidates: NVIDIA **OpenShell** (the
  sandbox component of NemoClaw — the *harness* is rejected for vendor lock-in, the sandbox
  is not), and **Qwen-AgentWorld** (a language *world model* that simulates terminal/OS/web
  environments — wrong tool for acting, plausible tool for testing swarm logic without real
  side effects). Neither is a Phase 0 dependency.
- **Validate against a single-agent baseline on the golden eval set** before generalizing.
  Multi-agent loses to a good single agent more often than the literature suggests.

---

## 11. MyAIServer reference (for debugging only)

Do not change these from the client side.

- Host `TAI`, user `admin`, DGX OS 7.5.0, kernel 6.17.0-1029-nvidia, driver 580.173.02,
  CUDA 13.0, aarch64
- GB10 Grace Blackwell, 128 GB unified LPDDR5X @ 273 GB/s (121 GB reported usable)
- 1 TB PCIe Gen4 NVMe, **TCG Pyrite** (no hardware self-encryption — LUKS would be
  mandatory if sensitive data lived here; it does not)
- Wi-Fi `wlP9s9`, powersave disabled; ethernet `enP7s7` unused (no cable)
- Ollama: native systemd service, `User=ollama`, models in
  `/usr/share/ollama/.ollama/models`, binds `127.0.0.1:11434`
- LiteLLM: `/home/admin/open-webui/litellm_env/`, config
  `/home/admin/open-webui/config.yaml`, secrets `/etc/litellm/litellm.env` (0600),
  unit `litellm.service`, port 4000
- Tailscale Funnel is currently **on** (for cloud-deployed teammate code). It will be
  turned off once teammates are on the tailnet. The URL does not change either way.
- `vm.swappiness=1` (15 GB swap present; must not be touched during inference)
- `nvidia-smi` reports "Memory-Usage: Not Supported" on unified memory. Use cgroup counters
  or `free`.

**Verify current model state before relying on it:**
```bash
ollama list
curl -s https://tai.bombay-tint.ts.net/v1/models -H "Authorization: Bearer $KEY"
```
Models were added and removed during bring-up; `gemma4`, `nemotron-3.5-lightning`, and
`qwen3.6:27b` were all slated for removal but the final state is unconfirmed.

---

## 12. Do not do

- Do not pass `tools=` to `fast`. Ever. (§3.1)
- Do not hardcode model names in Yoyo code. Use `agent` / `fast`. (§3)
- Do not hardcode `10.0.0.63`. Use the Tailscale hostname. (§2)
- Do not connect to Ollama on 11434. Go through LiteLLM. (§2)
- Do not build prompts assuming 128K+ context. The cap is 32K. (§4)
- Do not fan out concurrent requests expecting speedup on `fast`. (§3.2)
- Do not reference `gpt-oss:120b`, `shared-llm`, LUKS on the box, or the Squid gateway.
  All void. (§7)
- Do not ingest a real corpus before open questions 4 and 6 are closed. (§8)
- Do not assume embeddings or reranking are available on the endpoint. (§6)

# Project Yoyo — laptop client

A private assistant running entirely on hardware you own. No third-party model API, no
corpus leaving your network.

This file is the **living status document**. Update it in the same commit as the change it
describes; if it disagrees with the code, the file is the bug.

- **Last updated:** 2026-08-14 (end of day 2)
- **Phase:** Phase 0 complete on the critical path. Mail and orchestration in progress.
- **Tests:** 198 passing
- **Only gate on a real corpus:** OQ4 — the disk is not encrypted

**Primary sources, authoritative over this summary:** `yoyo-client-handoff.md` (the endpoint
contract) and `docs/model-baseline-gb10.md` (the measurements).

---

## 1. What was built, and when

**2026-08-13 — MyAIServer (the GB10 box).** Ollama under systemd, LiteLLM v1.95.0 with
Postgres-backed keys, authenticated HTTPS over Tailscale, shared with 3–5 teammates.
Bake-off across four models; gpt-oss:120b dropped, vLLM deferred.

**2026-08-14 — the laptop client**, built in this order:

1. **Architecture split (ADR-021).** The box became a model server only; Yoyo moved to the
   laptop. Recorded what that gave up as well as what it gained.
2. **Core stack.** Config, LLM client, SQLite schema, Qdrant, chunking, ingest, hybrid
   retrieval with RRF, turn loop, HTTP API, CLI, `yoyo doctor`.
3. **Reconciled against the handoff.** Roles replaced invented capability names; the
   tool-fidelity constraint became enforced code; 900 s timeout; 429 backoff; reasoning-trace
   handling; streaming; local embeddings.
4. **Proven end to end.** First RAG turn answered correctly with citations. Ingest
   idempotency verified across new / changed / unchanged, zero orphaned vectors.
5. **Backup and restore.** `yoyo backup` / `restore-drill` / `restore`. Drill passed 11/11
   against the real USB drive.
6. **Tools and the golden eval set.** Bounded agent loop, four built-in tools, seven gates —
   **7/7 against the live model**, including tool fidelity under urgency framing, at
   reasoning `low`, and retrying after an injected error.
7. **MCP, both directions.** Client adapter mounts any MCP server; first-party servers for
   the Obsidian vault and the corpus. Live vault turn answered correctly across vault +
   corpus.
8. **Mail.** Gmail and Microsoft 365 behind one tool surface. Read and draft, no send path.

### Bugs found by running it, not by reading it

Recorded because each cost real time and would otherwise be rediscovered:

| Bug | Why it mattered |
|---|---|
| Rich markup ate citation output | `[model-baseline-gb10#3]` parsed as a style tag and vanished |
| Budget exhaustion returned an empty answer | Eight tool calls of work, nothing shown to the user |
| `StringIO` as subprocess stderr | No `fileno()` — worked on Linux, failed on Windows |
| mcp 2.0 renamed `FastMCP` → `MCPServer` | Both servers died on import |
| SDK moved camelCase → snake_case | Tool schemas read as empty, so **every tool call arrived with no arguments**, and errors came back as ordinary strings |
| Empty `YOYO_VAULT_PATH` | `Path("")` → `.` — the working directory silently became the vault |
| Prefix doubling | `vault_vault_search` |
| An eval "failure" that was mine | The abstain gate's marker list was too narrow; the model had answered correctly. Fixed the gate, not the prompt. |

---

## 2. Architecture

```
┌────────────────────────────────────┐        ┌─────────────────────────────────┐
│  MyAIServer — ASUS Ascent GX10     │        │  Yoyo — Windows laptop          │
│  (NVIDIA GB10, 128 GB @ 273 GB/s)  │        │  C:\Projects\Yoyo\YoyoClient    │
│                                    │        │                                 │
│   Ollama  127.0.0.1:11434          │        │   corpus · SQLite · Qdrant      │
│     └── LiteLLM v1.95.0  :4000     │◀───────│   hybrid retrieval + RRF        │
│           └── Postgres (keys)      │ HTTPS  │   local embeddings (fastembed)  │
│                                    │  over  │   agent loop · tools · evals    │
│   serves: `agent`, `fast`          │Tailscale   MCP client + 3 MCP servers    │
└────────────────────────────────────┘        └─────────────────────────────────┘
        https://tai.bombay-tint.ts.net/v1
```

Yoyo is a **pure client** with no more privilege than any teammate; its data never touches
the GB10. Code names a **role**, never a model.

### The contract

| Item | Value |
|---|---|
| Base URL | `https://tai.bombay-tint.ts.net/v1` (hostname, never the LAN IP) |
| Auth | per-client LiteLLM virtual key |
| Server timeout | 900 s — the client must be ≥ this |
| Context ceiling | **32768 tokens, hard**, whatever a model advertises |
| Concurrency | `OLLAMA_NUM_PARALLEL=4`, shared with teammates |

| Capability | Model | Speed | Tools | Concurrency |
|---|---|---|---|---|
| `agent` | muse-glimmer 27.9B dense Q4_K_M | ~12 tok/s | **yes** | 3.62× @ 4 |
| `fast` | qwen3.6 35B-A3B MoE | ~76 tok/s | **NEVER** | 1.13× — serialises |

| Role | → | Tools | Reasoning | For |
|---|---|---|---|---|
| `supervisor` | `agent` | yes | high | planning, multi-step |
| `worker` | `agent` | yes | low | tool-using worker turns |
| `answer` | `fast` | no | — | the default RAG turn |
| `summarize` / `extract` | `fast` | no | — | closed-context work |

---

## 3. Status

**✅ done and verified** · **🟡 built, not yet exercised for real** · **🔨 scaffolded** ·
**⬜ not started** · **❌ dropped**

### MyAIServer

| Feature | | Notes |
|---|---|---|
| Ollama + LiteLLM + Postgres, systemd | ✅ | survives reboot |
| HTTPS over Tailscale | ✅ | verified from the laptop |
| `agent` and `fast` served | ✅ | |
| Bake-off, four models | ✅ | `docs/model-baseline-gb10.md` |
| `think: false` on `fast` | ⬜ | assume thinking is on |
| Embedding model (`bge-m3`) | ⬜ | not deployed — Yoyo embeds locally |
| Reranker | ⬜ | not deployed |
| Funnel off · DHCP reservation · key rate limits | ⬜ | housekeeping |

### Yoyo laptop client

| Feature | | Where |
|---|---|---|
| Config layer, role registry | ✅ | `config.py`, `yoyo-models.yaml` |
| Tool-fidelity guard (raises, not warns) | ✅ | `llm.py::_guard_tools` |
| LLM client: chat, stream, 429 backoff, reasoning capture | ✅ | `llm.py` |
| `yoyo doctor` — 7 checks | ✅ | `doctor.py` |
| SQLite schema, FTS5, citations | ✅ | `migrations/`, `storage/db.py` |
| Local embeddings (fastembed, 768d) | ✅ | `embeddings.py` |
| Qdrant vectors, no orphans | ✅ | `storage/vectors.py` |
| Chunking, ingest, hash-skip | ✅ | `rag/` |
| Hybrid retrieval + RRF | ✅ | `rag/retrieve.py` |
| Turn loop with citations | ✅ | `core.py` |
| HTTP API + streaming | 🟡 | `api.py` — `/ask` used, `/ask/stream` untested |
| CLI (20 commands) | ✅ | `cli.py` |
| Backup / restore drill | ✅ | `backup.py` — 11/11 on the real drive |
| Tool registry, 4 built-ins | ✅ | `tools.py` |
| Bounded agent loop | ✅ | `agent.py` — iteration + wall-clock budgets |
| Golden eval set, 7 gates | ✅ | `evals/golden.yaml` — 7/7 live |
| MCP client adapter | ✅ | `mcp/client.py` |
| Vault MCP server | ✅ | `mcp/vault_server.py` — drafts-only write |
| Corpus MCP server | 🟡 | `mcp/corpus_server.py` — never mounted by another client |
| Mail MCP server (Gmail + M365) | 🟡 | `mail/`, `mcp/mail_server.py` — **needs OAuth setup** |
| Obsidian vault as canon | 🟡 | pointed at `test-vault`, not a real vault |
| LangGraph + PydanticAI orchestration | ⬜ | next |
| Langfuse observability | ⬜ | |
| Calendar MCP | ⬜ | |
| Voice (STT/TTS) | ⬜ | box-side plan void |
| Agent swarm | ⬜ | post-Phase 0 |
| Encryption at rest | ❌ | **BitLocker off — see OQ4** |
| Egress auditing | ❌ | lost moving to Windows — OQ5 |

---

## 4. Tomorrow

In the order I'd take them.

### 1. Turn on BitLocker — 10 minutes, unblocks everything else

Settings → Privacy & security → Device encryption. **Save the recovery key somewhere that is
not the laptop.** Then re-run `yoyo backup F:\yoyo-backups`.

Why first: you're about to put mailbox refresh tokens on this disk. Everything below makes
the unencrypted-disk problem worse, and this is the cheapest item on the list.

### 2. Finish mail setup — your accounts, your consent

Neither can be done for you: both need account-level app registration and OAuth consent.

**Gmail** — Cloud Console → enable Gmail API → Credentials → OAuth client ID → *Desktop app*
→ save JSON to `secrets\gmail-personal.json` → add yourself as a test user.

**Microsoft** — Entra → App registrations → New → Authentication → *Allow public client
flows: Yes* → API permissions → Graph → Delegated → `Mail.Read`, `Mail.ReadWrite`.
**Do not add `Mail.Send`.** Copy the Application ID into `yoyo-mail.yaml`.

```powershell
uv pip install -e ".[dev,local-embed,mail]"
# yoyo-mail.yaml: fill client_id, set enabled: true
yoyo mail accounts
yoyo mail auth personal
yoyo mail auth work
yoyo mail search "invoice"
# yoyo-mcp.yaml: mail.enabled: true
yoyo agent "what did Alice send me about the invoice?"
```

### 3. Run the graph live  ✅ built, needs a real run

Built and unit-tested; never run against the live endpoint.

```powershell
uv pip install -e ".[dev,local-embed,mail]"
yoyo plan "what does my vault say about the GB10 box, and what did the bake-off conclude about concurrency?"
```

Two things to watch: whether the planner produces sensible *independent* subtasks (dependent
ones waste the parallelism), and whether wall-clock beats running the same question through
`yoyo agent`. If it does not, the decomposition is not earning its keep.

### 4. Point the vault at real notes

`YOYO_VAULT_PATH` currently points at `test-vault` (three notes I wrote). Swap it for your
actual Obsidian vault — **after** step 1.

### Smaller items, any time

- **Duplicate tool-call guard.** The agent still occasionally reruns a search with reworded
  terms. Cache results within a turn and short-circuit repeats — mechanical, not more prompting.
- **`docs/model-baseline-gb10.md`** — verify the numbers against a fresh `ollama list`;
  `gemma4`, `nemotron-3.5-lightning` and `qwen3.6:27b` were slated for removal, unconfirmed.
- **`think: false`** on `fast`, server-side.
- **Answer OQ7** (local vs server embeddings) while the corpus is still small — either way
  costs a full reindex.
- **Calendar MCP**, same adapter shape as mail.
- **Test `/ask/stream`** — written, never exercised.

---

## 5. Setup

**Prerequisites:** Windows, Python 3.11+, Docker Desktop, Tailscale on the same tailnet, and
a LiteLLM virtual key for this laptop.

```powershell
cd C:\Projects\Yoyo\YoyoClient
copy .env.example .env        # fill YOYO_LLM_BASE_URL and YOYO_LLM_API_KEY
uv venv; .venv\Scripts\Activate.ps1
uv pip install -e ".[dev,local-embed]"     # add ,mail and ,ingest as needed
docker compose up -d
yoyo migrate
yoyo doctor                   # the gate — nothing below is trustworthy until this is green
```

| Doctor check | Fails when | Try |
|---|---|---|
| `env` | placeholders left, timeout < 900 s | edit `.env` |
| `server reachable` | tailnet down, bad key | `ping tai.bombay-tint.ts.net` |
| `roles` | a role points at an unserved capability | it prints what is served |
| `tool fidelity` | a `tools: true` role points at `fast` | repoint at `agent` — never relax |
| `embeddings` | fastembed missing, dimension mismatch | install extra / `reindex --recreate` |
| `sqlite` | schema not applied | `yoyo migrate` |
| `qdrant` | Docker down, dimension mismatch | `docker compose up -d` |

---

## 6. Commands

```powershell
# corpus
yoyo ingest <path>            yoyo search "q"          yoyo stats
yoyo reindex --recreate       yoyo migrate

# asking
yoyo ask "q"                  # RAG turn on `fast`, ~15-30 s
yoyo ask "q" --role supervisor
yoyo agent "q"                # tool-calling turn on `agent`, 60-150 s
yoyo plan "q"                 # multi-step: plan, parallel workers, synthesise
yoyo serve                    # HTTP API on 127.0.0.1:8080

# tools, MCP, evals
yoyo tools                    yoyo eval                yoyo eval --only <case>
yoyo mcp list                 yoyo mcp serve-vault     yoyo mcp serve-corpus
yoyo mcp serve-mail

# mail
yoyo mail accounts            yoyo mail auth <name>    yoyo mail search "q"

# backup
yoyo backup F:\yoyo-backups   yoyo restore-drill --dest F:\yoyo-backups
yoyo restore <archive> --force
```

Latencies to expect: `fast` short 2–5 s · `fast` with thinking 15–25 s · `agent` single turn
30–60 s · `agent` tool loop 2–5 min · cold model load +7–11 s.

---

## 7. Configuration

| File | Holds |
|---|---|
| `.env` | endpoint URL, key, paths, vault path, timeout (keep ≥ 900) |
| `yoyo-models.yaml` | roles → capabilities, embeddings, reranking, retrieval tuning |
| `yoyo-mcp.yaml` | MCP servers to mount as a client |
| `yoyo-mail.yaml` | mail accounts and providers |
| `docker-compose.yml` | Qdrant + Open WebUI, loopback only |
| `secrets/` | OAuth client secrets — gitignored |
| `data/mail-tokens/` | OAuth refresh tokens — gitignored, **unencrypted** |

---

## 8. Data model

**SQLite is the system of record. Qdrant holds vectors and a `chunk_id`** — ephemeral and
rebuildable. If they disagree, SQLite wins and you re-embed.

| Table | Holds |
|---|---|
| `documents` | path, title, content hash (drives skip-if-unchanged) |
| `chunks` | text, ordinal, offsets, `embedded_at`, `embed_model` |
| `chunks_fts` | FTS5 index, trigger-maintained |
| `conversations` / `messages` | history, model, tokens, latency |
| `message_citations` | which chunks produced which answer |
| `entities` / `entity_mentions` | schema only, no code yet |

---

## 9. Invariants

Breaking these is a bug, not a style choice.

1. **Never pass tools to `fast`.** Enforced in code; it raises. Evidence in the baseline doc.
2. **Roles in code, never capability or model names.**
3. **`llm.py` is the only module that talks to the server.**
4. **Business logic lives behind `api.py`** — keeps the future three-tier split cheap.
5. **SQLite is the system of record.**
6. **Every answer records its citations.**
7. **Never fan out concurrent requests against `fast`.** Only `agent` scales; never infer
   concurrency from architecture.
8. **Never assume more than 32K context** — the reasoning trace counts against it.
9. **Reasoning is not the answer.** Keep `reasoning_content` out of user-facing text.
10. **Yoyo writes drafts, humans approve.** Vault writes go to `yoyo-drafts/`; mail has no
    send path at all. Do not add a second approval mechanism — it would only be a way around
    the first.
11. **Changing the embedding model invalidates the corpus.** `yoyo reindex --recreate`.

---

## 10. Tests

```powershell
pytest -q          # 198 passing
ruff check src tests
```

| Area | Tests | Covers |
|---|---|---|
| Graph | 22 | routing, plan capping reported not silent, budgets passed through, parallelism bounded *and* actually parallel, worker/planner/synthesis failure paths |
| Structured output | 19 | fenced/prose-wrapped/nested/escaped JSON, retry feeds the error back, gives up cleanly |
| Mail | 30 | config, account resolution, Gmail/Graph parsing, HTML→text, MIME round trip, **structural proof no send path exists** |
| Vault | 22 | path confinement both directions, symlink escape, frontmatter, backlinks, drafts-only writes, drafts excluded from canon |
| Eval harness | 20 | fidelity gate catches a fabricating model, retry gate fails give-up-after-one-error, abstention both directions |
| MCP client | 20 | config, schema translation, result unwrapping, SDK field-name drift, failure diagnostics, **live stdio round trip** |
| Agent / tools | 19 | arg validation, errors surfaced not raised, iteration + wall-clock budgets, forced answer on exhaustion |
| Backup | 14 | archive contents, `.env` exclusion, drill fails on corruption and count mismatch |
| Storage | 8 | migrations, hash skip, chunk rebuild, FTS, ordering |
| Chunking | 8 | boundaries, coverage, ordinals, size bounds |
| Retrieval | 6 | RRF ranking, context budget, citations |

**Not covered — assume broken until exercised:** every mail network path, `/ask/stream`,
Docling extraction, the corpus MCP server mounted by a third-party client.

---

## 11. Open questions

| # | Question | Blocks |
|---|---|---|
| **4** | **Encryption at rest.** BitLocker off. Corpus, SQLite, LiteLLM key, and soon mailbox refresh tokens all plaintext. Deferred by owner; **test data only** while it stands. | a real corpus, real mail |
| 5 | **Egress auditing.** ADR-009's Squid boundary doesn't exist on Windows. Yoyo's outbound traffic is unaudited. | nothing, but must not be implicitly answered |
| 7 | **Embeddings local or server.** Costs a reindex either way — cheapest to decide now. | — |
| 8 | **Golden eval set** covers current pins only. Reopen for new roles or models. | future pins |
| 1 | Email provider protocol | *closed by the mail build — Gmail + Graph* |
| 2 | Corpus size and formats | Docling sizing |
| 6 | Backups | *closed 2026-08-14, drill 11/11* |

---

## 12. Decisions

| ADR | Subject |
|---|---|
| ADR-021 | Two-tier split — model server vs laptop stack, and what it cost |
| ADR-022 | Sparsity predicts speed; concurrency is empirical; thinking is on and costly |
| ADR-023 | Tool-call fidelity is a hard constraint |
| ADR-024 | gpt-oss:120b dropped; vLLM deferred, not rejected; SGLang a liability |
| ADR-025 | Embeddings run locally until the server exposes one |

Authoritative log: the Claude project docs `yoyo-architecture-decisions-2026-08-14.md` and
`yoyo-open-questions-ledger.md`. `docs/adr/` mirrors ADR-021 only.

**Void from the original plan:** `plan-gb10.md` §1–§4, Phase-0 T2/T3/T4/T-tenancy,
ADR-020's `shared-llm` mechanics, ADR-012-GB10's memory contract, ADR-002-GB10's engine
ordering, ADR-017-GB10 voice.

---

## 13. Maintaining this file

Change a feature → move its row in §3 and add a changelog line, same commit. Close an open
question → move it out of §11 and record an ADR. **🟡 means "written but never run against
the real thing"** and stays 🟡 until someone runs it.

## 14. Changelog

| Date | Change |
|---|---|
| 2026-08-13 | MyAIServer built. Bake-off across four models; gpt-oss:120b dropped, vLLM deferred. |
| 2026-08-14 | Architecture split (ADR-021). Laptop stack scaffolded. |
| 2026-08-14 | Reconciled against the handoff: roles, enforced tool fidelity, 900 s timeout, 429 backoff, reasoning handling, streaming, local embeddings. |
| 2026-08-14 | First end-to-end RAG turn with citations. Ingest idempotency proven; 10 chunks = 10 vectors, no orphans. |
| 2026-08-14 | Backup + restore drill; **11/11 on the real USB drive**. OQ6 closed. |
| 2026-08-14 | Tool registry, bounded agent loop, golden eval set — **7/7 live**, all four hard gates. |
| 2026-08-14 | MCP both directions: client adapter, vault server, corpus server. Live vault turn correct across vault + corpus. Tuned 260 s/8 iters → **148 s/6 iters, completed**. |
| 2026-08-14 | Mail MCP: Gmail + Microsoft 365, read and draft, no send path. 168 tests. |
| 2026-08-15 | Git initialised (`bbc89d2`, 56 files). Fixed a regression where a transfer archive re-disabled the vault MCP server. |
| 2026-08-15 | LangGraph supervisor graph: plan → parallel workers → synthesise, with structured output through `llm.py` rather than PydanticAI. 198 tests. |

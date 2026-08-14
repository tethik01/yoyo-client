# Project Yoyo — laptop client

A private assistant running entirely on hardware you own. No third-party model API, no
corpus leaving your network.

This file is the **living status document**. Update it in the same commit as the change it
describes; if it disagrees with the code, the file is the bug.

- **Last updated:** 2026-08-15 (end of day 3)
- **Phase:** Phase 0 complete on the critical path. Orchestration measured and settled.
  Mail built but awaiting OAuth setup.
- **Tests:** 256 passing
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
| `agent`, `fast` and `coder` served | ✅ | `coder` added 2026-08-15 (ADR-027) |
| Bake-off, four models | ✅ | `docs/model-baseline-gb10.md` |
| qwen3-coder-next evaluated and promoted | ✅ | 7/7 gates, 50 tok/s — ADR-027 |
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
| CLI (24 commands) | ✅ | `cli.py` — documented in §6 |
| Backup / restore drill | ✅ | `backup.py` — 11/11 on the real drive |
| Tool registry, 4 built-ins | ✅ | `tools.py` |
| Bounded agent loop | ✅ | `agent.py` — iteration + wall-clock budgets |
| Golden eval set, 7 gates | ✅ | `evals/golden.yaml` — 7/7 live on `coder` |
| Concurrency bench | ✅ | `bench.py` — distinct prompts, 429s counted separately |
| Fabricated-citation scrubber | ✅ | `citations.py` — strips invented paths, CLI warns |
| Untried-source hint in the agent loop | ✅ | `agent.py` — fixes source tunnelling |
| MCP client adapter | ✅ | `mcp/client.py` |
| Vault MCP server | ✅ | `mcp/vault_server.py` — drafts-only write |
| Corpus MCP server | 🟡 | `mcp/corpus_server.py` — never mounted by another client |
| Mail MCP server (Gmail + M365) | 🟡 | `mail/`, `mcp/mail_server.py` — **needs OAuth setup** |
| Obsidian vault as canon | 🟡 | pointed at `test-vault`, not a real vault |
| LangGraph supervisor graph | ✅ | `graph/supervisor.py` — plan → parallel workers → synthesise |
| Orchestration baseline measured | ✅ | ADR-026, four rounds — `plan` wins multi-part |
| PydanticAI | ❌ | rejected — structured output via `llm.py` instead |
| Langfuse observability | ⬜ | |
| Calendar MCP | ⬜ | |
| Voice (STT/TTS) | ⬜ | box-side plan void |
| Agent swarm | ⬜ | post-Phase 0 |
| Encryption at rest | ❌ | **BitLocker off — see OQ4** |
| Egress auditing | ❌ | lost moving to Windows — OQ5 |

---

## 4. Next

In the order I'd take them.

### 1. Turn on BitLocker — 10 minutes, unblocks everything else

Settings → Privacy & security → Device encryption. **Save the recovery key somewhere that is
not the laptop.** Then re-run `yoyo backup F:\yoyo-backups`.

Why first: you're about to put mailbox refresh tokens on this disk. Everything below makes
the unencrypted-disk problem worse, and this is the cheapest item on the list.

### 2. Finish mail setup — your accounts, your consent

**The largest gap between what is built and what is usable.** The code is done and tested;
neither step below can be done for you, because both need account-level app registration and
OAuth consent in your own browser.

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

### 3. Re-test the graph in its intended case

ADR-026 is settled for **two local sources**. The graph has still never been measured on the
case it was built for: mail **and** vault **and** corpus in one question. That test needs
step 2 done first, and it is the one that decides whether decomposition earns its keep
generally or only on this question shape.

### 4. Point the vault at real notes

`YOYO_VAULT_PATH` currently points at `test-vault` (three notes I wrote). Swap it for your
actual Obsidian vault — **after** step 1.

### Smaller items, any time

- **`docs/model-baseline-gb10.md`** — verify the numbers against a fresh `ollama list`;
  `gemma4`, `nemotron-3.5-lightning` and `qwen3.6:27b` were slated for removal, unconfirmed.
  The file also predates `coder`.
- **`think: false`** on `fast`, server-side — build `qwen3.6-nothink`, register it as a
  separate capability, and compare. `fast` currently pays thinking overhead on every turn.
- **Answer OQ7** (local vs server embeddings) while the corpus is still small — either way
  costs a full reindex.
- **Re-run the ADR-026 comparison a few more times.** Every round so far is a single trial
  per config. The gaps were large enough to act on and too small a sample to call closed.
- **Round 4 spent 3 tool calls after both parts were answerable** — possibly the new
  untried-source hint over-encouraging exploration. Only worth chasing if agent latency
  starts to matter.
- **Calendar MCP**, same adapter shape as mail.
- **Test `/ask/stream`** — written, never exercised.
- **Raise `OLLAMA_MAX_LOADED_MODELS` to 3** so `agent`, `fast` and `coder` can be resident
  together; currently a role switch can cost a cold load.

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

## 6. Command reference

Every command. Run `yoyo --help` or `yoyo <command> --help` for the machine-generated
version; this section explains **what each one is for and when you would reach for it**,
which `--help` cannot.

All commands are run from `C:\Projects\Yoyo\YoyoClient` with the venv active. Exit code 0
means pass; `doctor` and `restore-drill` return 1 on failure so they can gate a script.

### 6.1 Health and setup

| Command | What it does |
|---|---|
| `yoyo doctor` | Checks every seam in one pass: tailnet reachability, API key auth, that the roles in `yoyo-models.yaml` name capabilities the server actually serves, local embeddings load, SQLite migrations applied, Qdrant reachable, collection dimensions match the embed model. **Run this first whenever anything behaves oddly** — it turns "it's broken" into a named seam. Exits 1 if any check fails. |
| `yoyo migrate` | Applies pending SQLite migrations. Idempotent — prints `nothing (up to date)` when there is nothing to do. Runs implicitly on first use, so you rarely call it directly. |
| `yoyo stats` | Counts: documents, chunks, embedded chunks, conversations, messages, plus the Qdrant collection info. The fastest way to answer "did that ingest actually land?" — `chunks` and `embedded chunks` should be equal. |

### 6.2 Corpus

| Command | What it does |
|---|---|
| `yoyo ingest <path>` | Reads a file or folder into the corpus: extracts text, chunks it (1200 chars / 150 overlap), stores in SQLite, embeds into Qdrant. `--no-recursive` to stay in one folder. **Idempotent by content hash** — re-running on unchanged files skips them, so you can point it at the same folder daily. Reports new/changed/unchanged and lists failures rather than dying on one bad file. |
| `yoyo search "q"` | Hybrid retrieval **without calling a model**. Shows the passages, their chunk ids and fusion scores. Use this to separate "retrieval didn't find it" from "the model ignored it" — the single most useful debugging move when an answer is wrong. `--top-k N` (default 6). |
| `yoyo reindex` | Re-embeds chunks that have no vector. Cheap; safe to run any time. |
| `yoyo reindex --recreate` | Drops the Qdrant collection and re-embeds **everything**. Required after changing the embed model or its dimensions in `yoyo-models.yaml` — vectors from different models are not comparable, and mixing them silently degrades retrieval rather than erroring. |

### 6.3 Asking — three escalating modes

These are the three ways to get an answer, cheapest first. Picking the right one matters more
than tuning any of them.

| Command | What it does |
|---|---|
| `yoyo ask "q"` | **One retrieval, one model turn.** Retrieves passages, hands them to the model, prints the answer with numbered citations and the sources beneath. No tools, no loop. The default for "what do my documents say about X". `--role <name>` to use a different role, `--no-rag` to ask the model cold with no retrieval, `--conversation <id>` to continue a thread. |
| `yoyo agent "q"` | **Tool-calling loop.** The model chooses tools (corpus search, vault, mail, clock) and iterates until it can answer. Bounded: 8 iterations and 600 s by default, `--max-iterations N` to change. Mounts MCP servers from `yoyo-mcp.yaml` unless `--no-mcp`. Prints every tool call with ok/err so you can see the reasoning path. Use when the answer needs a live lookup, or several. |
| `yoyo plan "q"` | **Multi-agent research.** A planner decomposes the question, workers run **in parallel** with their own tool budgets, a synthesiser assembles the answer. `--max-subtasks N` (default 4), `--max-parallel N` (default 3). Prints the plan and its reasoning before the answer. Use for questions spanning different sources. |

**Which one?** (ADR-026, four measured rounds)

- Single source, single part → `ask`, or `agent` if it needs a lookup.
- **Multi-part or multi-source → `plan`.** It is now both more reliable *and* faster than
  `agent` on these: measured 23.6 s against 30.1 s, and in an earlier round `agent` answered
  a two-part question **wrongly** by searching only the vault and reporting "not found" for
  the half that was in the corpus.
- Do not reach for `plan` to speed up a simple question — it is ~3x slower there.

| Command | What it does |
|---|---|
| `yoyo serve` | Runs the local HTTP API on `127.0.0.1:8080` — `/ask` and `/ask/stream`. Loopback only, no auth, not exposed to the tailnet. This is the seam a future phone client would talk to. |

### 6.4 Tools and MCP

Yoyo speaks MCP **in both directions**: it mounts other people's servers as a client, and it
exposes its own capabilities as servers other clients can mount.

| Command | What it does |
|---|---|
| `yoyo tools` | Lists every registered tool with its description — exactly the text the model sees. If the model is misusing a tool, read its description here first; the description is the prompt. |
| `yoyo mcp list` | Mounts everything in `yoyo-mcp.yaml` and shows what each server provides, or why it failed. The go-to when a tool you expected is missing from `yoyo tools`. |
| `yoyo mcp serve-vault` | Runs Yoyo's Obsidian vault server over stdio. Read + search + backlinks, and `vault_write_draft` which **flattens any path into `yoyo-drafts/`** — the assistant cannot overwrite your notes. |
| `yoyo mcp serve-corpus` | Runs the ingested-corpus server over stdio. Read-only. |
| `yoyo mcp serve-mail` | Runs the mail server over stdio. Read and draft only — **no send path exists in the code**, and a test asserts that structurally. |

The `serve-*` commands are not meant to be run by hand — they are what another MCP client
(Claude Desktop, or Yoyo itself) launches as a subprocess. Running one in a terminal just
leaves it waiting on stdin.

### 6.5 Evaluation and measurement

The two commands that decide whether a model is allowed to do a job. Neither produces a score
you optimise; both produce a verdict you act on.

| Command | What it does |
|---|---|
| `yoyo eval` | Runs the golden set — 7 cases, 4 hard gates: **tool fidelity** (a probe tool holds an unguessable secret; the model must call it and report the value, fabricating instead is a hard fail), **tool retry** (the probe fails once — giving up after one error fails), **grounded** (the answer must cite a real chunk id and must contain no fabricated file path), **abstain** (the corpus cannot answer it; inventing an answer fails). `--only <case-or-kind>` to run one. Budget ~5 min for the full set. |
| `yoyo eval --role <role>` | Same gates against a different role — **this is how a candidate model gets promoted**. A model that has not passed all four gates does not get pinned to a tool-using role, regardless of how fast it is. |
| `yoyo bench --role <role>` | Measures single-stream speed and concurrency scaling for the capability behind a role. `--concurrency 1,4` sets the levels, `--repeats N` averages rounds. Reports aggregate tok/s, per-stream tok/s, scaling factor, and 429s **counted separately** so a per-key rate limit is never mistaken for the model serialising. Prints `NO MEASUREMENT` rather than a number when every request failed. |

**Why bench exists:** concurrency is **empirical** (ADR-022). Two architectural hypotheses
were tested and both falsified — being MoE predicts nothing, and a sibling model's scaling
predicts nothing. `agent` scales 3.76x at concurrency 4; `fast` and `coder` both serialise
(~1.1x) despite all three being MoE. Measure every new model.

### 6.6 Mail

Read and draft only, by design. Consent is per-account and the OAuth flow runs in your
browser — Yoyo never sees your password.

| Command | What it does |
|---|---|
| `yoyo mail accounts` | Every configured account, its provider, whether it is enabled, and whether it is authenticated. Shows where tokens live. Start here. |
| `yoyo mail auth <name>` | Runs the OAuth consent flow for one account. Warns first that this stores a long-lived refresh token for the whole mailbox on an unencrypted disk (OQ4). |
| `yoyo mail search "q"` | Searches mail **without involving a model** — the mail equivalent of `yoyo search`. Use it to confirm auth and scopes work before debugging an agent turn. `--account <name>`, `--limit N`. |

Scopes are deliberately minimal: Gmail `gmail.readonly` + `gmail.compose`; Microsoft Graph
`Mail.Read` + `Mail.ReadWrite`. **Never `Mail.Send`.** Drafts land in your mailbox for you to
review and send yourself — that asymmetry is the human-in-the-loop mechanism, not a
limitation to be removed later.

### 6.7 Backup and restore

| Command | What it does |
|---|---|
| `yoyo backup <folder>` | Snapshots SQLite + config into a timestamped zip. **Vectors are not included** — they are derived data, rebuilt by `reindex --recreate`, and including them would triple the archive for no recovery value. |
| `yoyo restore-drill <archive>` | **Proves a backup can be restored**, reading only, never touching live data. Opens the archive into a temp location, checks the schema, counts rows, verifies integrity. `--dest <folder>` uses the newest archive in that folder. Exits 1 on any failure. |
| `yoyo restore <archive> --force` | Replaces the live database from an archive. Destructive — `--force` is required. Vectors are *not* restored; run `yoyo reindex --recreate` afterwards, as the command reminds you. |

**An unverified backup is a guess.** `yoyo backup` prints the drill command for this reason.
Current status: 11/11 checks passing against the real USB drive (`F:\yoyo-backups`).

### 6.8 Latencies to expect

Measured on this hardware, current model assignments.

| Operation | Time |
|---|---|
| `yoyo search` (no model) | < 1 s |
| `yoyo ask` on `coder` | 8–10 s |
| `yoyo ask` on `fast` (thinking on) | 20–25 s |
| `yoyo agent`, single-source question | ~8 s |
| `yoyo agent`, multi-part question | ~30 s |
| `yoyo plan`, multi-part question | ~24 s |
| `yoyo eval`, full set | ~5 min |
| Cold model load | +7–11 s |

A first run after boot pays the model load; a `NO MEASUREMENT` from `bench` or a timeout on
the first `ask` of the day is usually the box swapping a model in, not a fault.

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
pytest -q          # 256 passing
ruff check src tests
```

| Area | Tests | Covers |
|---|---|---|
| Mail | 30 | config, account resolution, Gmail/Graph parsing, HTML→text, MIME round trip, **structural proof no send path exists** |
| Vault | 22 | path confinement both directions, symlink escape, frontmatter, backlinks, drafts-only writes, drafts excluded from canon |
| Eval harness | 20 | fidelity gate catches a fabricating model, retry gate fails give-up-after-one-error, abstention both directions |
| MCP client | 20 | config, schema translation, result unwrapping, SDK field-name drift, failure diagnostics, **live stdio round trip** |
| Agent / tools | 34 | arg validation, errors surfaced not raised, iteration + wall-clock budgets, forced answer on exhaustion, duplicate short-circuit, **untried-source hint**, **fabricated-path stripping** |
| Graph | 24 | plan/dispatch/synthesise, subtask cap never silently truncates, worker gets the full question, **planner splits on source difference not cost** |
| Citations | 7 | scrubber keeps real identifiers, replaces invented paths visibly, gate and scrubber share one regex |
| Bench | 9 | distinct prompts, `NO MEASUREMENT` when every request fails, 429s not read as serialisation |
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
| ADR-026 | Graph vs single agent — reversed on round 3, confirmed on round 4 |
| ADR-027 | qwen3-coder-next promoted to the tool-using roles |

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
| 2026-08-14 | Mail MCP: Gmail + Microsoft 365, read and draft, no send path. 168 tests. |
| 2026-08-15 | Git remote set up (`tethik01/yoyo-client`). LangGraph supervisor graph built; PydanticAI rejected. |
| 2026-08-15 | **qwen3-coder-next promoted** (ADR-027): 7/7 gates, 50 tok/s vs `agent`'s 11.7. Serialises (1.09x) — no fan-out benefit, but nearly all use is single-stream. `reasoning` must never be set on a coder role (Ollama 500). |
| 2026-08-15 | Fabricated citation path observed live twice on `coder`. Fixed in four prompts, a mechanical eval gate, and `citations.py` — the interactive path strips and warns, the gate still fails. |
| 2026-08-15 | **ADR-026 reversed.** On `coder`, `yoyo agent` answered a two-part question in 8.3 s and got it **wrong** — tunnelled into the vault, never called `search_corpus`. `yoyo plan` got it right in 23.6 s. |
| 2026-08-15 | Source-tunnelling fixed: untried-source hint at 2 calls, plus a prompt rule that "not in the notes" ≠ "not there". Patched agent now answers both parts correctly — in 30.1 s, still losing to the graph's 23.6 s. **ADR-026 confirmed on round 4.** |
| 2026-08-15 | `PLANNER_INSTRUCTION` rewritten: the "roughly three times slower" warning was measured on `agent` and is false on `coder`. Split criterion is now source difference, and "when unsure, SPLIT". |
| 2026-08-15 | README §6 expanded into a full command reference — every command, what it is for, and which of the three asking modes to reach for. 256 tests. |

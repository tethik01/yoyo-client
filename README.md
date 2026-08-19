# Project Yoyo — laptop client

A private assistant running entirely on hardware you own. No third-party model API, no
corpus leaving your network.

This file is the **living status document**. Update it in the same commit as the change it
describes; if it disagrees with the code, the file is the bug.

- **Last updated:** 2026-08-15 (day 3, later)
- **Phase:** Phase 0 complete. Orchestration settled (ADR-026/027). Gmail live. All five
  second-brain phases built (ADR-030) — **none of them exercised on real material yet.**
- **Tests:** 865 passing
- **Only gate on a real corpus:** OQ4 — the disk is not encrypted

**Start here if you just want to USE it:** [`USER-GUIDE.md`](USER-GUIDE.md) — the three
modes, vault vs corpus, citations, and what it gets wrong. This file is the build status;
that one is how to drive it.

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

**2026-08-15 — measurement, then breadth.**

9. **Git remote** set up against `tethik01/yoyo-client`.
10. **LangGraph supervisor graph** — plan → parallel workers → synthesise, with the same
    budget governance as the agent loop. PydanticAI evaluated and rejected; structured output
    goes through `llm.py` so there stays one egress point.
11. **A third model measured and promoted (ADR-027).** qwen3-coder-next passed all four hard
    gates 7/7 and runs 4.3x faster than `agent`. `supervisor`, `worker`, and later
    `answer`/`summarize`/`extract` all moved to it. The question that started this was
    whether a *bigger* model would help; the answer was that the right axis is sparsity.
12. **The orchestration question settled, against expectation (ADR-026).** Four rounds. The
    single agent answered a two-part question in 8.3 s and got it **wrong** — it tunnelled
    into the vault and never called `search_corpus`. Three harness fixes later it is correct
    and *slower* than the graph. Multi-part questions now go to `yoyo plan`.
13. **Voice, tasks and calendar (ADR-028).** Local STT/TTS with no audio leaving the laptop;
    the vault's checkboxes as structured tasks; read-only calendar sharing mail's OAuth app.
14. **The docs made self-checking.** The owner found §2 still describing the pre-ADR-027
    world. `tests/test_readme_matches_code.py` now fails CI when this file disagrees with the
    code, and every ADR is mirrored into `docs/adr/` so a clone explains itself.
15. **Web search (ADR-029)** through the owner's SearXNG, with an SSRF gate checked after DNS
    resolution, untrusted-content framing that deliberately does not strip injection attempts,
    and an egress log. Prompt injection entered the threat model the same day.
16. **The second brain, all five phases.** Conversations as verbatim raw sources; a wiki layer
    whose two mechanical gates (verbatim quote, and no claim may cite another wiki page) are
    what make automatic writing defensible; identity ambiguity asked rather than guessed;
    contradictions flagged rather than resolved; and finally **intent routing and spoken
    answers (ADR-030)** so the mode does not have to be chosen — routing that can only
    over-serve, and always says what it picked.
17. **Memory's model call gated.** `yoyo eval --only extraction` verifies every quote against
    the real source and requires zero claims from a conversation containing no facts. The
    layer whose failures compound is the layer that gets a gate.

### Bugs found by running it, not by reading it

**2026-08-15 — memory extracted six pages of world history and passed every gate.** The
owner's first real `yoyo memory build --dry-run` produced pages on World War I, World War II,
the West Asia conflict, Gaza, the Abraham Accords and the Israeli-Palestinian conflict:
8 proposed, 8 accepted, 0 rejected, 0 flagged, 0 ambiguous. Nothing was breached — the
verbatim-quote gate held, the no-page-cites-a-page gate held. **The input was wrong.** Half
of every conversation transcript is model output, and every accepted claim quoted *Yoyo
explaining geopolitics to Bhavin*, which memory then filed as things to remember about
Bhavin's life. `sources.render`'s own docstring had predicted this a day earlier — "that
ambiguity is exactly how a model's guess gets quoted back as the owner's decision" — and the
pipeline did it anyway. Fixed structurally: `build.evidence_from()` strips the assistant's
turns before extraction sees them, verification runs against that same text so a quote of
Yoyo cannot pass, and the extraction prompt now says general knowledge is never a memory.
The run is a shipped regression case (`extraction-ignores-world-knowledge`). **This is the
argument for dry runs over reasoning about what a system will probably do.**

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
| `bench` reported `SERIALISES (0.00x)` | Every request had 403'd — a confident wrong reading of no data. Now `NO MEASUREMENT` |
| `--role` never reached the grounded cases | Two cases were judged against the wrong model and **reported PASS** |
| `reasoning: high` on a coder role | Ollama 500 — `coder` has no thinking mode at all |
| A fabricated citation path | `file:///Users/robertovivar/.../MyAIServer.md` — a username belonging to nobody here |
| A test pinned to `endpoint == "agent"` | Broke on an *intended* promotion. Now asserts the property, not the value |
| Source tunnelling | Four vault searches, `search_corpus` never called, "not found" reported for something present |
| Completion date read as a due date | `- [x] ship it ✅ 2026-08-14` made every finished task look due the day it was finished |
| ISO fractional-second trim dropped the offset | Silently converted **every** Microsoft calendar event to UTC. Survived the first round of tests |
| The UI had no MCP tools at all | `web_search` was configured, enabled and working, and the agent replied "unknown tool 'web_search'". The CLI mounts MCP servers before every run; `api.py` never did, so the browser saw only the four built-ins |
| Invented web URLs went unnoticed | Asked for local news with no web tool, the model produced three plausible, clickable, entirely made-up news-site links. The scrubber only knew about invented *file paths* — a gap that only became reachable the day web search landed |
| A zero-result search read as absence | `mail_search("Suno charge*")` matched nothing (the wildcard), so the agent said "I found no records of Suno charges in your mail" — for a receipt `yoyo mail read` produced seconds later. The prompt's own budget rule told it to give up |
| Doctor conflated "unset" with "misconfigured" | A vault path pointing at a file reported "not configured" — reads as *you haven't set this up* when you have, wrongly |
| README §2 described the pre-ADR-027 world | Found by the owner, not by me. Now guarded by a test |

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
│                                    │  over  │   agent loop · graph · evals    │
│   serves: `agent`, `coder`, `fast` │Tailscale  MCP client + 6 MCP servers     │
│                                    │        │   voice: STT/TTS, local only    │
└────────────────────────────────────┘        └─────────────────────────────────┘
        https://tai.bombay-tint.ts.net/v1
```

Yoyo is a **pure client** with no more privilege than any teammate; its data never touches
the GB10. Code names a **role**, never a model. **Audio never leaves the laptop** — see §6.8.

### The contract

| Item | Value |
|---|---|
| Base URL | `https://tai.bombay-tint.ts.net/v1` (hostname, never the LAN IP) |
| Auth | per-client LiteLLM virtual key |
| Server timeout | 900 s — the client must be ≥ this |
| Context ceiling | **32768 tokens, hard**, whatever a model advertises |
| Concurrency | `OLLAMA_NUM_PARALLEL=4`, shared with teammates |

| Capability | Model | Speed | Tools | Concurrency | Thinking |
|---|---|---|---|---|---|
| `coder` | qwen3-coder-next 80B-A3B MoE | ~50 tok/s | **yes** — gates passed | **3.75× @ 4** (corrected 2026-08-15; first read 1.09×) | none |
| `agent` | muse-glimmer 27.9B dense Q4_K_M | ~12 tok/s | **yes** — gates passed | 3.76× @ 4 | controllable |
| `fast` | qwen3.6 35B-A3B MoE | ~76 tok/s | **NEVER** | 1.13× — serialises | on, not switchable |

**`coder` is the default for everything** since ADR-027. It passed all four hard gates 7/7
and is 4.3× faster than `agent` single-stream with no thinking tokens to pay for. `agent` is
kept as the fallback because it is the **only** capability measured to scale under
concurrency and the only one with controllable reasoning depth.

Three counter-intuitive facts behind that table, each measured after a wrong prediction:

- **`fast` is the slowest in practice** despite the highest tok/s. Thinking is on and cannot
  be switched off server-side yet, so it spends its output on reasoning traces: `coder`
  8.5 s against `fast` 23.0 s on the same eval case.
- **Being MoE predicts nothing about concurrency.** All three are MoE; only the *dense* one
  scales. Measure every new model (ADR-022).
- **`coder` has no thinking mode at all.** Setting `reasoning` on a coder role makes Ollama
  return 500. That is why the reasoning-depth roles are the `agent_*` ones.

| Role | → | Tools | Reasoning | For |
|---|---|---|---|---|
| `supervisor` | `coder` | yes | — | planning, agentic turns |
| `worker` | `coder` | yes | — | tool-using worker turns |
| `agent_supervisor` | `agent` | yes | high | fallback — deep reasoning or genuine fan-out |
| `agent_worker` | `agent` | yes | low | fallback worker |
| `answer` | `coder` | no | — | the default RAG turn — the hottest path |
| `summarize` / `extract` | `coder` | no | — | closed-context work |
| `answer_fast` | `fast` | no | — | prose fallback; revisit when `think: false` lands |

`fast` now serves only `answer_fast`. It is kept reachable rather than deleted because
`coder` is coding-tuned and its long-form prose is less proven than its retrieval — worth
re-comparing once `think: false` is set on the server.

---

## 3. Status

**✅ done and verified** · **🟡 built, not yet exercised for real** · **🔨 scaffolded** ·
**⬜ not started** · **❌ dropped**

### MyAIServer

| Feature | | Notes |
|---|---|---|
| Ollama + LiteLLM + Postgres, systemd | ✅ | survives reboot |
| HTTPS over Tailscale | ✅ | verified from the laptop |
| `agent`, `fast` and `coder` served | ✅ | `coder` added 2026-08-15 (ADR-027); `ollama list` confirms three models, 92 GB of 121 GB usable |
| Bake-off, four models | ✅ | `docs/model-baseline-gb10.md` |
| qwen3-coder-next evaluated and promoted | ✅ | 7/7 gates, 50 tok/s — ADR-027 |
| `think: false` on `fast` | ⬜ | assume thinking is on |
| Embedding model (`bge-m3`) | ⬜ | not deployed — Yoyo embeds locally |
| Reranker | ⬜ | not deployed |
| Funnel off · DHCP reservation · key rate limits | ✅ | 2026-08-15, owner-reported — not verifiable from the laptop |

### Yoyo laptop client

| Feature | | Where |
|---|---|---|
| Config layer, role registry | ✅ | `config.py`, `yoyo-models.yaml` |
| Tool-fidelity guard (raises, not warns) | ✅ | `llm.py::_guard_tools` |
| LLM client: chat, stream, 429 backoff, reasoning capture | ✅ | `llm.py` |
| `yoyo doctor` — 9 checks | ✅ | `doctor.py` |
| SQLite schema, FTS5, citations | ✅ | `migrations/`, `storage/db.py` |
| Local embeddings (fastembed, 768d) | ✅ | `embeddings.py` |
| Qdrant vectors, no orphans | ✅ | `storage/vectors.py` |
| Chunking, ingest, hash-skip | ✅ | `rag/` |
| Hybrid retrieval + RRF | ✅ | `rag/retrieve.py` |
| Turn loop with citations | ✅ | `core.py` |
| HTTP API + streaming | ✅ | `api.py` — `/ask/stream` covered against a mocked model |
| Web UI | ✅ | `static/index.html` — six views (chat, memory, map, research, tools, health) (chat, memory, map, tools, health), toasts on every failure, **browser smoke test** in `tests/ui/` |
| UI write boundary (token + Origin) | ✅ | `auth.py` — loopback is not a security boundary; see §9 |
| Job runner (long work, survives reload) | ✅ | `jobs.py`, `/jobs*` — doctor, eval, bench, ingest, remember, memory-build (dry run), backup |
| Conversation memory | ✅ | agent and graph turns persisted; follow-ups replay prior turns |
| Memory Phase 1 — conversations as raw sources | ✅ | `memory/sources.py`, `yoyo remember` |
| Memory review queue (propose → decide → apply) | ✅ | `memory/review.py` — the gates prove traceable, you decide worth keeping |
| Continuous memory — automatic sweeps | ✅ | `memory/pipeline.py`, `scheduler.py` — idle + nightly, incremental by watermark, capped by the review queue |
| Per-conversation "don't remember" | ✅ | `conversations.remember`, toggle in the chat bar |
| Memory Phase 2 — wiki layer | 🟡 | `memory/wiki.py`, `memory/extract.py` — verbatim-quote + source-kind gates |
| Memory Phase 3 — identity | 🟡 | aliases asked not guessed; enriches your own note in a marked block |
| Memory Phase 4 — contradictions | 🟡 | recorded as edges, never resolved; `yoyo memory forget` |
| Memory schema doc (governance) | ✅ | `yoyo-memory/SCHEMA.md`, regenerated each build |
| Memory extraction eval gate | 🟡 | `yoyo eval --only extraction` — built, never run against the endpoint |
| Memory Phase 5 — voice-first routing | 🟡 | `router.py` + `voice/speech.py` — `yoyo do`, `yoyo talk --mode auto`; engines still uninstalled |
| Vault map (UI) | 🟡 | `/vault/graph` + hand-rolled force layout — corpus overlay, broken links kept |
| Citation resolution endpoint | ✅ | `/citation/<id>` — chunks, mail and notes through one route |
| CLI (56 commands) | ✅ | `cli.py` — documented in §6 |
| Backup / restore drill | ✅ | `backup.py` — 11/11 on the real drive |
| Tool registry, 4 built-ins | ✅ | `tools.py` |
| Bounded agent loop | ✅ | `agent.py` — iteration + wall-clock budgets |
| Golden eval set, 10 cases | ✅ | `evals/golden.yaml` — 7/7 live on `coder`; the 3 extraction cases have **never been run live** |
| Concurrency bench | ✅ | `bench.py` — distinct prompts, 429s counted separately |
| Fabricated-citation scrubber | ✅ | `citations.py` — strips invented paths, CLI warns |
| Untried-source hint in the agent loop | ✅ | `agent.py` — fixes source tunnelling |
| MCP client adapter | ✅ | `mcp/client.py` |
| Vault MCP server | ✅ | `mcp/vault_server.py` — drafts-only write |
| Corpus MCP server | ✅ | `mcp/corpus_server.py` — live stdio round trip |
| Mail MCP server (Gmail + M365) | ✅ | `mail/`, `mcp/mail_server.py` — Gmail authed and answering live; M365 registration not done |
| Calendar adapters + MCP (read-only) | 🟡 | `calendar/`, `mcp/calendar_server.py` — enabled; awaiting `yoyo calendar auth` |
| Tasks MCP over the vault | 🟡 | `tasks.py`, `mcp/tasks_server.py` — no credentials needed |
| Filesystem MCP (third-party) | 🟡 | `yoyo-mcp.yaml` — read-only by allowlist, scoped to `Notes` |
| MCP tool allow/deny filtering | ✅ | `mcp/client.py::ServerSpec.permits` |
| Deep research (`yoyo research`, Research tab) | ✅ | `research.py` — plan → search → read → cited report, drafts-only |
| Tool guide (UI) | ✅ | `toolguide.py` + `/tools` — grouped by intent, undocumented tools shown not hidden |
| Web search + fetch (SearXNG) | 🟡 | `websearch.py`, `mcp/search_server.py` — SSRF gate, untrusted-content framing |
| Egress logging | ✅ | `data/egress.jsonl` — partial answer to OQ5 |
| STT — faster-whisper, local | 🟡 | `voice/whisper.py` — needs `pip install -e ".[voice]"` |
| TTS — Piper + Windows SAPI | 🟡 | `voice/tts.py` — SAPI needs no download |
| Push-to-talk (`yoyo talk`) | 🟡 | `voice/mic.py` — untested without a microphone |
| Obsidian vault as canon | 🟡 | `C:\Projects\Yoyo\Notes` — real folder, one note so far; the map has nothing to draw until it has linked notes |
| LangGraph supervisor graph | ✅ | `graph/supervisor.py` — plan → parallel workers → synthesise |
| Orchestration baseline measured | ✅ | ADR-026, four rounds — `plan` wins multi-part |
| README-vs-code consistency guard | ✅ | `tests/test_readme_matches_code.py` — this file fails CI when it lies |
| ADR mirror in the repo | ✅ | `docs/adr/` — all ten; the project doc stays authoritative |
| Version control | ✅ | `tethik01/yoyo-client` — commits are manual |
| PydanticAI | ❌ | rejected — structured output via `llm.py` instead |
| Langfuse observability | ⬜ | |
| Agent swarm | ⬜ | post-Phase 0 |
| Encryption at rest | ❌ | **BitLocker off — see OQ4.** Now *checked*: `yoyo doctor` fails on it, and `yoyo remember` / `yoyo memory build` refuse to run |
| Egress auditing | 🟡 | web requests logged (`yoyo web egress`); everything else still unaudited — OQ5 |

---

## 4. Next

Everything on the roadmap is now **built**. What is left is almost entirely *running it* —
and the gap between "built" and "proven" is where every bug in §1 came from.

### 1. Turn on BitLocker — 10 minutes, and Yoyo now stops until you do

```powershell
Get-BitLockerVolume -MountPoint C:            # FullyDecrypted = unprotected
# Settings > Privacy & security > Device encryption  (or Manage BitLocker on Pro)
yoyo doctor                                    # "encryption at rest" must go green
yoyo backup F:\yoyo-backups                    # re-run once encrypted
```

**Save the recovery key somewhere that is not this laptop.** If Device encryption is absent,
this is Windows Home — either upgrade to Pro for BitLocker, or accept that OQ4 stays open and
keep memory on throwaway content only.

This stopped being a note and became a control on 2026-08-15: `yoyo doctor` **fails** on an
unencrypted disk, and `yoyo remember` and `yoyo memory build` **refuse to run** (exit 3,
`--force` to override for throwaway content). The gate was your own condition; it now lives
in the machine instead of in your memory, because a condition in a README row does not hold
at 11pm three weeks from now.

This was already the gate on real mail. It is now the gate on memory as well: `yoyo remember`
puts a searchable transcript of every conversation on this disk, and `yoyo memory build` puts
structured pages about the people in your life next to it. Everything below makes that worse.
**Do not run either against real material until this is on** — that was the agreed condition.

### 2. Run the extraction gate before trusting memory

```powershell
yoyo eval --only extraction     # ~1 min, three cases, needs the tailnet
```

Memory's only model call is the extractor, and its failures are the ones that compound: a
wrong claim written today is a retrievable source tomorrow and indistinguishable from
something you actually said by next week. The gate checks every quote verbatim against the
source and checks that a conversation with no facts in it produces **zero** claims.

It has never been run live. If `coder` fails the abstain case, that is a finding worth having
before any real conversation goes through `yoyo remember`, not after.

### 3. Then actually use memory, and read what it proposes

**Re-run this after the 2026-08-15 fix — it is the open item.** The first real dry run
produced six pages of world history quoted from Yoyo's own replies (§1). Extraction now
reads only your turns. Whether that leaves anything *worth keeping* is unverified.

```powershell
yoyo remember                          # conversations become searchable, verbatim
yoyo memory build --dry-run            # see the claims before anything is written
yoyo memory build
yoyo memory show                       # what it decided you care about
```

**The open question is whether the extraction is any good**, and nothing but running it
answers that. Watch for: claims that are facts about a *conversation* rather than about an
entity, subjects that should have been merged, and anything that reads like an inference.
A high volume of trivial pages is a failure even though every quote verifies.

### 4. Ingest real material

The corpus is **3 documents**. Nothing about retrieval quality, chunk size or the context
budget has met reality, and every latency number in §6.12 was measured against a corpus small
enough to be atypical. This is the cheapest way to find out what is actually wrong.

### 5. Write some linked notes

The vault map has one note to draw. It is the surface the whole second brain is meant to show
up in, and it cannot demonstrate anything until there are notes with wikilinks between them.

### 6. Install voice and run it

```powershell
uv pip install -e ".[voice]"
yoyo voice status                      # what actually works
yoyo say "Yoyo can speak"              # Windows SAPI, no download
yoyo transcribe some-recording.m4a     # downloads the whisper model on first run
yoyo talk                              # push-to-talk, routed per turn, spoken answers
```

Two decisions only running it can settle: **whisper model size** (judge on proper nouns, not
on speed — `small` turns "Qdrant" into "quadrant") and whether the spoken answer shape is
right. The shaping is unit-tested and has never been heard.

### 7. Microsoft 365 mail and calendar — if you still want them

Gmail is live. The Entra registration was never done: App registrations → New →
Authentication → *Allow public client flows: Yes* → Graph → Delegated → `Mail.Read`,
`Mail.ReadWrite`, `Calendars.Read`. **Never `Mail.Send`.** Then `yoyo mail auth work`.

Also still pending on Google: `yoyo calendar auth personal` — the config is enabled and the
adapter is tested, but consent was never granted, so calendar has answered nothing real.

### 8. Re-test the graph in its intended case

ADR-026 is settled for two local sources. The graph has still never been measured on the case
it was built for: **mail and vault and corpus in one question.** Now that Gmail is live this
is finally runnable, and it is the test that decides whether decomposition earns its keep
generally or only on the question shape it was tuned against.

### Smaller items, any time

- **`docs/model-baseline-gb10.md`** — staleness banner added and the model list **confirmed
  against `ollama list` on 2026-08-15**: three models, 92 GB of 121 GB usable. Still needs
  `yoyo bench --role supervisor --concurrency 1,4` to fold `coder`'s numbers in
  properly — run it *after* `OLLAMA_MAX_LOADED_MODELS=3`, or a cold load pollutes round one.
  (`supervisor` is the role; `coder` is the capability behind it. `coder_supervisor` was the
  pre-promotion candidate name and no longer exists.)
- **`think: false` on `fast`**, server-side — build `qwen3.6-nothink`, register it as its own
  capability, compare. `fast` pays thinking overhead on every turn today.
- **Answer OQ7** (local vs server embeddings) while the corpus is small — either way costs a
  full reindex, and step 4 above makes that more expensive every day.
- **Re-run the ADR-026 comparison** a few more times. Every round is a single trial.
- **Watch the router for clamping.** If `yoyo route` keeps showing "(raised from ask…)", the
  classifier and the rules disagree, and one of them is wrong.
- **Raise `OLLAMA_MAX_LOADED_MODELS` to 3** so `agent`, `fast` and `coder` are resident
  together; a role switch can currently cost a cold load.
- **Memory consolidation** is deliberately not built. Ten conversations about one trip stay
  ten sets of claims. Revisit when memory is large enough for retrieval to degrade — building
  it before there is anything to consolidate would be guessing at the shape.

---

## 5. Setup

**Prerequisites:** Windows, Python 3.11+, Docker Desktop, Tailscale on the same tailnet, and
a LiteLLM virtual key for this laptop.

```powershell
cd C:\Projects\Yoyo\YoyoClient
copy .env.example .env        # fill YOYO_LLM_BASE_URL and YOYO_LLM_API_KEY
uv venv; .venv\Scripts\Activate.ps1
uv pip install -e ".[dev,local-embed]"     # add ,mail ,voice ,ingest as needed
docker compose up -d
yoyo migrate
yoyo doctor                   # the gate — nothing below is trustworthy until this is green
```

| Doctor check | Fails when | Try |
|---|---|---|
| `env` | placeholders left, timeout < 900 s, **duplicate keys** | edit `.env` |
| `server reachable` | tailnet down, bad key | `ping tai.bombay-tint.ts.net` |
| `roles` | a role points at an unserved capability | it prints what is served |
| `tool fidelity` | a `tools: true` role points at `fast` | repoint at a capability that PASSED the gates (`coder`, or `agent`) — never relax |
| `embeddings` | fastembed missing, dimension mismatch | install extra / `reindex --recreate` |
| `sqlite` | schema not applied | `yoyo migrate` |
| `qdrant` | Docker down, dimension mismatch | `docker compose up -d` |
| `vault` | `YOYO_VAULT_PATH` set but unusable | unset is fine; a path pointing at a file is not. Warns if it looks like `test-vault` |
| `optional configs` | a malformed `yoyo-mail/calendar/voice.yaml` | offline only — auth status is `yoyo mail accounts` |

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
| `yoyo remember` | Makes past conversations searchable — **Phase 1 of memory**. Verbatim only: it stores what was said and interprets nothing. `--conversation N` for one, `--min-turns N` to skip thin ones. Idempotent, content-hashed. |
| `yoyo reindex --recreate` | Drops the Qdrant collection and re-embeds **everything**. Required after changing the embed model or its dimensions in `yoyo-models.yaml` — vectors from different models are not comparable, and mixing them silently degrades retrieval rather than erroring. |

### 6.3 Asking — three escalating modes, or let Yoyo pick

These are the three ways to get an answer, cheapest first. Picking the right one matters more
than tuning any of them — which is exactly why `yoyo do` exists, and why it always tells you
what it picked.

| Command | What it does |
|---|---|
| `yoyo ask "q"` | **One retrieval, one model turn.** Retrieves passages, hands them to the model, prints the answer with numbered citations and the sources beneath. No tools, no loop. The default for "what do my documents say about X". `--role <name>` to use a different role, `--no-rag` to ask the model cold with no retrieval, `--conversation <id>` to continue a thread. |
| `yoyo agent "q"` | **Tool-calling loop.** The model chooses tools (corpus search, vault, mail, clock) and iterates until it can answer. Bounded: 8 iterations and 600 s by default, `--max-iterations N` to change. Mounts MCP servers from `yoyo-mcp.yaml` unless `--no-mcp`. Prints every tool call with ok/err so you can see the reasoning path. Use when the answer needs a live lookup, or several. |
| `yoyo do "q"` | **Routes for you.** Picks `ask`, `agent` or `plan`, prints the choice and the reason above the answer, then runs it. `--mode ask\|agent\|plan` overrides outright (no classifier call); `--rules-only` skips the classifier and uses the deterministic rules alone; `--conversation <id>` continues a thread. Say `plan: …` or "use ask mode" in the question itself and that wins. |
| `yoyo route "q"` | Shows how a question **would** be routed — mode, reason, rules floor, signals — without answering it. A misrouted answer and a bad answer look identical from outside; this separates them. |
| `yoyo research "topic"` | **Deep research.** Plans sub-questions, searches, **reads the pages**, and writes a cited report into `yoyo-drafts/`. `--depth quick\|standard\|deep`, `--no-corpus` to skip your own documents, `--no-save` to print without writing. Minutes, not seconds. Every URL was returned by a search or fetched; invented ones are stripped and the removal is reported. |
| `yoyo plan "q"` | **Multi-agent research.** A planner decomposes the question, workers run **in parallel** with their own tool budgets, a synthesiser assembles the answer. `--max-subtasks N` (default 4), `--max-parallel N` (default 3). Prints the plan and its reasoning before the answer. Use for questions spanning different sources. |

**Which one?** (ADR-026, four measured rounds)

- Single source, single part → `ask`, or `agent` if it needs a lookup.
- **Multi-part or multi-source → `plan`.** It is now both more reliable *and* faster than
  `agent` on these: measured 23.6 s against 30.1 s, and in an earlier round `agent` answered
  a two-part question **wrongly** by searching only the vault and reporting "not found" for
  the half that was in the corpus.
- Do not reach for `plan` to speed up a simple question — it is ~3x slower there.

**And if you would rather not choose:** `yoyo do` classifies for you, with one deliberate
bias — **uncertainty escalates, never downgrades.** A cheap deterministic pass (source words,
time words, multi-part structure) computes the *least* capable mode that could be right; the
model classifier may raise that and is clamped if it tries to lower it. Over-serving a simple
question costs seconds. Under-serving a multi-source one costs an answer that is fluent,
sourceless and wrong — `ask` has no tools and will answer a question about your mail anyway.
Every choice is printed with its reason, and `--mode` overrides it. Routing you cannot see is
routing you cannot correct.

| Command | What it does |
|---|---|
| `yoyo serve` | Runs the local API **and the web UI** on `127.0.0.1:8081`. Open http://127.0.0.1:8081 in a browser. Change the port with `YOYO_API_PORT`. Loopback only, no auth, not on the tailnet. This is the seam a future phone client would talk to. |

**The web UI** (`/` or `/ui`) exists for the three things the terminal could show and nothing
else could:

- **Tool calls as they arrive**, streamed over SSE while the turn runs.
- **Clickable citations.** `[7]`, `[mail:19fe...]` and `[MyAIServer.md]` all resolve through
  one endpoint and open the original. A citation you cannot follow is decoration, and until
  now following one meant a second terminal.
- **The fabricated-citation warning**, surfaced in the page rather than swallowed — a turn
  that needed the scrubber is a turn whose model invented a source.

- **The Health tab** — doctor, eval and bench as buttons, with **history**. Runs are rows in
  SQLite, so a case that flips between eval runs is visible, and every bench row carries the
  host state it was taken on. Long work runs as a **job**: start it, close the tab, come
  back, and the log replays from where it got to.

One self-contained HTML file served from the package. No build step, no npm, **no CDN** — a
UI that fetched a framework from unpkg would break on the first offline day and quietly
contradict the point of the project. A test asserts it loads nothing remote.

**Testing it.** The UI had no test at all until 2026-08-15, and it showed: a 500 left a
blank panel and a traceback in a console nobody was reading. `tests/ui/` now drives the real
page in a real browser — every view renders, a job runs end to end, an approval leaves the
queue, a bad request raises a visible toast, and any console error fails the run. It is not
collected by pytest (it needs a browser and a server); run it by hand after touching the UI:

```powershell
python tests/ui/fixture_server.py     # throwaway vault + queue on :8099
python tests/ui/smoke.py              # screenshots to /tmp/uitest/shots
```

**The UI can now start work, so it needs a lock.** Every state-changing route requires an
`x-yoyo-token` header plus a same-origin `Origin`; reads stay open. This is not about other
people using your laptop — it is that any page you visit can `fetch()` a POST at
`127.0.0.1` and, before this, would have been obeyed. The token is generated on first
`yoyo serve` into `data/ui-token` and injected into the page, so there is nothing to
configure and no unauthenticated route handing it out. A script that needs it can read the
file.

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
| `yoyo mcp serve-tasks` | Runs the vault-tasks server over stdio. Read-only; there is no tool that can tick a box. |
| `yoyo mcp serve-calendar` | Runs the calendar server over stdio. Read-only; no create, update, delete or RSVP tool exists. |
| `yoyo mcp serve-search` | Runs the web search + fetch server over stdio. **The only server that sends data out.** |

**Mounting someone else's server** is config only — see `yoyo-mcp.yaml`. One thing matters
more than the mechanics: `allow` is an **exclusive** allowlist and `deny` overrides it.
Third-party servers pick their own surface and it is usually wider than you want; the
reference filesystem server ships `write_file`, `edit_file` and `move_file` beside its read
tools. Filtering at the mount boundary means the model never learns a tool exists, which is
stronger than instructing it not to call one it can see. A test asserts the shipped
filesystem allowlist contains no write tool.

The `serve-*` commands are not meant to be run by hand — they are what another MCP client
(Claude Desktop, or Yoyo itself) launches as a subprocess. Running one in a terminal just
leaves it waiting on stdin.

### 6.5 Evaluation and measurement

The two commands that decide whether a model is allowed to do a job. Neither produces a score
you optimise; both produce a verdict you act on.

| Command | What it does |
|---|---|
| `yoyo eval` | Runs the golden set — 11 cases, 7 hard gates: **tool fidelity** (a probe tool holds an unguessable secret; the model must call it and report the value, fabricating instead is a hard fail), **tool retry** (the probe fails once — giving up after one error fails), **grounded** (the answer must cite a real chunk id and must contain no fabricated file path), **abstain** (the corpus cannot answer it; inventing an answer fails), and **extraction** (memory's only model call: every claim's quote is verified verbatim against the source, and a source with no durable facts must produce zero claims). `--only <case-or-kind>` to run one — `--only extraction` gates memory alone. Budget ~5 min for the full set. |
| `yoyo eval --role <role>` | Same gates against a different role — **this is how a candidate model gets promoted**. A model that has not passed all four gates does not get pinned to a tool-using role, regardless of how fast it is. |
| `yoyo bench --role <role>` | Measures single-stream speed and concurrency scaling for the capability behind a role. `--concurrency 1,4` sets the levels, `--repeats N` averages rounds. Reports aggregate tok/s, per-stream tok/s, scaling factor, and 429s **counted separately** so a per-key rate limit is never mistaken for the model serialising. Prints `NO MEASUREMENT` rather than a number when every request failed. |

**Run `yoyo eval --only extraction` before pointing `yoyo memory build` at real
conversations, and again after any change to the extraction prompt.** It is the only gate on
the layer whose failures compound: a wrong claim written today is a retrievable source
tomorrow and indistinguishable from something you actually said by next week. One
unverifiable quote fails the case — not a percentage, because averaging away a fabrication
in memory defeats the point of having the gate.

**Why bench exists:** concurrency is **empirical** (ADR-022). Two architectural hypotheses
were tested and both falsified — being MoE predicts nothing, and a sibling model's scaling
predicts nothing. `agent` scales 3.76x at concurrency 4 and `fast` serialises (1.13x) despite
both being MoE.

**And a scaling number is only valid for the host state it was taken on.** `coder` was
recorded at 1.09x from a single trial, then re-measured at **3.75x** over three repeats once
`OLLAMA_MAX_LOADED_MODELS=3` and `OLLAMA_KEEP_ALIVE=30m` were set on the box — four requests
in the wall clock of one, no 429s. The likely mechanism is eviction (92 GB of weights against
121 GB usable, five-minute default keep-alive), which from the client is indistinguishable
from serialisation. Unproven. What is certain is that the first number was wrong, and that
`--repeats 1` is how you get a number like it.

### 6.6 Mail

Read and draft only, by design. Consent is per-account and the OAuth flow runs in your
browser — Yoyo never sees your password.

| Command | What it does |
|---|---|
| `yoyo mail accounts` | Every configured account, its provider, whether it is enabled, and whether it is authenticated. Shows where tokens live. Start here. |
| `yoyo mail auth <name>` | Runs the OAuth consent flow for one account. Warns first that this stores a long-lived refresh token for the whole mailbox on an unencrypted disk (OQ4). |
| `yoyo mail search "q"` | Searches mail **without involving a model** — the mail equivalent of `yoyo search`. Use it to confirm auth and scopes work before debugging an agent turn. `--account <name>`, `--limit N`. |
| `yoyo mail read <id>` | Read one message in full. **This is how you resolve a `[mail:...]` citation** — paste the citation straight in, the `mail:` prefix is accepted. |
| `yoyo mail draft ...` | Save a draft (`--to --subject --body`, optional `--cc --reply-to`). It is **not sent**; it lands in your Drafts for you to review. Exposed so the draft path gets exercised on a message you chose rather than one an agent wrote unprompted. |

**Mail answers carry citations.** Every message reaches the model with a `citation` field
like `mail:19fe2cb1d4f118a3`, and answers quote it as `[mail:19fe2cb1d4f118a3]`. It is an
identifier, not a URL: Yoyo *could* build a `mail.google.com/mail/u/0/#all/<id>` link, but
`u/0` guesses which signed-in account you are and the format is undocumented — a citation
that silently opens the wrong mailbox is worse than one you paste into `yoyo mail read`.
Added after the first live mail turn answered correctly and unverifiably.

Scopes are deliberately minimal: Gmail `gmail.readonly` + `gmail.compose`; Microsoft Graph
`Mail.Read` + `Mail.ReadWrite`. **Never `Mail.Send`.** Drafts land in your mailbox for you to
review and send yourself — that asymmetry is the human-in-the-loop mechanism, not a
limitation to be removed later.

### 6.7 Web search — the one thing that sends data out

| Command | What it does |
|---|---|
| `yoyo web search "q"` | Search through your own SearXNG, no model involved. |
| `yoyo web fetch <url>` | Fetch one page and print its readable text. |
| `yoyo web egress` | **What Yoyo has sent to the internet**, with timestamps. |

Everything else in Yoyo is local. This is not, and cannot be. SearXNG helps — it proxies
your queries to Google, Bing and others, so no single vendor holds an API key tied to you
and profiles every search Yoyo makes — but the queries still leave. That is an improvement,
not a fix (ADR-029).

Three guards, all in code rather than config:

- **Private, loopback and link-local addresses are refused**, checked *after* DNS
  resolution — `evil.com` can resolve to `127.0.0.1`, and a string check waves that through.
  Without this, a fetched page could reach Qdrant, the model endpoint or your router.
  Non-`http(s)` schemes are refused too: `file:///C:/Users/...` through a web fetcher reads
  your disk.
- **Fetched pages are wrapped as untrusted input.** A page is the first place an outsider
  can write into Yoyo's context, and it may contain text aimed at the model. The content
  arrives inside an explicit marker saying it is data, never instructions. Injection
  attempts are *not* stripped — you cannot enumerate the phrasings — they are framed and
  left visible so the model can report them.
- **Every outbound request is logged** to `data/egress.jsonl`. ADR-009 promised a Squid
  audit boundary; ADR-021 lost it moving to Windows and OQ5 has been open since. This
  restores the visibility, not the control — it blocks nothing — but it answers "what has
  this thing been sending?".

**Setup gotcha:** SearXNG ships with its JSON API disabled. Add `json` under
`search.formats` in its `settings.yml` and restart, or every query returns 403 and looks
like a network fault.

### 6.8 Memory — the second brain

| Command | What it does |
|---|---|
| `yoyo remember` | **Phase 1.** Past conversations become searchable, verbatim. Interprets nothing. |
| `yoyo memory sweep` | **Read every idle conversation Yoyo has not read yet.** What the scheduler runs by itself every few minutes; run it by hand to catch up or to watch what it does. |
| `yoyo memory status` | What continuous memory has done and what it is waiting on — last sweep, conversations read, how many are queued behind the cap. |
| `yoyo memory ignore <id>` | Tell the sweep to leave one conversation alone. `--undo` to reverse. **Not** forgetting: it stops future reading and leaves what was already learned. |
| `yoyo memory propose` | Extract and queue for review, ignoring the watermark. The manual equivalent of a sweep. |
| `yoyo memory review` | The claims waiting on you, each with the quote it rests on. |
| `yoyo memory decide <id>` | Approve one, or `--reject` it. **Rejection is permanent** — a rejected claim is never re-proposed, because a queue that re-asks becomes a treadmill and a treadmill gets rubber-stamped. |
| `yoyo memory apply` | Write the approved claims. **The only path from a proposal to a page.** Refuses on an unencrypted disk (`--force` for throwaway content). |
| `yoyo memory build` | The old one-shot path: extract, verify and write in one step. Kept for throwaway testing; prefer propose → review → apply. |
| `yoyo memory build --dry-run` | **Everything except writing.** Extracts, verifies every quote, reconciles against what is on disk, and prints each proposed claim next to the quote it rests on. No page, no index, no log line. This is how you answer *"is the extraction any good"* without putting pages about your family in your vault to find out — and it is exempt from the encryption gate, because blocking the one command that lets you inspect memory would push you toward running the real thing instead. |
| `yoyo memory show [subject]` | List memory pages, or read one with its sources and quotes. |
| `yoyo memory forget <subject>` | Really deletes. `--containing` for specific claims. |

**Two filters decide what reaches the queue, and they answer different questions.**
Verification asks *did the model read this* — verbatim quote, real source, never a wiki page.
Salience (`memory/salience.py`) asks *is this about your life*: a question is not a fact,
anything about Yoyo is not a memory, and a claim needs either first-person wording or a
subject memory already knows. Every drop is logged with its reason, because a filter that
eats claims silently looks exactly like an extractor that finds nothing.

Three layers, following the pattern the owner chose ([Karpathy's gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)):
**raw sources** (immutable) → **the wiki** (Yoyo-written) → **the schema** (governance,
regenerated at `yoyo-memory/SCHEMA.md`).

**Memory writes automatically, with no approval step.** That is only safe because of one
rule, enforced in code:

> **A claim traces to a raw source. Never to another wiki page.**

Without it, a fact extracted wrongly on Monday is a source on Tuesday, cited on Wednesday,
and by Friday indistinguishable from something you said — the provenance would be *real*,
because Yoyo did read it in its own notes. Two gates close that path, neither involving
judgement:

- **Verbatim quote.** Every claim carries a quote that must appear in the source it names.
  No quote, or a quote the source does not contain, and the claim is discarded before it
  reaches a page. Same trick as the golden eval's unguessable secret.
- **Source kind.** A claim citing anything under `yoyo-memory/` is rejected outright.

What it will not do: edit your prose (only a marked block in a note you already keep),
resolve a contradiction (both claims are kept and the tension is shown — *"contradictions
are recorded as edges rather than resolved"*), or guess whether "Mom" and a name are the
same person (it asks). `log.md` is append-only, so the record of what memory did cannot be
edited by the thing that did it.

**Sources: conversations and your own notes. Mail and calendar are deliberately excluded** —
a decision, not an oversight.

### 6.9 Calendar and tasks

| Command | What it does |
|---|---|
| `yoyo calendar accounts` | Configured calendars and whether each is authenticated. |
| `yoyo calendar auth <name>` | OAuth consent for one calendar. **Reuses mail's app registration** — same Google Desktop-app client, same Entra Application ID. |
| `yoyo calendar agenda` | Today's agenda merged across every enabled account, with clashes flagged. `--day YYYY-MM-DD`, `--days N`. |
| `yoyo calendar search "q"` | Find events by text, no model involved. |
| `yoyo tasks list` | Open tasks from the vault's Markdown checkboxes, soonest deadline first. `--status open\|done\|all`, `--due-before`, `--tag`, `--contains`, `--folder`. |
| `yoyo tasks summary` | Counts only — total, open, overdue, due today, undated. |

**Calendar is read-only and will stay that way.** Mail can write drafts because a draft is
inert until you send it. A calendar has no equivalent: a "tentative" event is already on
other people's calendars and has already sent invitations. There is no way for Yoyo to
propose a meeting without acting, so it does not propose meetings. Scopes requested are
`calendar.readonly` and `Calendars.Read` — writing is impossible at the token level, not
merely absent from the code.

**Tasks are read-only too**, for the same reason the vault only accepts drafts: ticking a
box is a silent state change, and approval is meant to be a human action. Four due-date
dialects are parsed (`📅 2026-08-20`, `[due:: 2026-08-20]`, `due 2026-08-20`, bare ISO).
Relative dates are deliberately **not** guessed — a wrong deadline silently reorders what
you think is urgent.

### 6.10 Voice

Everything here runs on this laptop. No audio is sent to MyAIServer or anywhere else.

| Command | What it does |
|---|---|
| `yoyo voice status` | What is configured for speech and whether each piece actually works — asks the engines rather than trusting the config file. |
| `yoyo voice devices` | Lists microphones with their indices, for `mic.device` in `yoyo-voice.yaml`. Input devices only. |
| `yoyo transcribe <file>` | Transcribes audio locally with faster-whisper. Writes a timestamped `.transcript.md` sidecar. `--model` to compare sizes, `--ingest` to put it straight into the corpus, `--language en` to stop it guessing on short clips. |
| `yoyo say "text"` | Speaks text aloud. `--out file.wav` renders instead of playing. `--engine piper\|sapi`. |
| `yoyo talk` | Push-to-talk conversation: ENTER to start, ENTER to stop, transcribed locally, **routed per turn**, spoken back in a shape built for listening. `--mode auto\|ask\|agent\|plan` (default `auto`), `--full-text` to speak the written answer verbatim instead, `--no-speak`, `--device N`. Say "use plan mode" out loud to override a turn. |

**Why local:** audio is the most sensitive input Yoyo will ever touch — it captures people
who never agreed to be recorded by an assistant — and Yoyo's egress is unaudited (OQ5).
Sending it over the tailnet would add an unaudited flow of the most sensitive data type
available. Only the transcribed *text* reaches the model.

**Two things to know before trusting a transcript:**

- **Model size matters most on proper nouns**, which is exactly what your corpus is full of.
  `small` turns "Qdrant" into "quadrant" and "LiteLLM" into "light LLM" often enough to
  matter. Compare `--model base`, `small` and `medium` on your own audio before settling.
- **Whisper invents fluent sentences out of silence.** VAD filtering is on by default to
  trim non-speech before the model sees it. Leave it on.

**A spoken answer is not the written one read aloud.** `yoyo talk` reshapes it first
(`voice/speech.py`): citations are counted and announced ("two sources are on screen") rather
than recited, code blocks are announced rather than spelled out, markdown structure becomes
sentences, and anything past ~700 characters is cut at a sentence boundary with "the rest is
on screen". Nothing is silently dropped, and the reshaping is **pure regex, never a second
model call** — a "say this shorter" round trip would be a fresh chance to fabricate on text
whose citations have already been removed, with nothing left to check it against. The written
answer stays on screen, unchanged, and remains the source of truth. `--full-text` opts out.

In `auto` mode the route is also **spoken** before the answer ("Looking that up."). With no
screen to glance at, an unannounced routing decision is invisible, and an invisible decision
is one you cannot override.

TTS has two engines: **SAPI** is built into Windows, needs no download, and sounds like a
satnav; **Piper** is a small neural voice that sounds close to natural and needs one `.onnx`
file. SAPI is the default so `yoyo say` is never dead on arrival.

### 6.11 Backup and restore

| Command | What it does |
|---|---|
| `yoyo backup <folder>` | Snapshots SQLite + config into a timestamped zip. **Vectors are not included** — they are derived data, rebuilt by `reindex --recreate`, and including them would triple the archive for no recovery value. |
| `yoyo restore-drill <archive>` | **Proves a backup can be restored**, reading only, never touching live data. Opens the archive into a temp location, checks the schema, counts rows, verifies integrity. `--dest <folder>` uses the newest archive in that folder. Exits 1 on any failure. |
| `yoyo restore <archive> --force` | Replaces the live database from an archive. Destructive — `--force` is required. Vectors are *not* restored; run `yoyo reindex --recreate` afterwards, as the command reminds you. |

**An unverified backup is a guess.** `yoyo backup` prints the drill command for this reason.
Current status: 11/11 checks passing against the real USB drive (`F:\yoyo-backups`).

### 6.12 Latencies to expect

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
| `yoyo transcribe`, whisper `small` on CPU | roughly 2-4x realtime |
| First `yoyo transcribe` ever | + model download (~500 MB for `small`) |
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
| `yoyo-calendar.yaml` | calendar accounts — **same OAuth app as mail** |
| `yoyo-voice.yaml` | STT/TTS engines, model size, microphone |
| `yoyo-memory.yaml` | **When Yoyo reads your conversations** — idle trigger, nightly pass, and the queue cap |
| `yoyo-search.yaml` | SearXNG endpoint, fetch limits, egress logging |
| `docker-compose.yml` | Qdrant + Open WebUI, loopback only |
| `secrets/` | OAuth client secrets — gitignored |
| `data/mail-tokens/` | OAuth refresh tokens — gitignored, **unencrypted** |
| `data/calendar-tokens/` | separate from mail, so revoking one does not revoke the other |
| `data/voice-models/` | whisper + piper weights — never `%TEMP%`, which Windows cleans |
| `data/egress.jsonl` | **every outbound web request** — `yoyo web egress` |

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
12. **Audio never leaves the laptop.** Only transcribed text reaches a model. Enforced by a
    structural test — no module under `voice/` may import a network client. A future engine
    that calls out must be a separate provider with explicit opt-in.
13. **Calendar is read-only at the token level**, not merely in code. A calendar has no inert
    draft state, so every write acts on other people immediately.
14. **Yoyo never ticks a task box.** Same reason as #10: approval is a human action, and a
    silent state change is not one.
15. **Fetched web content is data, never instructions.** It is wrapped in an untrusted
    marker before the model sees it, and injection attempts are framed rather than stripped.
16. **Nothing private goes into a web query.** Enforced by prompt and tool description, not
    by code — which is why the egress log exists to check it.
17. **Fetch never reaches a private address.** Checked after DNS resolution. Not adjustable.
18. **Loopback is not a security boundary.** Every state-changing route requires the
    `x-yoyo-token` header and a same-origin `Origin`. The threat is not another machine — it
    is any page you visit, which can POST to `127.0.0.1` from your own browser. A cross-origin
    page cannot set a custom header without a preflight this server never answers, which is
    what actually closes it.
19. **The UI cannot run anything destructive.** Jobs come from an explicit registry, not a
    name lookup, and `restore`, `reindex --recreate` and `memory forget` are deliberately
    absent from it. A real `memory build` is absent too until the per-claim review exists.
20. **This file is checked against the code.** `tests/test_readme_matches_code.py` fails when
    the role table, command list, config list, or test counts drift. §13's "if it disagrees
    with the code, the file is the bug" is a test, not an aspiration.

---

## 10. Tests

```powershell
pytest -q          # 865 passing
ruff check src tests
```

| Area | Tests | Covers |
|---|---|---|
| Doctor / CLI | 95 | every command renders its help; **an unencrypted disk fails doctor, and `remember` / `memory build` refuse to run on one**; an unreadable BitLocker status says how to get a real answer instead of shrugging; tool-fidelity message no longer says only `agent`; **unset vs misconfigured vault are different verdicts**; doctor makes no network call |
| Tasks | 50 | every checkbox flavour, four due-date dialects, completion-date-is-not-a-due-date, drafts excluded, **structural proof nothing can tick a box** |
| Eval harness | 47 | fidelity gate catches a fabricating model, retry gate fails give-up-after-one-error, abstention both directions, `--role` override reaches every runner, **memory extraction gated on verbatim quotes and on abstaining from a source with no facts** |
| Calendar | 39 | ISO offsets incl. Graph's 7-digit fractions, local day bounds, conflict maths (back-to-back is not a clash), declined/cancelled exclusion, **structural proof of no write path and read-only scopes** |
| Voice | 36 | timestamp formatting, speakable-text stripping, config validation, PowerShell quoting, **structural proof no voice module imports a network client** |
| Agent / tools | 50 | arg validation, errors surfaced not raised, iteration + wall-clock budgets, forced answer on exhaustion, duplicate short-circuit, **untried-source hint**, **fabricated-path stripping** |
| MCP client | 36 | config, schema translation, result unwrapping, SDK field-name drift, failure diagnostics, live stdio round trip |
| Vault | 34 | **memory pages are on the map and invisible to search** — Yoyo cannot cite its own writing; path confinement both directions, symlink escape, frontmatter, backlinks, drafts-only writes, drafts excluded from canon |
| Web search | 42 | SSRF gate incl. DNS-resolves-to-loopback, non-http schemes refused, **untrusted-content framing kept not stripped**, egress log survives corruption, the SearXNG 403 explains itself |
| Mail | 41 | config, account resolution, Gmail/Graph parsing, HTML→text, MIME round trip, **structural proof no send path exists** |
| Structured output | 17 | schema coercion, retry on invalid JSON, the single-egress-point rule |
| README guard | 18 | **this file vs the code** — role table, capability names, command list, **flags and `--role` values shown in examples**, config files, ADR references, test counts |
| MCP servers (live) | 15 | corpus, tasks and calendar spawned over stdio; arguments really arrive; startup diagnostics say what is wrong |
| Graph | 15 | plan/dispatch/synthesise, subtask cap never silently truncates, worker gets the full question, planner splits on source difference not cost |
| Bench | 13 | distinct prompts, `NO MEASUREMENT` when every request fails, 429s not read as serialisation |
| Backup | 13 | archive contents, `.env` exclusion, drill fails on corruption and count mismatch |
| HTTP API + UI | 53 | `/ask/stream` end to end against a mocked model, RAG context reaches the prompt, empty question rejected, **no write route exists** |
| Tool fidelity | 9 | the guard raises rather than warns; the shipped yaml has no tool role on a fabricating endpoint |
| Storage | 8 | migrations, hash skip, chunk rebuild, FTS, ordering |
| Chunking | 8 | boundaries, coverage, ordinals, size bounds |
| Citations | 17 | scrubber keeps real identifiers, replaces invented paths visibly, gate and scrubber share one regex |
| Memory (wiki + review queue) | 55 | **a rejected claim is never re-proposed** and only approved claims reach a page; the approval rate is the metric; **the assistant's half of a transcript is not evidence** — a claim quoting Yoyo cannot survive verification; **a dry run writes nothing — no page, no index, no log line**; **the laundering path is closed** — no claim may cite a wiki page; verbatim quotes; contradictions flagged not resolved; owner prose untouched |
| Memory (raw sources) | 15 | verbatim transcripts, speaker labels, trivial turns skipped, **structural proof the raw layer never calls a model** |
| Intent router | 22 | **uncertainty escalates, never downgrades** — the classifier cannot route below the deterministic floor; overrides call no model; a dead classifier degrades to rules, not to an exception |
| Spoken answers (speech) | 13 | citations counted not recited, code announced not spelled out, truncation says so, **structural proof shaping only deletes and never invents words** |
| Jobs + UI auth | 39 | **`/health` reports when the running code is older than the disk**; **the literal route is declared before the parameterised one** — `/memory/proposals/subject` was being parsed as an id; **`/memory/apply` is the only route that writes a page** and there is no approve-everything route; **a POST without the token is refused** and reads stay open; a foreign Origin is rejected even with a token; a failing job is a status not a crash; a reattaching client replays what it missed; **no job kind is destructive** and memory-build from the UI is dry-run only |
| Research | 16 | **an invented URL is stripped and reported** — the highest-stakes place in this system for a fake source; a dead search says it failed to *gather* rather than implying the topic was empty; sources are interleaved so later questions are not crowded out; reports are drafts, never canon |
| Memory salience | 20 | **all six world-history claims from the live run are dropped**, verbatim as fixtures; a question is not a fact; a known subject no longer needs a possessive; **every drop carries a reason**; the filter calls no model |
| Continuous memory | 23 | **the watermark never loses a slice** when extraction fails; the queue cap pauses rather than drops; an ignored conversation is never read; **the sweep has no path to a write**; the nightly pass fires once a day, not once a tick |
| Retrieval | 6 | RRF ranking, context budget, citations |

**Not covered — assume broken until exercised:** every mail and calendar **network** path
(no OAuth in CI), every voice **engine** (no model weights or sound card), microphone
capture, and Docling extraction. Everything else above runs offline on every commit.

**Nothing here has touched MyAIServer.** The whole suite is offline by design, so it proves
the client is internally consistent and proves nothing about the server's behaviour. Live
verification is `yoyo doctor`, `yoyo eval` and `yoyo bench`, and those need the tailnet.

---

## 11. Open questions

| # | Question | Blocks |
|---|---|---|
| **4** | **Encryption at rest.** BitLocker off. Corpus, SQLite, LiteLLM key, mail **and calendar** refresh tokens, and any **audio or transcript** all plaintext. Each component added since has widened this. Deferred by owner; **test data only** while it stands. | a real corpus, real mail, real recordings |
| 5 | **Egress auditing.** ADR-009's Squid boundary doesn't exist on Windows. **Partially addressed:** web search and fetch now log to `data/egress.jsonl` (ADR-029). Everything else — the model endpoint, OAuth refreshes, fastembed downloads — is still unaudited, and nothing is *blocked*. | nothing, but must not be implicitly answered |
| 7 | **Embeddings local or server.** Costs a reindex either way — cheapest to decide now. | — |
| 8 | **Golden eval set** covers current pins only. Reopened and closed for `coder` (7/7, ADR-027); reopen again for any new pin. Three **extraction** cases added 2026-08-15 and **not yet run live**. | future pins |
| **10** | **Is the extraction any good?** **First real answer, 2026-08-15: no — and the gates could not see it.** Six pages of world history, 8/8 accepted, every quote verifying. Cause was the input, not the gates: claims quoted Yoyo's own replies. Fixed (owner-turns-only evidence, plus a "general knowledge is never a memory" rule) and **the fix is unverified against a real run.** Re-open until a dry run over real conversations produces something worth keeping. | trusting memory; deciding whether consolidation is needed |
| 9 | **Whisper model size.** `small` is a config default, not a measured answer. Needs a comparison on real audio, judged on proper nouns. | trusting a transcript |
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
| ADR-028 | Voice runs locally on the laptop; calendar is read-only and shares mail's OAuth app |
| ADR-029 | Web search through self-hosted SearXNG; egress logged, not blocked |
| ADR-030 | Intent routing that can only over-serve; spoken answers get their own shape |

Authoritative log: the Claude project docs `yoyo-architecture-decisions-2026-08-14.md` and
`yoyo-open-questions-ledger.md`. **`docs/adr/` now mirrors every one of them** so a clone explains
itself — the project doc still wins on conflict, and the mirror is dated.

**Void from the original plan:** `plan-gb10.md` §1–§4, Phase-0 T2/T3/T4/T-tenancy,
ADR-020's `shared-llm` mechanics, ADR-012-GB10's memory contract, ADR-002-GB10's engine
ordering. **ADR-017-GB10 voice** (CPU-pinned containers on the box) is superseded by
ADR-028, not merely void.

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
| 2026-08-15 | **qwen3-coder-next promoted** (ADR-027): 7/7 gates, 50 tok/s vs `agent`'s 11.7. Recorded as serialising (1.09x) — **later corrected to 3.75x**, see below. `reasoning` must never be set on a coder role (Ollama 500). |
| 2026-08-15 | Fabricated citation path observed live twice on `coder`. Fixed in four prompts, a mechanical eval gate, and `citations.py` — the interactive path strips and warns, the gate still fails. |
| 2026-08-15 | **ADR-026 reversed.** On `coder`, `yoyo agent` answered a two-part question in 8.3 s and got it **wrong** — tunnelled into the vault, never called `search_corpus`. `yoyo plan` got it right in 23.6 s. |
| 2026-08-15 | Source-tunnelling fixed: untried-source hint at 2 calls, plus a prompt rule that "not in the notes" ≠ "not there". Patched agent now answers both parts correctly — in 30.1 s, still losing to the graph's 23.6 s. **ADR-026 confirmed on round 4.** |
| 2026-08-15 | `PLANNER_INSTRUCTION` rewritten: the "roughly three times slower" warning was measured on `agent` and is false on `coder`. Split criterion is now source difference, and "when unsure, SPLIT". |
| 2026-08-15 | README §6 expanded into a full command reference — every command, what it is for, and which of the three asking modes to reach for. 256 tests. |
| 2026-08-15 | **Voice built** (ADR-028): faster-whisper STT, Piper + Windows SAPI TTS, `yoyo transcribe / say / talk`. Entirely local — no audio crosses the tailnet, because audio is the most sensitive input Yoyo handles and egress is unaudited (OQ5). |
| 2026-08-15 | **Tasks MCP**: the vault's Markdown checkboxes as structured, filterable items. Four due-date dialects parsed; no relative-date guessing. Read-only — Yoyo does not tick boxes. |
| 2026-08-15 | **Calendar adapters + MCP** (read-only): Google + Graph, sharing mail's OAuth app registration. No write path at all — a calendar has no inert draft state the way mail does. |
| 2026-08-15 | Bug caught by its own test: the ISO fractional-second trim dropped the trailing offset, silently converting every Microsoft event to UTC. 379 tests. |
| 2026-08-15 | README §2 corrected: "The contract" still listed only `agent` and `fast` and showed the pre-ADR-027 role map. Found by the owner, not by me — a doc that disagrees with the code is the bug, and this one had been wrong since the promotion. The `yoyo-models.yaml` header carried the same stale rule ("any tools:true role MUST point at `agent`") and is fixed too. |
| 2026-08-15 | **README made self-checking.** `tests/test_readme_matches_code.py` (16 tests) asserts this file against the code: role table vs `yoyo-models.yaml`, all 36 commands documented both ways, config files, MCP-server count, ADR references, and test counts. Written because the §2 drift was found by the owner rather than by CI. |
| 2026-08-15 | Audit of the whole README found more than the one section: §1 stopped at day 2, the bugs table missed ten entries, §5 repeated the stale `agent` rule, §9 was missing four invariants, and §10's counts were wrong in nine rows with two files absent. All corrected against measured values. |
| 2026-08-15 | `/ask/stream` covered against a mocked model (13 tests) — it was the last "assume broken" item that could be closed offline, and it is the seam a phone client would use. |
| 2026-08-15 | Live stdio round trips for the corpus, tasks and calendar MCP servers (15 tests). Only the vault server had one, and the worst bug in this project — arguments silently arriving empty — was invisible to unit tests. |
| 2026-08-15 | `yoyo doctor` gained `vault` and `optional configs` checks and its first tests (57 incl. a help smoke test over all 36 commands). Fixed while testing: it reported a misconfigured vault path as "not configured". |
| 2026-08-15 | All eight ADRs mirrored into `docs/adr/`; `.env.example` and `.gitignore` extended for voice and calendar. **480 tests.** |
| 2026-08-15 | **Gmail OAuth live.** First real mailbox connected (personal Gmail). Two setup traps recorded: an app left in Google's "Testing" status expires refresh tokens after 7 days — publish to production instead — and the test-user list must match the account you actually sign in with. |
| 2026-08-15 | Fixed two bad diagnostics found by doing it for real: `yoyo mail auth` succeeded on a `enabled: false` account and said nothing, then the next command reported "No mail accounts configured" — for accounts that existed and were authenticated. Auth now warns at the moment it can be acted on, and `resolve()` distinguishes *disabled* from *absent*. 483 tests. |
| 2026-08-15 | **First live agent turn over real mail** — correct amount, date and invoice number in 36 s. It also had no citation, so it was unverifiable: mail now carries `[mail:<id>]`, resolvable with `yoyo mail read`. An identifier rather than a URL, for the same reason the agent is forbidden from constructing links. |
| 2026-08-15 | `yoyo mail draft` exposed on the CLI so the draft path is exercised deliberately, on a message the owner picked, before an agent ever writes one. |
| 2026-08-15 | **False negative on real mail**, caused by my own prompt rule. "Do not re-run a search with reworded terms; if a search returns nothing useful, say so" was written to stop budget waste and did not distinguish *found things, none relevant* from *found literally nothing*. A wildcard query matched zero and the agent reported the receipt did not exist. Fixed in the prompt and mechanically: an empty tool result now carries a note saying zero results is evidence about the QUERY, with one broad retry allowed. 509 tests. |
| 2026-08-15 | **Web search built** (ADR-029) over the owner's self-hosted SearXNG. Three guards in code, not config: an SSRF gate checked *after DNS resolution* (`evil.com` can resolve to 127.0.0.1), untrusted-content framing that deliberately does not strip injection attempts, and an egress log at `data/egress.jsonl`. **OQ5 moves ❌ → 🟡** — visibility restored, control not; it blocks nothing and only covers web traffic. |
| 2026-08-15 | Prompt injection enters the threat model. Yoyo now has read access to a real mailbox *and* fetches attacker-controlled text. The write asymmetries that looked like ergonomics — no mail send, drafts only, no calendar writes — are now load-bearing security controls. 558 tests. |
| 2026-08-15 | **Web UI built** — one self-contained HTML file served by `api.py` at `/`. Shows tool calls streaming over SSE, makes every citation clickable through a new `/citation/<id>` route that resolves chunks, mail and vault notes alike, and surfaces the fabricated-citation warning in the page. No build step and no CDN; a test asserts the page loads nothing remote. 568 tests. |
| 2026-08-15 | Two of my own tests were wrong again in the same way — banning a substring that appeared in the file's own explanatory comment. Both rewritten to assert behaviour: the UI test now checks what the page *loads* (src/href/fetch targets), not what it mentions. |
| 2026-08-15 | Default API port moved 8080 → **8081** — 8080 was already taken on the owner's machine, and it is the most-contested port on any dev laptop. `yoyo serve` now checks the port first and exits with the fix (`YOYO_API_PORT`) instead of a bare winerror, and prints the UI URL on start. |
| 2026-08-15 | **URL provenance.** A URL may now appear in an answer only if a tool put it there — checked against every tool result of the turn (and against the retrieved passages on the `ask` path, which has no tools at all). Provenance, not a blocklist: no opinion about which domains are real. Found because the UI made an invented answer easy to look at. |
| 2026-08-15 | **The API now mounts MCP servers at startup**, which it had never done. `/health` reports the reachable tool names and any server that failed to mount, and the UI shows the count in its header — the missing fact that turned a one-line config truth into a confusing model error. 585 tests. |
| 2026-08-15 | **Conversation memory.** Agent and graph turns were never persisted at all — a refresh lost everything and a follow-up ("and what about tomorrow?") reached the model with no idea what came before. The SQLite tables have existed since the first migration; nothing passed a `conversation_id`. Now: lazy conversation creation, auto-titling from the first question, prior turns replayed to the model (user/assistant text only — replaying stale tool calls would re-teach tools already called), and a history sidebar. |
| 2026-08-15 | API tests now run against a real migrated temp database. They stopped being pure the moment turns started persisting, and mocking the store would have left the persistence path untested exactly when it began to matter. 593 tests. |
| 2026-08-15 | Vault pointed at a real folder (`C:\Projects\Yoyo\Notes`); vault, tasks, search and mail MCP servers all live. Only calendar remains disabled, pending its OAuth. |
| 2026-08-15 | **Duplicate `.env` keys now fail `yoyo doctor`.** Found live: two `YOYO_VAULT_PATH` lines, the stale scaffold path and the real one. dotenv takes the last, so it worked — but which value was in force was invisible to a reader, and the losing line looked just as authoritative. Same class as a doc disagreeing with the code, same fix. |
| 2026-08-15 | **ADR-026 round 5 — three sources, and a new failure.** `yoyo agent` (31.8 s) answered *"your notes describe the GB10…"* and cited a **corpus** document; the vault held one empty file. `yoyo plan` (51.3 s) correctly reported the notes contained nothing and said how it established that. Slower and more honest. Sixth variant of confidently-wrong: not absence unestablished, but **presence attributed to the wrong source**. Neither called `web_search` for the "current spec" part. |
| 2026-08-15 | **Vault map in the UI.** Notes and `[[wikilinks]]` as a force-directed graph, hand-rolled (no CDN, no d3). Overlays which notes the corpus has ingested — the exact vault/corpus distinction the agent got wrong. Links to unwritten notes are kept as nodes, as Obsidian does. |
| 2026-08-15 | Third time a test policed a string rather than behaviour: `/vault/graph` failed a test banning any path containing "/vault". The invariant was never "no vault routes", it is "no vault WRITES" — now checked by HTTP method. |
| 2026-08-15 | **`USER-GUIDE.md` written** — how to drive the system rather than what is built. The three modes and when each is wrong, vault vs corpus (the distinction the assistant itself got wrong), following citations, and a frank section on the six confidently-wrong failures found so far. |
| 2026-08-15 | **MCP tool allow/deny filtering**, added while wiring the reference filesystem server. It ships write tools beside its read tools; mounting it whole would have handed an agent write access to a folder and voided invariant #10 without anyone deciding to. `allow` is exclusive, `deny` wins, withheld tools are logged rather than vanishing silently. Filesystem server now live, read-only, scoped to `Notes`. |
| 2026-08-15 | Calendar account and MCP server enabled, sharing mail's OAuth client. `YOYO_CALENDAR_CONFIG` added so the server's "no accounts configured" test points at a temp file — reading the shipped config made it pass or fail by whether the developer happened to have a calendar set up, which is no check on behaviour. It broke the moment a real account was enabled, which is how the flaw showed. |
| 2026-08-15 | **Second-brain roadmap agreed** (project doc `yoyo-second-brain-roadmap.md`), adopting Karpathy's three-layer wiki pattern: raw sources → LLM-written wiki → governing schema. The owner chose auto-write over a review queue, and that is defensible *because of the pattern's own rule* — **a claim must trace to a raw source and never to another wiki page**, so a fabrication cannot compound. Sources: conversations and the owner's own notes. Mail and calendar deliberately excluded for now. |
| 2026-08-15 | **Memory Phase 1** — `yoyo remember` makes past conversations searchable as verbatim corpus documents, speaker-labelled and timestamped. Deliberately dumb: a structural test asserts the raw-source layer never calls a model, because everything the wiki layer writes later must quote text no model generated. Also `ingest_text()`, so non-file sources reuse the whole existing chunk/embed/cite pipeline rather than growing a second retrieval path. |
| 2026-08-15 | **Memory Phases 2–4.** Wiki layer with two mechanical gates — verbatim quote, and no claim may cite another wiki page. Extraction is the single place memory calls a model, and the source is set by the caller so a model can never name its own evidence. Identity ambiguity is asked, never guessed. Contradictions are recorded as edges, not resolved. `SCHEMA.md` regenerated into the vault so the rules are readable where the memory lives. |
| 2026-08-15 | **Memory Phase 5 — routing and spoken answers.** `yoyo do` / `yoyo talk --mode auto` pick the mode. The design constraint: the mode choice was doing safety work, so routing may only over-serve. A deterministic pass computes a floor and the classifier is clamped to it — it can escalate, never downgrade — and a classifier that is down degrades to the rules rather than failing the turn. Every choice is printed with its reason, `--mode` overrides, and the UI offers one-click redo in another mode. Speech gets its own shape (`voice/speech.py`), regex-only so it can delete but never invent. |
| 2026-08-15 | **The dry run earned its keep on the first use.** Six pages of world history, every gate green — see §1. Memory now extracts from **the owner's turns only**: `build.evidence_from()` strips the assistant's half of a transcript before extraction, and verification runs against that same text, so a claim quoting Yoyo cannot pass. The eval runner applies the identical split, because a gate that tests a path production does not take is how this got through in the first place. Extraction's prompt gained the rule the shape of the failure taught: *if a fact would be equally true for a stranger, it says nothing about its owner.* |
| 2026-08-15 | **`yoyo memory build --dry-run`.** Extracts, verifies, reconciles and prints every proposed claim next to its quote — writing no page, no index and above all no log line, since the append-only log's only value is being an accurate record. It exists because the open question about memory is not "does it work" but "is what it extracts worth keeping", and that cannot be answered by a count. Caught by the same review: the README had been telling the owner to run `--dry-run` for two days before the flag existed, so the README guard now checks **flags**, not just command names. |
| 2026-08-15 | **The encryption gate was not gating.** On the owner's machine the BitLocker probe returns `Get-CimInstance : Access denied` — reading volume status is a privileged API, so an ordinary PowerShell gets UNKNOWN every time, and UNKNOWN was treated as "carry on". The refusal branch had never once fired on the machine it was written for. No cleverer probe exists; the honest fix is to say so, so doctor's detail now names elevation as the way to get a real answer and `remember` / `memory build` print "could not confirm this disk is encrypted" before proceeding. Silence and a clean bill of health must not look the same. |
| 2026-08-15 | **A third bug, caused by the fix for the second.** The owner reported "keep all" still failing after the route-ordering fix shipped — and it was: `api.ui()` re-reads `index.html` from disk on **every request**, while Python modules are imported once. He got the new front end (with its now-readable error message, which is how we could see the request was still hitting the wrong route) talking to the old backend. Nothing was wrong with the fix; the server had not been restarted, and nothing told him so. `/health` now compares the newest `.py` mtime on disk against what the process loaded, and the UI shows a persistent banner when they disagree. A system that can see it is running stale code has no excuse for letting you debug around it. |
| 2026-08-15 | **Two bugs from one click on "keep all", found by the owner.** FastAPI matches routes in declaration order, so `POST /memory/proposals/subject` was captured by `/memory/proposals/{proposal_id}`, "subject" failed to parse as an int, and the request 422'd. The *second* bug is why that was hard to read: FastAPI's validation errors put a LIST of objects in `detail`, and the UI's `body.detail` rendered it as **"[object Object]"** — an error message you cannot read is the same failure as no error message, one step further along. Fixed both, plus a structural test that a route re-added above the literal one fails in CI rather than in the browser. |
| 2026-08-15 | **`yoyo research` — a topic in, a cited report out.** Plan sub-questions, search, **actually read the top pages**, write a report with a sources section assembled by code rather than by the model. It fans out across sub-questions, which is a design that would have been pointless a day earlier: `coder` was recorded as serialising at 1.09x and re-measured at 3.75x. Provenance is enforced hardest here — a research report is the highest-stakes place in this system for an invented source, because a page of confident prose with a references section is exactly the shape people stop checking. Reports land in `yoyo-drafts/`, never in the vault proper. |
| 2026-08-15 | **The Tools page became a guide.** A tool's `description` is written for the model — terse, imperative — and tells a person nothing about when to reach for it or what it will refuse. `toolguide.py` adds curated copy grouped by *what you are trying to do* rather than by which server provides it, with an example you could actually say and the caveat that matters (`vault_search` cannot see Yoyo's own pages; `mail_draft` has no send path; `web_fetch` refuses private addresses after DNS resolution). A tool with no entry is shown as **undocumented rather than hidden** — silently omitting it would recreate, one level down, the exact gap the page exists to close. |
| 2026-08-15 | **Memory became continuous.** A watermark per conversation (`messages.id` is monotonic, so "read up to N" is the whole state) makes sweeps incremental; a scheduler thread queues one when a conversation has been quiet ten minutes, plus a nightly catch-up for the evenings the laptop was shut. The brake that matters is the **queue cap**: compute is not the constraint, attention is, and a queue you cannot clear gets rubber-stamped. At the cap the sweep pauses and says so, advancing no watermark, so clearing the queue resumes it exactly where it stopped. Approving now writes the page immediately — approval is the judgement, writing is bookkeeping — and any conversation can be told **"don't remember this"**, which stops future reading without unsaying what was already learned. |
| 2026-08-15 | **A salience filter, built from the run that needed it.** The six world-history claims are now regression fixtures. Four mechanical rules: a question is not a fact, anything about Yoyo is not a memory, a claim needs first-person wording or a subject memory already knows, and a two-word quote is not evidence. No second model call — a model judging its own output is one more thing to distrust — and every drop carries a reason, because silent filtering and an empty extractor look identical from outside. |
| 2026-08-15 | **A Tools page, and conversations can be deleted.** The health pill had read "34 tools" for days with no way to see which 34 — the same complaint as an uncitable answer, and exactly how a correctly configured `web_search` went unnoticed for an afternoon. Clicking the count now opens the list, with each tool's description, arguments, and which MCP server it came from, plus a table of mounts and their failures. |
| 2026-08-15 | **The UI failed silently, and now cannot.** Clicking "Run doctor" returned a 500 — `no such table: jobs`, because `yoyo migrate` had not been run — and the page showed *nothing*. Three fixes, in order of how much they matter: the API **applies pending migrations at startup** (a schema the code needs is not a decision to delegate to someone who is trying to do something else); every unhandled error returns JSON with a message instead of an HTML 500; and the UI raises a **toast** for every failure. Then the redesign: sidebar navigation, stat tiles, per-view empty states that distinguish "nothing here" from "could not load", light and dark, and a **browser smoke test** — the UI was the only part of this project with no test, which is exactly why this one got through. |
| 2026-08-15 | **The review queue, and the hole it closed on the way.** `vault.py` excluded `yoyo-drafts/` from search but not `yoyo-memory/` — so while extraction had always refused to *read* a wiki page as a source, `vault_search` could still find one and hand it to the model as "your notes". The same circularity through the other door, and worse: at write time a bad quote fails a check, at read time the quote is genuinely there and nothing mechanical could catch it. Memory pages are now invisible to search and still visible on the map, because drawing a node is not citing it. Then the queue itself: extract → propose → you decide → apply, with `/memory/apply` the single write path, rejection permanent (a re-asking queue is a treadmill, and a treadmill gets rubber-stamped), and the **approval rate** surfaced as the metric that says whether review is working at all. |
| 2026-08-15 | **The UI can run things now** — a job runner (`jobs.py`, `/jobs*`) plus a Health tab. Doctor, eval, bench, ingest, remember, dry-run memory build and backup are jobs: rows in SQLite that survive a reload, replay their log to a reattaching client, and record the *arguments* next to the result. That last part is the direct lesson of the 1.09x → 3.75x reversal — a bench row now carries the host state it was taken on, because a measurement without its inputs is an anecdote. Eval history is per-case across runs, so a gate that flips is visible rather than a thing you notice three days later. |
| 2026-08-15 | **A write boundary before the first write route** (`auth.py`). Loopback stops other machines, not other origins: any page you visit can POST to `127.0.0.1`, and it needed to stop being able to the moment a POST could ingest files. Token header + Origin check, token injected into the page rather than served from a route. Jobs come from an explicit registry — `restore`, `reindex --recreate` and `memory forget` are not in it, and will not be. |
| 2026-08-15 | **`coder` does not serialise after all — 1.09x was wrong.** Re-measured over three repeats after `OLLAMA_MAX_LOADED_MODELS=3` and `OLLAMA_KEEP_ALIVE=30m`: **3.75x @ 4**, 14.8 → 55.5 aggregate tok/s, four requests in the wall clock of one, zero 429s. The consequence drawn from the old figure — "the graph loses its fan-out benefit" — is withdrawn; `plan` fan-out pays on the default pins. Cause unestablished: 92 GB of weights against 121 GB usable with a five-minute keep-alive means eviction and serialisation look identical from the client. The original was a single trial. **ADR-022 extended: concurrency is empirical per model *and per host configuration*, and a scaling number without the box state it was taken on is not a measurement.** |
| 2026-08-15 | **A role named in §4 never existed.** The doc told the owner to bench `coder_supervisor` — the candidate's label before ADR-027 promoted it; the registry has only `supervisor`. The command exits 2 for anyone following the doc. The README guard now checks `--role` values against `yoyo-models.yaml` — the third rung of the same ladder after command names and flags. |
| 2026-08-15 | **OQ4 given teeth.** `yoyo doctor` gained an `encryption at rest` check that reads BitLocker's volume status for C: and **fails** when it is not encrypted, and `yoyo remember` / `yoyo memory build` refuse to run on an unencrypted disk (exit 3, `--force` for throwaway content). Reads status only — enabling encryption is a system security change and stays the owner's. Unknown status *passes*: a check that cannot run is not evidence of absence, which is the one mistake this project keeps finding new variants of. |
| 2026-08-15 | **Memory extraction gated** (`yoyo eval --only extraction`). Three cases against the real verifier: quotes must verify verbatim, a source with no durable facts must yield zero claims, and an inference trap ("Sarah called about the review") where the only honest answer is nothing. Recall is deliberately the softest of the three — a model that extracts everything scores perfectly on recall and is unusable. |
| 2026-08-15 | Caught while building: my first contradiction design struck through the older claim, which needed a string heuristic to decide two claims were "the same fact" — and one crude enough to catch *lives in Toronto/Lisbon* also catches *is my sister/is moving in March*, silently striking a true fact. Replaced with flagging, per the pattern's own rule. Also: `SCHEMA.md` was being read back as an entity called "schema"; a page must now declare `about:` to count as one. |

# ADR-021: Split Yoyo into a model server and a laptop stack

- **Status:** ACCEPTED (owner decision, 2026-08-14)
- **Supersedes in part:** ADR-020, and §1–§4 of `plan-gb10.md`
- **Revision 2** — revision 1 of this file was written before `yoyo-client-handoff.md` was
  available and named client-side capabilities that do not exist (`large`, `tools`, `embed`,
  `rerank`). Corrected below.

> **This is a repo mirror.** The authoritative ADR log lives in the Claude project
> (`yoyo-architecture-decisions-2026-08-14.md`), which also carries ADR-022 through ADR-025.
> The endpoint contract is `yoyo-client-handoff.md`; the measurements are
> `docs/model-baseline-gb10.md`. On conflict, those win over this summary.

## Context

The GB10 plan docs assume all of Yoyo runs on the box: compose stack, LUKS-encrypted
`/srv/yoyo`, Squid egress gateway, Qdrant, SQLite, MCP servers, voice, inference. The
2026-08-13 build ended elsewhere. The box — an ASUS Ascent GX10, host `TAI`, DGX OS 7.5.0 —
became a model server only: Ollama on loopback, LiteLLM v1.95.0 with Postgres-backed keys,
authenticated HTTPS over Tailscale, shared with 3–5 teammates.

## Decision

| Tier | Runs on | Contains |
|---|---|---|
| MyAIServer | ASUS Ascent GX10 | Ollama, LiteLLM, Postgres, Tailscale endpoint |
| Yoyo | Windows laptop | Corpus, SQLite, Qdrant, RAG, orchestration, MCP servers, UI |

Contract: LiteLLM's OpenAI-compatible API, per-client virtual key. Yoyo is a **pure client**
with no more privilege than any teammate; its data never touches the GB10.

The endpoint serves exactly two capabilities, **`agent`** and **`fast`**. Yoyo code names a
**role** — `supervisor`, `worker`, `answer`, `summarize`, `extract` — and `yoyo-models.yaml`
maps roles to capabilities. Model identity appears nowhere in code.

Laptop runtime: native Windows Python; Docker Desktop for Qdrant and Open WebUI.

## Voided by this decision

`plan-gb10.md` §1–§4 · Phase-0 T2-GB10/T3/T4/T-tenancy · ADR-020's `shared-llm` Docker
mechanics (the goal is now met by LiteLLM virtual keys — better; the isolation claim
survives, the mechanism does not) · ADR-012-GB10's memory contract (sized around a resident
gpt-oss:120b that is gone, on a box Yoyo no longer runs on) · ADR-002-GB10's engine ordering
(see ADR-024) · ADR-017-GB10 voice.

## Consequences

**Gained.** Model and backend swaps are invisible to Yoyo. The endpoint serves the whole
team. Development happens where the editor and corpus already are.

**Lost, deliberately:**

- **ADR-009's read-only egress guarantee no longer holds.** Squid SSL-bump with per-identity
  ACLs was a Linux/compose control on a box Yoyo owned. On Windows, Yoyo's outbound traffic
  is **unaudited**. Open Question #5.
- **ADR-014 encryption at rest does not apply.** No LUKS. Corpus, SQLite and keys sit on the
  laptop filesystem; if BitLocker is off, nothing is encrypted. Open Question #4 — **blocks
  the first real ingest.**
- **Phone clients are not enabled by this**, despite being the motivation for wanting
  decoupling. With corpus and RAG on the laptop, a phone must talk to the laptop, making it
  an always-on tailnet service. The three-tier split is **deferred, not rejected** — all
  business logic sits behind `src/yoyo/api.py` and none in the CLI precisely to keep that
  extraction cheap.
- **Voice unaddressed.** **M-tenancy** becomes a MyAIServer gate, not a Yoyo one.

## Rejected alternatives

- **Keep everything on the box.** Rejected by the owner: development friction, and the box is
  more valuable as a shared service than a single-app host.
- **Three tiers now.** Premature. Revisit when a phone client is actually being built.

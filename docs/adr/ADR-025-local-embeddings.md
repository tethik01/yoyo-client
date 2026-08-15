# ADR-025: Embeddings run locally until the endpoint exposes one

> Mirrored from the Claude project decision log on 2026-08-15. The project doc is
> authoritative; if they disagree, this mirror is the bug.

- **Status:** ACCEPTED (2026-08-14)

`bge-m3` and `bge-reranker-v2-m3` are named in the plan but **are not deployed** on
MyAIServer. The laptop's key is scoped accordingly — an embedding call returns 403.

**Decision.** Yoyo embeds locally on the laptop CPU (fastembed, `BAAI/bge-base-en-v1.5`,
768 dimensions) rather than blocking Phase 0 on a server change. Sanctioned by the handoff.
Reranking stays disabled; fusion order stands.

**Consequences.** Embeddings keep working when the tailnet is down — a genuine robustness
gain. Against that, model configuration now lives in two places, exactly what ADR-019 wanted
to avoid; accepted as temporary. Flipping to `provider: server` is a yaml edit plus a
**full corpus reindex**, since changing the embedding model invalidates every vector.
Recorded as open question 7, not settled doctrine.

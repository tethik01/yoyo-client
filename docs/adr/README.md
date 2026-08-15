# ADR mirror

The authoritative decision log lives in the Claude project as
`yoyo-architecture-decisions-2026-08-14.md` and the per-decision `yoyo-adr-0NN-*.md` docs.
This folder mirrors them into the repo so a clone is self-contained — a reader with the code
and no access to the project can still find out *why*.

**If the two disagree, the project doc wins** and this mirror is the bug. Mirrored
2026-08-15.

| ADR | Subject | Status |
|---|---|---|
| 021 | Two-tier split: model server vs laptop stack | ACCEPTED |
| 022 | Sparsity predicts speed; concurrency is empirical; thinking is costly | ACCEPTED |
| 023 | Tool-call fidelity is a hard constraint | ACCEPTED |
| 024 | gpt-oss:120b dropped; vLLM deferred; SGLang a liability | ACCEPTED |
| 025 | Embeddings run locally until the endpoint exposes one | ACCEPTED |
| 026 | Graph vs single agent — reversed on round 3, confirmed on round 4 | ACCEPTED, REVISED |
| 027 | qwen3-coder-next promoted to the tool-using roles | ACCEPTED |
| 028 | Voice is local; calendar and tasks are read-only | ACCEPTED |

Inherited ADRs not restated here (009 egress, 012 memory, 014 encryption, 017 voice, 020
tenancy, and the canon set) are binding except where an ADR above supersedes them. ADR-021
§"What this voids" and README §12 list the voided parts.

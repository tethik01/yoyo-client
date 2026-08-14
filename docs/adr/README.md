# Decision log

The **authoritative** log is the Claude project doc
`yoyo-architecture-decisions-2026-08-14.md`, which overlays
`yoyo-architecture-decisions-gb10.md`, which overlays canon. Newest overlay wins on conflict.

Only ADR-021 is mirrored here, because it is the one a developer reading this repo needs
immediately. The rest are summarised below — read the project log for the full text.

| ADR | Subject |
|---|---|
| ADR-021 | Two-tier split: model server vs laptop stack. Mirrored in this folder. |
| ADR-022 | Sparsity predicts generation speed; concurrency is empirical and must be measured per model; thinking is on by default and costly. |
| ADR-023 | Tool-call fidelity is a hard constraint. `fast` must never receive tools — enforced in `llm.py::_guard_tools`, which raises. |
| ADR-024 | gpt-oss:120b dropped on measurement; vLLM **deferred, not rejected**; SGLang a maintenance liability. |
| ADR-025 | Embeddings run locally (fastembed) until MyAIServer exposes one. |

Evidence for ADR-022/023/024 is in `../model-baseline-gb10.md`.
The endpoint contract is `../../yoyo-client-handoff.md`.

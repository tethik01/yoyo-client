# ADR-026: Graph vs single agent — reversed on round 3, confirmed on round 4


- **Status:** ACCEPTED, REVISED (2026-08-15, four live runs)
- **Depends on:** ADR-027

Identical two-part question throughout, spanning the live vault and the ingested corpus.

| Round | Config | Model | Wall clock | Tool calls | Answer |
|---|---|---|---|---|---|
| 1 | `yoyo agent` | agent | 148 s | 5 | correct |
| 1 | `yoyo plan` | agent | 360 s | 13 | hedged; one subtask refused |
| 2 | `yoyo plan` | agent | 446 s | 14 | correct |
| 3 | `yoyo plan` | coder | **23.6 s** | 3 | correct |
| 3 | `yoyo agent` | coder | 8.3 s | 5 | **WRONG — second half missing** |
| 4 | `yoyo agent` | coder, patched | **30.1 s** | 7 | correct |

**Round 3 reversed the original decision.** The single agent was 2.8x faster and factually
wrong: it ran `vault_search` four times with reworded queries, never called
`search_corpus` (which was registered and available), and reported the corpus half as "not
found". It tunnelled into one source and reported absence-there as absence-everywhere.

The graph got it right structurally, not by being smarter: each worker started with a clean
budget and no prior commitment to a source.

**Three fixes shipped:** an untried-source hint at the second call to one tool
(`REPEAT_HINT_AFTER = 2`), a prompt rule that "not in the notes" is not the same claim as
"not there", and a mechanical fabricated-citation scrubber (`citations.py`).

**Round 4 confirmed the reversal.** The patched agent crossed to `search_corpus` exactly
where the hint fires and answered both parts — in 30.1 s, losing to the graph's 23.6 s. Once
forced to cover both sources it does the graph's work without the parallelism.

**Decision.** Single-source questions → `yoyo agent`. Multi-part or multi-source →
`yoyo plan`, now better on both correctness and latency. `PLANNER_INSTRUCTION` was
rewritten: its "roughly three times slower" warning was measured on `agent` and is false on
`coder`, so the split criterion is now source difference, with "when unsure, SPLIT".

**Open.** One trial per config across all four rounds. The graph has still never been tested
in its intended case — mail **and** vault **and** corpus — which needs OAuth first.

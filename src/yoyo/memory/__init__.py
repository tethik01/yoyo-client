"""Yoyo's memory — the second-brain layer.

Three layers, following the wiki pattern the owner chose (Karpathy's gist):

    raw sources (immutable)  ->  the wiki (LLM-written)  ->  the schema (governance)

- `sources` — Phase 1. Conversations and notes as verbatim, retrievable raw sources.
  Never calls a model.
- `extract` — the ONLY place memory calls a model. Proposes claims; decides nothing.
- `wiki`   — verification and rendering. Enforces the rule the whole design rests on:
  **a claim traces to a raw source, never to another wiki page.** Without it, automatic
  writing poisons its own well; with it, a fabrication cannot compound.
- `build`  — extraction + verification + identity + time. Contradictions supersede rather
  than overwrite; genuine ambiguity is asked, not guessed.

The owner's notes are raw sources. Yoyo never edits them — except inside a marked block in a
note about an entity the owner already keeps, which was their explicit choice.
"""

from . import build, extract, schema, sources, wiki

__all__ = ["build", "extract", "schema", "sources", "wiki"]

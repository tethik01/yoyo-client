"""Yoyo's memory — the second-brain layer.

Three layers, following the wiki pattern the owner chose (Karpathy's gist):

    raw sources (immutable)  ->  the wiki (LLM-written)  ->  the schema (governance)

- `sources` — Phase 1. Conversations and notes as verbatim, retrievable raw sources.
  Nothing here interprets anything.
- the wiki — Phase 2+. Entity and concept pages written automatically, where **every claim
  must trace to a raw source and never to another wiki page.** That single rule is what
  makes automatic writing defensible: a fabrication cannot compound, because nothing may
  cite a page that a model wrote.

The owner's notes are raw sources. Yoyo never edits them.
"""

from . import sources

__all__ = ["sources"]

"""The governance layer — the third of the pattern's three.

Karpathy's gist keeps conventions in a schema document beside the wiki. Yoyo writes the same
thing into the vault so that the rules governing Yoyo's memory are readable by the owner, by
a future maintainer, and by any other tool pointed at the folder — rather than living only in
Python docstrings nobody opens.

Regenerated on every build. It is documentation OF the code, not configuration FOR it: the
gates it describes are enforced in `wiki.verify`, and editing this file changes nothing.
That is stated in the file itself, because a governance doc that looks editable but is not
is worse than none.
"""

from __future__ import annotations

from pathlib import Path

from . import wiki

SCHEMA_FILE = "SCHEMA.md"

TEXT = """---
generated_by: yoyo
---

# How Yoyo's memory works

**This file is generated. Editing it changes nothing** — the rules below are enforced in
code (`yoyo/memory/wiki.py`). It exists so the rules are readable where the memory lives.

## Three layers

1. **Raw sources — immutable.** Your conversations with Yoyo, and the notes you write.
   Yoyo never edits your notes. `conversation://14`, `vault://Notes/Trip.md`.
2. **The wiki — written by Yoyo.** Everything in `yoyo-memory/`. Entity and concept pages.
3. **This schema — governance.** What you are reading.

## The rule everything rests on

> **A claim traces to a raw source. Never to another wiki page.**

Yoyo writes memory automatically, with no approval step. That is only safe because of this
rule. Without it a fact extracted wrongly on Monday becomes a source on Tuesday, is cited on
Wednesday, and by Friday is indistinguishable from something you said — the provenance would
be real, because Yoyo *did* read it in its own notes. With it, a mistake stays a single
mistake and always points back at something a human actually wrote or said.

Two gates enforce it, neither involving judgement:

- **Verbatim quote.** Every claim carries a quote that must appear in the raw source it
  names. No quote, or a quote the source does not contain, and the claim is discarded
  before it reaches a page.
- **Source kind.** A claim citing anything under `yoyo-memory/` is rejected outright.

## What Yoyo will and will not do

| | |
|---|---|
| Write pages under `yoyo-memory/` | ✅ automatically |
| Add a marked block to a note you already wrote | ✅ only between the markers |
| Edit your prose | ❌ never |
| Resolve a contradiction | ❌ never — both claims are kept and the tension is shown |
| Guess whether \u201cMom\u201d and a name are the same person | ❌ never — it asks |
| Delete a memory | only when you ask; the log records that it happened, not what it was |

## Files

- `index.md` — the catalogue, regenerated each build
- `log.md` — append-only. Never rewritten, so the record of what memory did cannot be
  edited by the thing that did it.
- `<kind>s/<Name>.md` — one page per entity

## Sources currently feeding memory

Conversations with Yoyo, and the notes you write. **Mail and calendar are deliberately
excluded** — that was a decision, not an oversight, and widening it should be one too.
"""


def write(root: Path) -> Path:
    path = root / wiki.WIKI_DIR / SCHEMA_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEXT, encoding="utf-8")
    return path

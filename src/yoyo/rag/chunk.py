"""Chunking. Boundary-aware, deterministic, no model calls.

Character-based on purpose: the tokenizer lives on the server, and a chunker that
needs a network round-trip is a chunker that fails when the tailnet is down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Preference order for where to cut: paragraph, then sentence, then line, then space.
_BOUNDARIES = [re.compile(p) for p in (r"\n\s*\n", r"(?<=[.!?])\s+", r"\n", r"\s+")]


@dataclass(slots=True)
class Chunk:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    token_estimate: int

    def as_row(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "token_estimate": self.token_estimate,
        }


def estimate_tokens(text: str) -> int:
    """~4 chars per token. Good enough for budgeting; never used for correctness."""
    return max(1, len(text) // 4)


def _cut_point(text: str, start: int, hard_end: int) -> int:
    """Find the latest boundary before hard_end, searching the last third of the window."""
    window_start = start + (hard_end - start) * 2 // 3
    segment = text[window_start:hard_end]
    for pattern in _BOUNDARIES:
        matches = list(pattern.finditer(segment))
        if matches:
            return window_start + matches[-1].end()
    return hard_end


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[Chunk]:
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be >= 0 and < size")

    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    chunks: list[Chunk] = []
    pos = 0
    ordinal = 0
    n = len(text)

    while pos < n:
        hard_end = min(pos + size, n)
        end = hard_end if hard_end >= n else _cut_point(text, pos, hard_end)
        if end <= pos:  # pathological input with no boundaries
            end = hard_end

        body = text[pos:end].strip()
        if body:
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    text=body,
                    char_start=pos,
                    char_end=end,
                    token_estimate=estimate_tokens(body),
                )
            )
            ordinal += 1

        if end >= n:
            break
        next_pos = end - overlap
        pos = next_pos if next_pos > pos else end

    return chunks

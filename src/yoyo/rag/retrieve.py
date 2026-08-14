"""Hybrid retrieval: dense (Qdrant) + sparse (SQLite FTS5), fused with RRF.

RRF rather than score-weighting because cosine similarity and BM25 are not on a
comparable scale and any weighting constant would be a number nobody can defend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import embeddings
from ..config import get_models
from ..storage import db, vectors

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Passage:
    chunk_id: int
    text: str
    title: str | None
    source_path: str
    ordinal: int
    score: float

    def cite(self) -> str:
        return f"[{self.title or self.source_path}#{self.ordinal}]"


def reciprocal_rank_fusion(
    rankings: list[list[tuple[int, float]]], k: int = 60
) -> list[tuple[int, float]]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, (chunk_id, _score) in enumerate(ranking, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)


def retrieve(query: str, top_k: int | None = None) -> list[Passage]:
    cfg = get_models()
    r = cfg.retrieval
    final_k = top_k or r.final_top_k

    dense: list[tuple[int, float]] = []
    try:
        vector = embeddings.embed([query])[0]
        dense = vectors.search(vector, limit=r.dense_top_k)
    except Exception as exc:  # noqa: BLE001 - degrade to sparse rather than fail the turn
        log.warning("dense retrieval unavailable, falling back to sparse only: %s", exc)

    with db.connection() as conn:
        sparse = db.fts_search(conn, query, limit=r.sparse_top_k)
        if not dense and not sparse:
            return []

        fused = reciprocal_rank_fusion([x for x in (dense, sparse) if x], k=r.rrf_k)
        candidate_ids = [cid for cid, _ in fused[: max(final_k * 3, final_k)]]
        rows = db.get_chunks(conn, candidate_ids)

    scores = dict(fused)
    passages = [
        Passage(
            chunk_id=int(row["id"]),
            text=row["text"],
            title=row["title"],
            source_path=row["source_path"],
            ordinal=int(row["ordinal"]),
            score=scores.get(int(row["id"]), 0.0),
        )
        for row in rows
    ]

    if cfg.reranking.enabled:
        passages = _rerank(query, passages)

    return passages[:final_k]


def _rerank(query: str, passages: list[Passage]) -> list[Passage]:
    """Cross-encoder rerank via the server. Falls back to fusion order on any failure."""
    from ..config import get_settings

    cfg = get_models().reranking
    s = get_settings()
    try:
        import httpx

        resp = httpx.post(
            f"{s.llm_base_url.rstrip('/')}/rerank",
            headers={"Authorization": f"Bearer {s.llm_api_key}"},
            json={
                "model": cfg.endpoint,
                "query": query,
                "documents": [p.text for p in passages],
            },
            timeout=60,
        )
        resp.raise_for_status()
        results = resp.json()["results"]
    except Exception as exc:  # noqa: BLE001
        log.warning("rerank failed, keeping fusion order: %s", exc)
        return passages

    ordered: list[Passage] = []
    for item in sorted(results, key=lambda r: r["relevance_score"], reverse=True):
        p = passages[item["index"]]
        p.score = float(item["relevance_score"])
        ordered.append(p)
    return ordered


def build_context(passages: list[Passage], max_chars: int | None = None) -> str:
    """Render passages for the prompt. Each block is citable so answers can be audited."""
    if max_chars is None:
        max_chars = get_models().retrieval.max_context_chars
    blocks: list[str] = []
    used = 0
    for p in passages:
        block = f"<source id=\"{p.chunk_id}\" title=\"{p.title or p.source_path}\">\n{p.text}\n</source>"
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)

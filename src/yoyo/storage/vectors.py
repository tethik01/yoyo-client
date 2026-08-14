"""Qdrant. Holds vectors and the chunk id needed to join back to SQLite — nothing else.

Deliberately no document text in the payload: one system of record, and re-embedding
should never risk stale copies of content diverging from SQLite.
"""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from ..config import get_settings

log = logging.getLogger(__name__)


def client() -> QdrantClient:
    s = get_settings()
    return QdrantClient(url=s.qdrant_url, timeout=30)


def ensure_collection(dimensions: int, recreate: bool = False) -> None:
    s = get_settings()
    c = client()
    exists = c.collection_exists(s.qdrant_collection)

    if exists and recreate:
        c.delete_collection(s.qdrant_collection)
        exists = False

    if exists:
        info = c.get_collection(s.qdrant_collection)
        current = info.config.params.vectors.size  # type: ignore[union-attr]
        if current != dimensions:
            raise RuntimeError(
                f"Collection {s.qdrant_collection!r} has dimension {current}, but the "
                f"embed model produces {dimensions}. Changing the embed model requires a "
                f"full re-embed: `yoyo reindex --recreate`."
            )
        return

    c.create_collection(
        collection_name=s.qdrant_collection,
        vectors_config=qm.VectorParams(size=dimensions, distance=qm.Distance.COSINE),
    )
    c.create_payload_index(
        collection_name=s.qdrant_collection,
        field_name="document_id",
        field_schema=qm.PayloadSchemaType.INTEGER,
    )
    log.info("created Qdrant collection %s (dim=%d)", s.qdrant_collection, dimensions)


def upsert(chunk_ids: list[int], vectors: list[list[float]], document_ids: list[int]) -> None:
    if not chunk_ids:
        return
    if not (len(chunk_ids) == len(vectors) == len(document_ids)):
        raise ValueError("chunk_ids, vectors and document_ids must be the same length")

    s = get_settings()
    points = [
        qm.PointStruct(id=cid, vector=vec, payload={"chunk_id": cid, "document_id": did})
        for cid, vec, did in zip(chunk_ids, vectors, document_ids, strict=True)
    ]
    client().upsert(collection_name=s.qdrant_collection, points=points, wait=True)


def search(vector: list[float], limit: int = 20) -> list[tuple[int, float]]:
    s = get_settings()
    hits = client().query_points(
        collection_name=s.qdrant_collection,
        query=vector,
        limit=limit,
        with_payload=True,
    ).points
    return [(int(h.payload["chunk_id"]), float(h.score)) for h in hits]


def delete_document(document_id: int) -> None:
    s = get_settings()
    client().delete(
        collection_name=s.qdrant_collection,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=document_id))]
            )
        ),
        wait=True,
    )


def info() -> dict[str, Any]:
    s = get_settings()
    c = client()
    if not c.collection_exists(s.qdrant_collection):
        return {"collection": s.qdrant_collection, "exists": False}
    i = c.get_collection(s.qdrant_collection)
    return {
        "collection": s.qdrant_collection,
        "exists": True,
        "points": i.points_count,
        "dimensions": i.config.params.vectors.size,  # type: ignore[union-attr]
        "status": str(i.status),
    }

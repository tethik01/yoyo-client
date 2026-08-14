"""MCP server exposing Yoyo's ingested corpus to other clients.

    python -m yoyo.mcp.corpus_server        # or: yoyo mcp serve-corpus

Read-only by design: retrieval and inspection, no mutation. Mount it in Claude Desktop to
query the corpus from there, or from a future phone client.
"""

from __future__ import annotations

import logging
import sys

from ._compat import make_server

log = logging.getLogger(__name__)

server = make_server("yoyo-corpus")


@server.tool(
    description=(
        "Hybrid search (dense + BM25, RRF-fused) over the user's ingested document corpus. "
        "Returns passages with chunk ids suitable for citation."
    )
)
def search_corpus(query: str, top_k: int = 6) -> dict:
    from ..rag import retrieve as rag

    passages = rag.retrieve(query, top_k=top_k)
    return {
        "count": len(passages),
        "passages": [
            {
                "chunk_id": p.chunk_id,
                "title": p.title,
                "source": p.source_path,
                "score": round(p.score, 5),
                "text": p.text,
            }
            for p in passages
        ],
    }


@server.tool(description="Read the full text of one corpus chunk by id.")
def read_chunk(chunk_id: int) -> dict:
    from ..storage import db

    with db.connection() as conn:
        rows = db.get_chunks(conn, [chunk_id])
    if not rows:
        raise ValueError(f"no chunk with id {chunk_id}")
    r = rows[0]
    return {
        "chunk_id": r["id"],
        "title": r["title"],
        "source": r["source_path"],
        "ordinal": r["ordinal"],
        "text": r["text"],
    }


@server.tool(description="Exact document, chunk and embedding counts for the corpus.")
def corpus_stats() -> dict:
    from ..storage import db

    with db.connection() as conn:
        return db.stats(conn)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    log.info("yoyo-corpus serving over stdio")
    server.run("stdio")


if __name__ == "__main__":
    main()

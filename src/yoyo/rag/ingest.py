"""Ingest: file -> text -> chunks -> SQLite -> embeddings -> Qdrant.

Content-hashed, so re-running over an unchanged folder is close to free.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .. import embeddings
from ..config import get_models
from ..storage import db, vectors
from .chunk import chunk_text

log = logging.getLogger(__name__)

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".org", ".csv", ".json", ".yaml", ".yml"}
DOC_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm"}
EMBED_BATCH = 64


@dataclass
class IngestReport:
    scanned: int = 0
    ingested: int = 0
    skipped_unchanged: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    chunks_written: int = 0
    chunks_embedded: int = 0

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} ingested={self.ingested} "
            f"unchanged={self.skipped_unchanged} failed={len(self.failed)} "
            f"chunks={self.chunks_written} embedded={self.chunks_embedded}"
        )


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in DOC_SUFFIXES:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise RuntimeError(
                f"{path.name} needs docling. Install it with: uv pip install -e .[ingest]"
            ) from exc
        result = DocumentConverter().convert(str(path))
        return result.document.export_to_markdown()
    raise RuntimeError(f"unsupported file type: {suffix}")


def _hash(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def ingest_path(root: Path, recursive: bool = True) -> IngestReport:
    cfg = get_models()
    vectors.ensure_collection(embeddings.dimensions())

    files = _collect(root, recursive)
    report = IngestReport(scanned=len(files))

    with db.connection() as conn:
        for path in files:
            try:
                text = extract_text(path)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
                report.failed.append((str(path), str(exc)))
                log.warning("extract failed for %s: %s", path, exc)
                continue

            digest = _hash(text)
            with db.transaction(conn):
                doc_id, changed = db.upsert_document(
                    conn,
                    source_path=str(path),
                    title=path.stem,
                    content_hash=digest,
                    mime_type=path.suffix.lower().lstrip("."),
                    byte_size=path.stat().st_size,
                )
                if not changed:
                    report.skipped_unchanged += 1
                    continue

                pieces = chunk_text(text, cfg.retrieval.chunk_size, cfg.retrieval.chunk_overlap)
                db.insert_chunks(conn, doc_id, [p.as_row() for p in pieces])
                report.chunks_written += len(pieces)
                report.ingested += 1

            if changed:
                # Vectors for the old revision are stale the moment chunks are rebuilt.
                vectors.delete_document(doc_id)

        report.chunks_embedded = embed_pending(conn)

    return report


def ingest_text(
    source_path: str,
    title: str,
    text: str,
    mime_type: str = "text",
) -> bool:
    """Ingest text that is not a file on disk. Returns True if it changed.

    Added for conversation transcripts, which have no path to walk but are otherwise ordinary
    documents — same chunking, same embeddings, same citation ids. A separate retrieval path
    for "memory" would be a second thing to get subtly wrong, and would break the property
    that `[12]` resolves the same way whatever produced it.
    """
    cfg = get_models()
    vectors.ensure_collection(embeddings.dimensions())

    digest = _hash(text)
    with db.connection() as conn:
        with db.transaction(conn):
            doc_id, changed = db.upsert_document(
                conn,
                source_path=source_path,
                title=title,
                content_hash=digest,
                mime_type=mime_type,
                byte_size=len(text.encode("utf-8")),
            )
            if not changed:
                return False
            pieces = chunk_text(text, cfg.retrieval.chunk_size, cfg.retrieval.chunk_overlap)
            db.insert_chunks(conn, doc_id, [p.as_row() for p in pieces])

        # Outside the transaction: vectors for the old revision are stale the moment the
        # chunks are rebuilt, and deleting them inside would hold the write lock over a
        # network call to Qdrant.
        vectors.delete_document(doc_id)
        embed_pending(conn)
    return True


def embed_pending(conn) -> int:  # noqa: ANN001 - sqlite3.Connection
    """Embed every chunk that has no vector yet. Safe to re-run; resumes where it stopped."""
    cfg = get_models()
    label = f"{cfg.embeddings.provider}:{cfg.embeddings.local_model or cfg.embeddings.endpoint}"
    total = 0

    while True:
        rows = db.pending_embeddings(conn, EMBED_BATCH)
        if not rows:
            break
        ids = [int(r["id"]) for r in rows]
        texts = [r["text"] for r in rows]

        vecs = embeddings.embed(texts)
        doc_ids = [
            int(r["document_id"])
            for r in db.get_chunk_documents(conn, ids)
        ]
        vectors.upsert(ids, vecs, doc_ids)

        with db.transaction(conn):
            db.mark_embedded(conn, ids, label)
        total += len(ids)
        log.info("embedded %d chunks (%d total)", len(ids), total)

    return total


def _collect(root: Path, recursive: bool) -> list[Path]:
    if root.is_file():
        return [root]
    pattern = "**/*" if recursive else "*"
    allowed = TEXT_SUFFIXES | DOC_SUFFIXES
    return sorted(
        p for p in root.glob(pattern) if p.is_file() and p.suffix.lower() in allowed
    )

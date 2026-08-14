"""SQLite access. System of record for documents, chunks, conversations, entities."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, get_settings

MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def connection(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path or get_settings().sqlite_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    v = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    return int(v or 0)


def migrate(path: Path | None = None) -> list[str]:
    """Apply every migration file whose number exceeds the recorded version."""
    applied: list[str] = []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"No migrations found in {MIGRATIONS_DIR}")

    with connection(path) as conn:
        have = current_version(conn)
        for f in files:
            try:
                number = int(f.name.split("_", 1)[0])
            except ValueError as exc:
                raise ValueError(f"Migration filename must start with a number: {f.name}") from exc
            if number <= have:
                continue
            conn.executescript(f.read_text(encoding="utf-8"))
            conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (number,))
            applied.append(f.name)
    return applied


# ------------------------------------------------------------------ writes ---


def upsert_document(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    title: str | None,
    content_hash: str,
    mime_type: str | None = None,
    byte_size: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """Returns (document_id, changed). changed=False means the hash matched — skip re-ingest."""
    existing = conn.execute(
        "SELECT id, content_hash FROM documents WHERE source_path = ?", (source_path,)
    ).fetchone()

    if existing and existing["content_hash"] == content_hash:
        return int(existing["id"]), False

    payload = json.dumps(metadata or {})
    if existing:
        conn.execute(
            """UPDATE documents
                  SET title=?, content_hash=?, mime_type=?, byte_size=?,
                      updated_at=datetime('now'), metadata=?
                WHERE id=?""",
            (title, content_hash, mime_type, byte_size, payload, existing["id"]),
        )
        # Chunks are derived data — rebuild them rather than trying to diff.
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (existing["id"],))
        return int(existing["id"]), True

    cur = conn.execute(
        """INSERT INTO documents (source_path, title, content_hash, mime_type, byte_size, metadata)
           VALUES (?,?,?,?,?,?)""",
        (source_path, title, content_hash, mime_type, byte_size, payload),
    )
    return int(cur.lastrowid), True


def insert_chunks(
    conn: sqlite3.Connection, document_id: int, chunks: list[dict[str, Any]]
) -> list[int]:
    ids: list[int] = []
    for c in chunks:
        cur = conn.execute(
            """INSERT INTO chunks (document_id, ordinal, text, char_start, char_end, token_estimate)
               VALUES (?,?,?,?,?,?)""",
            (
                document_id,
                c["ordinal"],
                c["text"],
                c.get("char_start"),
                c.get("char_end"),
                c.get("token_estimate"),
            ),
        )
        ids.append(int(cur.lastrowid))
    return ids


def mark_embedded(conn: sqlite3.Connection, chunk_ids: list[int], model: str) -> None:
    conn.executemany(
        "UPDATE chunks SET embedded_at = datetime('now'), embed_model = ? WHERE id = ?",
        [(model, cid) for cid in chunk_ids],
    )


def pending_embeddings(conn: sqlite3.Connection, limit: int = 256) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, text FROM chunks WHERE embedded_at IS NULL ORDER BY id LIMIT ?", (limit,)
    ).fetchall()


# ------------------------------------------------------------------- reads ---


def get_chunks(conn: sqlite3.Connection, chunk_ids: list[int]) -> list[sqlite3.Row]:
    if not chunk_ids:
        return []
    marks = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"""SELECT c.id, c.text, c.ordinal, d.title, d.source_path
              FROM chunks c JOIN documents d ON d.id = c.document_id
             WHERE c.id IN ({marks})""",
        chunk_ids,
    ).fetchall()
    order = {cid: i for i, cid in enumerate(chunk_ids)}
    return sorted(rows, key=lambda r: order[r["id"]])


def get_chunk_documents(conn: sqlite3.Connection, chunk_ids: list[int]) -> list[sqlite3.Row]:
    """document_id for each chunk id, in the order given."""
    if not chunk_ids:
        return []
    marks = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"SELECT id, document_id FROM chunks WHERE id IN ({marks})", chunk_ids
    ).fetchall()
    order = {cid: i for i, cid in enumerate(chunk_ids)}
    return sorted(rows, key=lambda r: order[r["id"]])


def fts_search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[tuple[int, float]]:
    """BM25 over chunk text. Lower bm25() is better, so negate for a descending score."""
    cleaned = _fts_escape(query)
    if not cleaned:
        return []
    rows = conn.execute(
        """SELECT rowid AS id, bm25(chunks_fts) AS score
             FROM chunks_fts WHERE chunks_fts MATCH ?
             ORDER BY score LIMIT ?""",
        (cleaned, limit),
    ).fetchall()
    return [(int(r["id"]), -float(r["score"])) for r in rows]


def _fts_escape(query: str) -> str:
    """FTS5 chokes on bare punctuation. Quote each token; drop empties."""
    tokens = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split() if t]
    return " OR ".join(f'"{t}"' for t in tokens)


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "documents": one("SELECT COUNT(*) FROM documents"),
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "chunks_embedded": one("SELECT COUNT(*) FROM chunks WHERE embedded_at IS NOT NULL"),
        "conversations": one("SELECT COUNT(*) FROM conversations"),
        "messages": one("SELECT COUNT(*) FROM messages"),
        "schema_version": current_version(conn),
    }

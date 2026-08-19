import pytest

from yoyo.storage import db


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "test.db"
    db.migrate(path)
    with db.connection(path) as c:
        yield c


def test_migration_sets_version(conn):
    """Derived from the files on disk, not hardcoded. A literal here means every new
    migration breaks a test that has nothing to do with the change."""
    highest = max(int(f.name.split("_", 1)[0]) for f in db.MIGRATIONS_DIR.glob("*.sql"))
    assert db.current_version(conn) == highest


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    first = db.migrate(path)
    second = db.migrate(path)
    assert first
    assert second == []


def test_upsert_document_detects_unchanged(conn):
    with db.transaction(conn):
        doc_id, changed = db.upsert_document(
            conn, source_path="a.md", title="a", content_hash="h1"
        )
    assert changed is True

    with db.transaction(conn):
        same_id, changed = db.upsert_document(
            conn, source_path="a.md", title="a", content_hash="h1"
        )
    assert same_id == doc_id
    assert changed is False


def test_changed_hash_clears_old_chunks(conn):
    with db.transaction(conn):
        doc_id, _ = db.upsert_document(conn, source_path="b.md", title="b", content_hash="h1")
        db.insert_chunks(conn, doc_id, [{"ordinal": 0, "text": "old content"}])
    assert db.stats(conn)["chunks"] == 1

    with db.transaction(conn):
        db.upsert_document(conn, source_path="b.md", title="b", content_hash="h2")
    assert db.stats(conn)["chunks"] == 0


def test_fts_search_finds_chunk(conn):
    with db.transaction(conn):
        doc_id, _ = db.upsert_document(conn, source_path="c.md", title="c", content_hash="h")
        db.insert_chunks(
            conn,
            doc_id,
            [
                {"ordinal": 0, "text": "the quick brown fox jumps"},
                {"ordinal": 1, "text": "completely unrelated material"},
            ],
        )
    hits = db.fts_search(conn, "brown fox")
    assert hits
    assert hits[0][0] == 1


def test_fts_escape_handles_punctuation_only(conn):
    assert db.fts_search(conn, "??? ---") == []


def test_get_chunks_preserves_requested_order(conn):
    with db.transaction(conn):
        doc_id, _ = db.upsert_document(conn, source_path="d.md", title="d", content_hash="h")
        ids = db.insert_chunks(
            conn, doc_id, [{"ordinal": i, "text": f"chunk {i}"} for i in range(3)]
        )
    reversed_ids = list(reversed(ids))
    rows = db.get_chunks(conn, reversed_ids)
    assert [r["id"] for r in rows] == reversed_ids


def test_mark_embedded_and_pending(conn):
    with db.transaction(conn):
        doc_id, _ = db.upsert_document(conn, source_path="e.md", title="e", content_hash="h")
        ids = db.insert_chunks(conn, doc_id, [{"ordinal": 0, "text": "x"}])
    assert len(db.pending_embeddings(conn)) == 1
    with db.transaction(conn):
        db.mark_embedded(conn, ids, "yoyo-embed")
    assert db.pending_embeddings(conn) == []
    assert db.stats(conn)["chunks_embedded"] == 1

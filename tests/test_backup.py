"""Backup and restore-drill tests.

The drill is the point: canon insists a restore is *proven* before the first real ingest,
because an unverified backup is a guess. These tests prove the drill itself detects the
failures it claims to detect.
"""

import json
import sqlite3
import zipfile

import pytest

from yoyo import backup
from yoyo.storage import db


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A populated database wired up as the live one."""
    data = tmp_path / "data"
    data.mkdir()
    dbpath = data / "yoyo.db"
    db.migrate(dbpath)

    with db.connection(dbpath) as conn, db.transaction(conn):
        doc_id, _ = db.upsert_document(
            conn, source_path="a.md", title="a", content_hash="h1"
        )
        ids = db.insert_chunks(
            conn, doc_id, [{"ordinal": i, "text": f"chunk {i}"} for i in range(3)]
        )
        db.mark_embedded(conn, ids, "local:test")
        conn.execute("INSERT INTO conversations (title) VALUES ('t')")
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (1,'user','q')"
        )
        conn.execute(
            "INSERT INTO message_citations (message_id, chunk_id, rank) VALUES (1,?,0)",
            (ids[0],),
        )

    settings = backup.get_settings()
    monkeypatch.setattr(settings, "sqlite_path", dbpath, raising=False)
    monkeypatch.setattr(settings, "data_dir", data, raising=False)
    return dbpath


def test_create_writes_archive(live, tmp_path):
    dest = tmp_path / "drive"
    dest.mkdir()
    archive = backup.create(dest)
    assert archive.exists()
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
    assert {"yoyo.db", "manifest.json"} <= names


def test_env_is_never_in_the_archive(live, tmp_path):
    """The key must not land on an unencrypted external drive."""
    dest = tmp_path / "drive"
    dest.mkdir()
    with zipfile.ZipFile(backup.create(dest)) as z:
        names = z.namelist()
    assert not any(".env" in n for n in names)


def test_manifest_records_counts_and_embedding_model(live, tmp_path):
    dest = tmp_path / "drive"
    dest.mkdir()
    with zipfile.ZipFile(backup.create(dest)) as z:
        m = json.loads(z.read("manifest.json"))
    assert m["stats"]["documents"] == 1
    assert m["stats"]["chunks"] == 3
    assert m["stats"]["chunks_embedded"] == 3
    assert m["embeddings"]["dimensions"] > 0
    assert m["env_required"]


def test_missing_folder_on_a_present_drive_is_created(live, tmp_path):
    """A folder that doesn't exist yet is not an error — just make it."""
    dest = tmp_path / "drive" / "yoyo-backups"
    archive = backup.create(dest)
    assert dest.is_dir()
    assert archive.exists()


def test_missing_drive_is_a_distinct_error(live, tmp_path, monkeypatch):
    """A missing drive root is unfixable from here and must say so."""
    import yoyo.backup as b

    real_exists = b.Path.exists

    def fake_exists(self):
        if str(self) in ("Z:\\", "/nonexistent-root"):
            return False
        return real_exists(self)

    monkeypatch.setattr(b.Path, "exists", fake_exists)
    target = tmp_path / "sub"
    monkeypatch.setattr(
        b.Path, "anchor", property(lambda self: "/nonexistent-root"), raising=False
    )
    with pytest.raises(FileNotFoundError, match="plugged in"):
        backup.create(target)


def test_target_that_is_a_file_is_rejected(live, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(NotADirectoryError):
        backup.create(f)


def test_drill_passes_on_a_good_archive(live, tmp_path):
    dest = tmp_path / "drive"
    dest.mkdir()
    result = backup.restore_drill(backup.create(dest))
    assert result.ok, [c for c in result.checks if not c[1]]
    names = [c[0] for c in result.checks]
    assert "integrity_check" in names
    assert "citations resolve" in names


def test_drill_fails_on_a_corrupt_archive(live, tmp_path):
    dest = tmp_path / "drive"
    dest.mkdir()
    archive = backup.create(dest)
    archive.write_bytes(b"not a zip file at all")
    result = backup.restore_drill(archive)
    assert not result.ok


def test_drill_fails_when_counts_disagree_with_manifest(live, tmp_path):
    """A silently truncated database must not pass as restorable."""
    dest = tmp_path / "drive"
    dest.mkdir()
    archive = backup.create(dest)

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive) as src:
        manifest = src.read("manifest.json")
        dbbytes = src.read("yoyo.db")

    scratch = tmp_path / "s.db"
    scratch.write_bytes(dbbytes)
    conn = sqlite3.connect(scratch)
    conn.execute("DELETE FROM chunks")
    conn.commit()
    conn.close()

    with zipfile.ZipFile(tampered, "w") as z:
        z.writestr("manifest.json", manifest)
        z.write(scratch, "yoyo.db")

    result = backup.restore_drill(tampered)
    assert not result.ok
    failed = [n for n, ok, _ in result.checks if not ok]
    assert any("chunks" in n for n in failed)


def test_drill_fails_on_missing_archive(tmp_path):
    result = backup.restore_drill(tmp_path / "nope.zip")
    assert not result.ok


def test_latest_picks_the_newest(live, tmp_path):
    dest = tmp_path / "drive"
    dest.mkdir()
    assert backup.latest(dest) is None
    a = backup.create(dest)
    assert backup.latest(dest) == a


def test_restore_refuses_to_clobber_without_force(live, tmp_path):
    dest = tmp_path / "drive"
    dest.mkdir()
    archive = backup.create(dest)
    with pytest.raises(FileExistsError, match="--force"):
        backup.restore(archive)


def test_restore_with_force_replaces_the_database(live, tmp_path):
    dest = tmp_path / "drive"
    dest.mkdir()
    archive = backup.create(dest)

    with db.connection(live) as conn, db.transaction(conn):
        conn.execute("DELETE FROM chunks")
    with db.connection(live) as conn:
        assert db.stats(conn)["chunks"] == 0

    backup.restore(archive, force=True)
    with db.connection(live) as conn:
        assert db.stats(conn)["chunks"] == 3

"""Backup and restore drill.

Design follows from two facts already established:

- **Qdrant is ephemeral and rebuildable.** Vectors are derived data. Backing them up would
  triple the archive size to protect something `yoyo reindex --recreate` regenerates.
- **SQLite is the system of record.** Documents, chunks, conversations and citations are the
  only irreplaceable state Yoyo owns.

So a backup is: a consistent SQLite snapshot + the config needed to interpret it + a
manifest to verify it against. Restore is: put the database back, re-embed.

`.env` is deliberately **excluded**. It holds the LiteLLM key, and the chosen backup target
is an unencrypted external drive on a machine with no disk encryption. A manifest records
which variables must be repopulated; the key itself gets re-issued, not restored.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import MODELS_FILE, get_models, get_settings
from .storage import db

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
DB_NAME = "yoyo.db"
MODELS_NAME = "yoyo-models.yaml"
REQUIRED_ENV = ["YOYO_LLM_BASE_URL", "YOYO_LLM_API_KEY"]


@dataclass
class DrillResult:
    ok: bool = True
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        if not ok:
            self.ok = False


def _snapshot(src: Path, dest: Path) -> None:
    """Consistent copy of a live WAL database. A plain file copy can tear."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _manifest() -> dict:
    s = get_settings()
    cfg = get_models()
    with db.connection() as conn:
        stats = db.stats(conn)
        docs = [
            {"path": r["source_path"], "hash": r["content_hash"], "chunks": r["n"]}
            for r in conn.execute(
                """SELECT d.source_path, d.content_hash,
                          (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) n
                     FROM documents d ORDER BY d.id"""
            )
        ]
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "embeddings": {
            "provider": cfg.embeddings.provider,
            "model": cfg.embeddings.local_model or cfg.embeddings.endpoint,
            "dimensions": cfg.embeddings.dimensions,
        },
        "qdrant_collection": s.qdrant_collection,
        "documents": docs,
        "env_required": REQUIRED_ENV,
        "notes": [
            "Qdrant vectors are NOT in this archive — they are derived data.",
            "After restoring the database, run: yoyo reindex --recreate",
            ".env is excluded on purpose. Re-issue the LiteLLM key rather than restoring it.",
        ],
    }


def ensure_target(dest_dir: Path) -> Path:
    """Resolve the backup folder, distinguishing a missing drive from a missing folder.

    "F:\\ is not plugged in" and "F:\\yoyo-backups hasn't been created yet" are different
    problems with different fixes, and one of them we can just solve.
    """
    dest_dir = Path(dest_dir).expanduser()

    if dest_dir.exists():
        if not dest_dir.is_dir():
            raise NotADirectoryError(f"Backup target {dest_dir} is a file, not a directory")
        return dest_dir

    anchor = Path(dest_dir.anchor) if dest_dir.anchor else None
    if anchor is not None and not anchor.exists():
        raise FileNotFoundError(
            f"Drive {anchor} is not available — is the external drive plugged in?"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    log.info("created backup folder %s", dest_dir)
    return dest_dir


def create(dest_dir: Path) -> Path:
    """Write a timestamped backup archive into dest_dir. Returns the archive path."""
    dest_dir = ensure_target(dest_dir)

    s = get_settings()
    if not s.sqlite_path.exists():
        raise FileNotFoundError(f"No database at {s.sqlite_path} — nothing to back up")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = dest_dir / f"yoyo-backup-{stamp}.zip"
    manifest = _manifest()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / DB_NAME
        _snapshot(s.sqlite_path, tmp_db)

        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp_db, DB_NAME)
            if MODELS_FILE.exists():
                z.write(MODELS_FILE, MODELS_NAME)
            z.writestr(MANIFEST, json.dumps(manifest, indent=2))

    log.info("wrote %s (%.1f KB)", archive, archive.stat().st_size / 1024)
    return archive


def latest(dest_dir: Path) -> Path | None:
    archives = sorted(Path(dest_dir).expanduser().glob("yoyo-backup-*.zip"))
    return archives[-1] if archives else None


def restore_drill(archive: Path) -> DrillResult:
    """Verify a backup can actually be restored. Never touches live data.

    Canon insists the restore drill happens before the first real ingest, because an
    unverified backup is a guess. This restores into a scratch directory and checks that
    the database opens, the schema is present, the counts match the manifest, and the
    citation joins still resolve.
    """
    r = DrillResult()
    archive = Path(archive).expanduser()

    if not archive.exists():
        r.add("archive exists", False, str(archive))
        return r
    r.add("archive exists", True, f"{archive.name} ({archive.stat().st_size / 1024:.1f} KB)")

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        try:
            with zipfile.ZipFile(archive) as z:
                bad = z.testzip()
                if bad:
                    r.add("archive intact", False, f"corrupt member: {bad}")
                    return r
                z.extractall(scratch)
        except zipfile.BadZipFile as exc:
            r.add("archive intact", False, str(exc))
            return r
        r.add("archive intact", True, "zip CRC ok")

        man_path = scratch / MANIFEST
        if not man_path.exists():
            r.add("manifest", False, "missing")
            return r
        manifest = json.loads(man_path.read_text(encoding="utf-8"))
        r.add("manifest", True, f"created {manifest['created_at']}")

        db_path = scratch / DB_NAME
        if not db_path.exists():
            r.add("database present", False, "missing from archive")
            return r
        r.add("database present", True, f"{db_path.stat().st_size / 1024:.1f} KB")

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            r.add("database opens", False, str(exc))
            return r

        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            r.add("integrity_check", integrity == "ok", integrity)

            version = db.current_version(conn)
            r.add("schema version", version > 0, f"v{version}")

            live = db.stats(conn)
            expected = manifest["stats"]
            for key in ("documents", "chunks", "chunks_embedded", "messages"):
                r.add(
                    f"count: {key}",
                    live.get(key) == expected.get(key),
                    f"restored={live.get(key)} manifest={expected.get(key)}",
                )

            # A restored corpus whose citations no longer join is restored in name only.
            orphans = conn.execute(
                """SELECT COUNT(*) FROM message_citations mc
                    LEFT JOIN chunks c ON c.id = mc.chunk_id WHERE c.id IS NULL"""
            ).fetchone()[0]
            r.add("citations resolve", orphans == 0, f"{orphans} orphaned")

            fts = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
            r.add("fts index", fts == live["chunks"], f"fts={fts} chunks={live['chunks']}")
        finally:
            conn.close()

    return r


def restore(archive: Path, force: bool = False) -> Path:
    """Replace the live database with the one in the archive. Destructive.

    Vectors are not restored — run `yoyo reindex --recreate` afterwards.
    """
    s = get_settings()
    archive = Path(archive).expanduser()

    if s.sqlite_path.exists() and not force:
        raise FileExistsError(
            f"{s.sqlite_path} already exists. Re-run with --force to overwrite it, and be "
            f"sure you have a backup of the current state first."
        )

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(archive) as z:
            z.extract(DB_NAME, tmp)
        s.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove WAL siblings; a stale -wal against a swapped database is corruption.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(s.sqlite_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        shutil.copy2(Path(tmp) / DB_NAME, s.sqlite_path)

    log.info("restored %s from %s", s.sqlite_path, archive.name)
    return s.sqlite_path

"""Continuous memory: the sweep that turns conversations into proposals on its own.

The parts were all here — turns are persisted, extraction works, the review queue exists.
What was missing was that every step needed a human to press something, which meant memory
was only ever as current as the last time the owner remembered to run a command. A second
brain you have to remind is not one.

Two mechanisms make an always-on sweep affordable and safe:

**A watermark per conversation.** `messages.id` is monotonic, so "extracted up to N" is the
whole of the state. Each sweep processes only turns newer than that. Without it every run
re-extracts your entire history — the cost grows forever and nearly all of it is redoing
settled work. The watermark advances only *after* claims are queued: a sweep that dies
mid-extraction repeats that slice next time, and duplicate proposals are deduplicated by
fingerprint, whereas a dropped slice is a memory that silently never existed.

**A queue cap.** This is the part that matters more than the plumbing. Compute is not the
constraint here — attention is. If the sweep proposes faster than the owner reviews, the
queue grows without bound, and a queue you cannot clear gets rubber-stamped. A rubber-stamped
review is *worse* than no review, because it launders the same output while looking careful.
So the sweep stops when `pending` reaches the cap and says so. Pausing is honest; an infinite
backlog pretending to be a workflow is not.

What this deliberately does NOT do: write anything to the vault. It proposes. The judgement
about whether a claim is worth keeping stays a human act — that is the entire finding from
the first real run, where six pages of world history passed every mechanical gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import REPO_ROOT
from ..storage import db

log = logging.getLogger(__name__)

CONFIG_FILE = REPO_ROOT / "yoyo-memory.yaml"


@dataclass(slots=True)
class MemoryConfig:
    """How the sweep behaves. Defaults are deliberately conservative."""

    enabled: bool = True
    #: A conversation is "finished enough" once it has been quiet this long. Extracting after
    #: every turn would re-read a half-formed thread repeatedly and propose its own
    #: intermediate states as facts.
    idle_minutes: int = 10
    #: Stop proposing at this many pending. Low on purpose until the approval rate says
    #: extraction is worth trusting.
    queue_cap: int = 25
    #: The nightly catch-up, local time, 24h. Covers whatever the idle trigger missed because
    #: the server was off or MyAIServer was unreachable.
    nightly_hour: int = 3
    #: How often the scheduler looks for idle conversations.
    poll_seconds: int = 300
    #: Write pages as soon as a claim is approved. Approval is the judgement; writing is
    #: bookkeeping, and a second click adds nothing.
    auto_apply: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "idle_minutes": self.idle_minutes,
            "queue_cap": self.queue_cap, "nightly_hour": self.nightly_hour,
            "poll_seconds": self.poll_seconds, "auto_apply": self.auto_apply,
        }


def load_config(path: Path | None = None) -> MemoryConfig:
    path = path or CONFIG_FILE
    if not path.exists():
        return MemoryConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = raw.get("memory") or {}
    cfg = MemoryConfig(
        enabled=bool(section.get("enabled", True)),
        idle_minutes=int(section.get("idle_minutes", 10)),
        queue_cap=int(section.get("queue_cap", 25)),
        nightly_hour=int(section.get("nightly_hour", 3)),
        poll_seconds=int(section.get("poll_seconds", 300)),
        auto_apply=bool(section.get("auto_apply", True)),
    )
    if cfg.queue_cap < 1:
        raise ValueError("memory.queue_cap must be at least 1 — 0 would disable review "
                         "silently rather than visibly; set enabled: false instead")
    if not 0 <= cfg.nightly_hour <= 23:
        raise ValueError(f"memory.nightly_hour must be 0-23, got {cfg.nightly_hour}")
    return cfg


# ------------------------------------------------------------------ watermarks ---


def watermark(source_id: str) -> int:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT last_message_id FROM memory_watermarks WHERE source_id = ?", (source_id,)
        ).fetchone()
    return int(row["last_message_id"]) if row else 0


def set_watermark(source_id: str, message_id: int, claims: int = 0,
                  error: str | None = None) -> None:
    with db.connection() as conn, db.transaction(conn):
        conn.execute(
            """INSERT INTO memory_watermarks
                 (source_id, last_message_id, extracted_at, claims_seen, last_error)
               VALUES (?, ?, datetime('now'), ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                 last_message_id = excluded.last_message_id,
                 extracted_at    = excluded.extracted_at,
                 claims_seen     = memory_watermarks.claims_seen + excluded.claims_seen,
                 last_error      = excluded.last_error""",
            (source_id, int(message_id), int(claims), error),
        )


def reset_watermarks() -> int:
    """Forget what has been extracted, so the next sweep re-reads everything.

    For after an extraction-prompt change: the old prompt's decisions are not evidence about
    the new one. Rejected proposals still stay rejected — that is the review's memory, not
    the sweep's, and re-asking them would be the treadmill.
    """
    with db.connection() as conn, db.transaction(conn):
        return int(conn.execute("DELETE FROM memory_watermarks").rowcount)


# ------------------------------------------------------------------- candidates ---


@dataclass
class Candidate:
    conversation_id: int
    title: str | None
    newest_message_id: int
    from_message_id: int


def candidates(idle_minutes: int = 10, limit: int = 50) -> list[Candidate]:
    """Conversations with owner turns Yoyo has not read yet, and which have gone quiet.

    Three filters, each earning its place:
      * `remember = 1` — the owner can tell any thread to be left alone.
      * quiet for `idle_minutes` — a conversation still in progress is not finished enough to
        summarise, and re-extracting it every few minutes proposes its own drafts as facts.
      * has a USER message past the watermark — nothing to do otherwise. Checking for a user
        message specifically, not any message, because Yoyo's own turns are never evidence.
    """
    sql = """
        SELECT c.id, c.title, MAX(m.id) AS newest,
               COALESCE(w.last_message_id, 0) AS mark
          FROM conversations c
          JOIN messages m ON m.conversation_id = c.id
     LEFT JOIN memory_watermarks w ON w.source_id = 'conversation://' || c.id
         WHERE c.remember = 1
           AND c.updated_at <= datetime('now', ?)
      GROUP BY c.id
        HAVING MAX(CASE WHEN m.role = 'user' AND m.id > COALESCE(w.last_message_id, 0)
                        THEN m.id ELSE 0 END) > 0
      ORDER BY c.updated_at
         LIMIT ?
    """
    with db.connection() as conn:
        rows = conn.execute(sql, (f"-{int(idle_minutes)} minutes", int(limit))).fetchall()
    return [
        Candidate(conversation_id=int(r["id"]), title=r["title"],
                  newest_message_id=int(r["newest"]), from_message_id=int(r["mark"]))
        for r in rows
    ]


def slice_of(conversation_id: int, after_message_id: int) -> tuple[str, int]:
    """The unread part of a conversation, rendered as a transcript. Returns (text, newest id).

    Rendered with the same speaker labels as a full transcript so `evidence_from()` can strip
    Yoyo's half exactly as it does everywhere else. One code path for "who said this", because
    two would eventually disagree and the disagreement would be invisible.
    """
    from . import sources as sources_mod

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, int(after_message_id)),
        ).fetchall()
    if not rows:
        return "", int(after_message_id)

    messages = [
        {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        for r in rows
    ]
    text, _turns, _skipped = sources_mod.render(conversation_id, None, messages)
    return text, int(rows[-1]["id"])


# ------------------------------------------------------------------------ sweep ---


@dataclass
class SweepReport:
    considered: int = 0
    swept: int = 0
    queued: int = 0
    skipped_capped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    capped: bool = False
    pending_before: int = 0
    pending_after: int = 0

    def summary(self) -> str:
        base = (f"considered={self.considered} swept={self.swept} queued={self.queued} "
                f"pending={self.pending_after}")
        if self.capped:
            base += f" CAPPED (skipped {self.skipped_capped})"
        if self.failures:
            base += f" failures={len(self.failures)}"
        return base


def sweep(config: MemoryConfig | None = None, limit: int = 50,
          on_log=None) -> SweepReport:  # noqa: ANN001
    """One pass: every idle conversation with unread owner turns becomes proposals.

    Never writes to the vault, never decides anything. The only side effects are rows in the
    review queue and an advanced watermark.
    """
    from . import build as build_mod
    from . import review

    cfg = config or load_config()
    say = on_log or (lambda line: log.info("%s", line))
    report = SweepReport()
    report.pending_before = review.stats()["pending"]

    if not cfg.enabled:
        say("memory sweeping is disabled in yoyo-memory.yaml")
        report.pending_after = report.pending_before
        return report

    todo = candidates(idle_minutes=cfg.idle_minutes, limit=limit)
    report.considered = len(todo)
    say(f"{len(todo)} conversation(s) with unread turns")

    pending = report.pending_before
    for candidate in todo:
        if pending >= cfg.queue_cap:
            # Stop, loudly. The watermark is NOT advanced, so nothing is lost — this slice is
            # simply read again once the queue has room.
            report.capped = True
            report.skipped_capped += 1
            continue

        source_id = f"conversation://{candidate.conversation_id}"
        text, newest = slice_of(candidate.conversation_id, candidate.from_message_id)
        if not text.strip():
            set_watermark(source_id, newest)
            continue

        try:
            accepted, rejected = build_mod.extract_and_verify({source_id: text})
        except Exception as exc:  # noqa: BLE001 - one bad conversation must not stop a sweep
            log.warning("sweep failed for %s: %s", source_id, exc)
            report.failures.append((source_id, f"{type(exc).__name__}: {exc}"))
            set_watermark(source_id, candidate.from_message_id, error=str(exc))
            continue

        # Say what was thrown away and why. A filter that drops silently looks exactly like
        # an extractor that found nothing, and those two need very different responses.
        for subject, why in rejected[:5]:
            say(f"  dropped {subject}: {why}")
        queued = review.propose(accepted).proposed
        # Advance only after the claims are safely queued.
        set_watermark(source_id, newest, claims=queued)
        report.swept += 1
        report.queued += queued
        pending += queued
        if queued:
            say(f"{source_id}: {queued} new claim(s) to review")

    if report.capped:
        say(f"queue is at the cap ({cfg.queue_cap} pending) — {report.skipped_capped} "
            f"conversation(s) left for later. Clear some of the review queue and the sweep "
            f"picks up exactly where it stopped.")

    report.pending_after = review.stats()["pending"]
    say(report.summary())
    return report


# --------------------------------------------------------------------- opt-out ---


def set_remember(conversation_id: int, remember: bool) -> bool:
    """Tell the sweep to read this conversation, or to leave it alone.

    Turning it off does NOT delete what has already been extracted — that is
    `yoyo memory forget`, which leaves a tombstone. Two different acts: "stop reading" and
    "unsay what you learned", and conflating them would make one of them silent.
    """
    with db.connection() as conn, db.transaction(conn):
        cur = conn.execute(
            "UPDATE conversations SET remember = ? WHERE id = ?",
            (1 if remember else 0, int(conversation_id)),
        )
        return cur.rowcount > 0


def is_remembered(conversation_id: int) -> bool:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT remember FROM conversations WHERE id = ?", (int(conversation_id),)
        ).fetchone()
    return bool(row["remember"]) if row else False


def status() -> dict[str, Any]:
    """What the sweep has done and what it is waiting on — for the UI and `yoyo memory status`."""
    from . import review

    cfg = load_config()
    with db.connection() as conn:
        marks = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(claims_seen), 0) AS claims, "
            "MAX(extracted_at) AS last FROM memory_watermarks"
        ).fetchone()
        ignored = conn.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE remember = 0"
        ).fetchone()
    queue = review.stats()
    return {
        "config": cfg.as_dict(),
        "conversations_swept": int(marks["n"]),
        "claims_ever_queued": int(marks["claims"]),
        "last_sweep": marks["last"],
        "conversations_ignored": int(ignored["n"]),
        "waiting": len(candidates(idle_minutes=cfg.idle_minutes)),
        "queue": queue,
        "capped": queue["pending"] >= cfg.queue_cap,
    }

"""The review queue: proposed memories, and the decision that turns one into a page.

The roadmap originally called for this and I argued it away, on the grounds that two
mechanical gates — verbatim quote, and no claim may cite a wiki page — made automatic
writing defensible. The gates work. They were both green when the first real run produced
six pages about World War I, the Abraham Accords and Gaza.

That is the distinction this module exists for:

    a gate proves a claim is TRACEABLE.
    only you can say whether it is WORTH KEEPING.

No amount of prompt work collapses those two. "Is this fact about my life or about the
world" is a judgement about *your* life, and the model does not have the standing to make
it. So extraction proposes, you dispose, and only approved claims reach a page.

**Rejection is permanent, and that is the design.** A queue that re-proposes what you
already rejected is a treadmill; a treadmill gets rubber-stamped; a rubber-stamped review is
worse than none, because it launders the same output while looking careful. `fingerprint`
gives every claim a stable identity so a re-run knows what it has already asked about.

**The queue is also the metric.** An approval rate near 100% means extraction is proposing
things so obvious they were not worth surfacing. Near 0% means it is generating noise and
you will stop reading. Both are visible in `stats()`, and both are more informative than
any count of pages written.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from ..storage import db
from .wiki import Claim, normalise

log = logging.getLogger(__name__)

STATUSES = ("pending", "approved", "rejected", "written")

#: Joins the parts of a fingerprint. A character that cannot occur inside a subject, claim
#: or quote, so "Priya|sister" and "Priya" + "sister" can never hash alike.
SEPARATOR = "\x1f"


def fingerprint(claim: Claim) -> str:
    """Stable identity for a claim, across runs.

    Normalised, so a model that re-words its own summary of the same quote from the same
    source does not get to ask twice. Deliberately includes the SOURCE: the same fact stated
    in two different conversations is two pieces of evidence, and collapsing them would hide
    that one of them is corroboration.
    """
    parts = [
        normalise(claim.subject), (claim.kind or "").strip().lower(),
        normalise(claim.claim), normalise(claim.quote), (claim.source or "").strip(),
    ]
    return hashlib.sha256(SEPARATOR.join(parts).encode("utf-8")).hexdigest()[:32]


@dataclass
class ProposeReport:
    proposed: int = 0
    already_pending: int = 0
    already_decided: int = 0

    def summary(self) -> str:
        return (
            f"new={self.proposed} already_pending={self.already_pending} "
            f"previously_decided={self.already_decided}"
        )


def propose(claims: list[Claim]) -> ProposeReport:
    """Queue verified claims for review. Idempotent, and it never revives a rejection."""
    report = ProposeReport()
    with db.connection() as conn, db.transaction(conn):
        for claim in claims:
            fp = fingerprint(claim)
            row = conn.execute(
                "SELECT status FROM memory_proposals WHERE fingerprint = ?", (fp,)
            ).fetchone()
            if row is not None:
                if row["status"] == "pending":
                    report.already_pending += 1
                else:
                    # approved, written, or rejected — all mean "you have already ruled on
                    # this". Re-asking is the treadmill.
                    report.already_decided += 1
                continue
            conn.execute(
                """INSERT INTO memory_proposals
                     (fingerprint, subject, kind, claim, quote, source, confidence)
                   VALUES (?,?,?,?,?,?,?)""",
                (fp, claim.subject, claim.kind, claim.claim, claim.quote,
                 claim.source, float(claim.confidence or 0.0)),
            )
            report.proposed += 1
    return report


def pending(limit: int = 200) -> list[dict[str, Any]]:
    """Grouped by subject in the caller's hands, but ordered here — a reviewer reads all of
    one person's claims together or judges each one out of context."""
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_proposals WHERE status = 'pending' "
            "ORDER BY subject COLLATE NOCASE, id LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def by_status(status: str, limit: int = 500) -> list[dict[str, Any]]:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_proposals WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def decide(proposal_id: int, status: str, note: str | None = None) -> bool:
    """Approve or reject one proposal. Returns False if it was already decided.

    A decision is not re-openable through this path on purpose: changing your mind about a
    written memory is `yoyo memory forget`, which leaves a tombstone. Silent undo would make
    the log a poor record of what memory has actually contained.
    """
    if status not in {"approved", "rejected"}:
        raise ValueError("a decision is 'approved' or 'rejected'")
    with db.connection() as conn, db.transaction(conn):
        cur = conn.execute(
            "UPDATE memory_proposals SET status = ?, decided_at = datetime('now'), note = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, note, proposal_id),
        )
        return cur.rowcount > 0


def decide_all(subject: str, status: str) -> int:
    """Every pending claim about one subject at once.

    Offered because the common judgement really is per-subject — "this whole page is about
    world history, not about me" was the shape of the first real failure. Not offered for
    the whole queue: a single button that approves everything is the rubber stamp.
    """
    if status not in {"approved", "rejected"}:
        raise ValueError("a decision is 'approved' or 'rejected'")
    with db.connection() as conn, db.transaction(conn):
        cur = conn.execute(
            "UPDATE memory_proposals SET status = ?, decided_at = datetime('now') "
            "WHERE status = 'pending' AND subject = ?",
            (status, subject),
        )
        return int(cur.rowcount)


def approved_claims() -> list[Claim]:
    """Approved but not yet written — what `apply()` will turn into pages."""
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_proposals WHERE status = 'approved' ORDER BY id"
        ).fetchall()
    return [
        Claim(subject=r["subject"], kind=r["kind"], claim=r["claim"], quote=r["quote"],
              source=r["source"], confidence=float(r["confidence"] or 0.0))
        for r in rows
    ]


def mark_written(claims: list[Claim]) -> int:
    with db.connection() as conn, db.transaction(conn):
        n = 0
        for claim in claims:
            cur = conn.execute(
                "UPDATE memory_proposals SET status = 'written', "
                "written_at = datetime('now') WHERE fingerprint = ? AND status = 'approved'",
                (fingerprint(claim),),
            )
            n += cur.rowcount
    return n


def stats() -> dict[str, Any]:
    """Counts, plus the approval rate — the number that says whether review is working.

    Watch it rather than the page count. High means extraction is proposing the obvious;
    low means it is generating noise you will soon stop reading. Neither is visible from
    "how many memories do I have".
    """
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM memory_proposals GROUP BY status"
        ).fetchall()
    counts = {r["status"]: int(r["n"]) for r in rows}
    decided = counts.get("approved", 0) + counts.get("written", 0) + counts.get("rejected", 0)
    kept = counts.get("approved", 0) + counts.get("written", 0)
    return {
        **{s: counts.get(s, 0) for s in STATUSES},
        "decided": decided,
        "approval_rate": round(kept / decided, 2) if decided else None,
    }

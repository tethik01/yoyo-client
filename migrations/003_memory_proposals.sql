-- Proposed memories, awaiting your decision.
--
-- Until now `memory build` wrote pages straight into the vault, and the only brake was two
-- mechanical gates: the quote must be verbatim, and no claim may cite another wiki page.
-- Those gates prove a claim is TRACEABLE. They cannot tell whether it is WORTH KEEPING —
-- and the first real run proved the difference, producing six pages of world history with
-- every gate green.
--
-- So claims land here first. A row is a proposal; you approve or reject it; only approved
-- rows are ever written to a page.
--
-- `fingerprint` is what makes review survivable. Without a stable identity per claim, every
-- re-run re-proposes everything you already rejected, review becomes a treadmill, and a
-- treadmill gets rubber-stamped — which would leave the system worse off than having no
-- review at all. A rejected claim stays rejected.

CREATE TABLE IF NOT EXISTS memory_proposals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL UNIQUE,
    subject      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    claim        TEXT NOT NULL,
    quote        TEXT NOT NULL,
    source       TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected','written')),
    proposed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at   TEXT,
    written_at   TEXT,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON memory_proposals (status, subject);
CREATE INDEX IF NOT EXISTS idx_proposals_subject ON memory_proposals (subject, kind);

INSERT OR IGNORE INTO schema_version (version) VALUES (3);

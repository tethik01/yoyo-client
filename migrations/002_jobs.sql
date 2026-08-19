-- Jobs: long-running work started from the UI.
--
-- Why a table rather than in-memory state: `yoyo eval` is five minutes and `bench` longer.
-- A browser refresh, a closed laptop lid, or a restarted server must not lose the record of
-- what ran. And once runs are rows, an eval becomes HISTORY rather than a screenshot — the
-- 1.09x -> 3.75x concurrency reversal would have been obvious as two rows in a table
-- instead of a surprise three days later.
--
-- `output` is the accumulated log; `result` is the structured outcome (JSON) for the kinds
-- that have one. Both nullable: a job that is still running has neither yet.

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    args         TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued','running','done','failed','cancelled')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    started_at   TEXT,
    finished_at  TEXT,
    output       TEXT NOT NULL DEFAULT '',
    result       TEXT,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_kind ON jobs (kind, created_at DESC);

INSERT OR IGNORE INTO schema_version (version) VALUES (2);

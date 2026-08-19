-- Continuous memory: a watermark per source, and a way to say "not this one".
--
-- WATERMARKS. `memory propose` re-read every conversation and re-extracted everything, every
-- run. Fine when you press the button occasionally; hopeless as a background sweep, where the
-- cost grows with your entire history and most of the work is redoing what was already done.
-- `messages.id` is monotonic, so "extracted up to N" is all the state needed to make a sweep
-- incremental, resumable, and safe to run every ten minutes forever.
--
-- The watermark advances only after claims are queued. A sweep that dies mid-extraction
-- re-does that slice next time — duplicate proposals are already deduplicated by fingerprint,
-- whereas a lost slice is a memory that never existed.
--
-- REMEMBER FLAG. A second brain that cannot be told to look away is a recorder. Default 1
-- because a memory you have to opt into stays empty; per-conversation because that is the
-- unit you actually think in ("not this thread").

CREATE TABLE IF NOT EXISTS memory_watermarks (
    source_id       TEXT PRIMARY KEY,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    extracted_at    TEXT NOT NULL DEFAULT (datetime('now')),
    claims_seen     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);

ALTER TABLE conversations ADD COLUMN remember INTEGER NOT NULL DEFAULT 1;

INSERT OR IGNORE INTO schema_version (version) VALUES (4);

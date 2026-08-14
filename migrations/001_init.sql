-- Yoyo schema v1.
-- SQLite is the system of record for documents, chunks, conversations and entities.
-- Qdrant holds only vectors + the chunk id needed to join back here.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- corpus ----

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path   TEXT NOT NULL,
    title         TEXT,
    content_hash  TEXT NOT NULL,
    mime_type     TEXT,
    byte_size     INTEGER,
    ingested_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    metadata      TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_path)
);

CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    text          TEXT NOT NULL,
    char_start    INTEGER,
    char_end      INTEGER,
    token_estimate INTEGER,
    embedded_at   TEXT,
    embed_model   TEXT,
    UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_unembedded ON chunks(embedded_at) WHERE embedded_at IS NULL;

-- Sparse half of hybrid retrieval. Contentless FTS table joined by rowid = chunks.id.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

-- --------------------------------------------------------- conversations ----

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    metadata    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
    content          TEXT NOT NULL,
    capability       TEXT,
    model            TEXT,
    prompt_tokens    INTEGER,
    completion_tokens INTEGER,
    latency_ms       INTEGER,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    metadata         TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);

-- Which chunks were actually shown to the model for a given answer. Without this
-- you cannot audit a wrong answer after the fact.
CREATE TABLE IF NOT EXISTS message_citations (
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunk_id    INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    rank        INTEGER NOT NULL,
    score       REAL,
    PRIMARY KEY (message_id, chunk_id)
);

-- -------------------------------------------------------------- entities ----

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived','merged')),
    merged_into INTEGER REFERENCES entities(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    attributes  TEXT NOT NULL DEFAULT '{}',
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    chunk_id   INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    PRIMARY KEY (entity_id, chunk_id)
);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);

SQL_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA cache_size = -64000;

CREATE TABLE IF NOT EXISTS db_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    generation  INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_root     TEXT NOT NULL,
    relative_path   TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    size            INTEGER NOT NULL,
    mtime           REAL,
    chunked         INTEGER NOT NULL DEFAULT 0,
    redundancy      INTEGER NOT NULL DEFAULT 1,
    version_group   TEXT NOT NULL,
    version_seq     INTEGER NOT NULL DEFAULT 1,
    content_text    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at      TEXT,
    UNIQUE(source_root, relative_path, version_seq)
);

CREATE TABLE IF NOT EXISTS blobs (
    blob_id     TEXT PRIMARY KEY,
    pt_size     INTEGER NOT NULL,
    ref_count   INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blob_storage (
    blob_id          TEXT NOT NULL REFERENCES blobs(blob_id),
    backend_name     TEXT NOT NULL,
    storage_path     TEXT NOT NULL,
    stored_size      INTEGER NOT NULL,
    stored_sha256    TEXT NOT NULL,
    compression_algo TEXT,
    compression_level INTEGER,
    encrypted        INTEGER NOT NULL DEFAULT 0,
    nonce            BLOB,
    auth_tag         BLOB,
    uploaded_at      TEXT NOT NULL DEFAULT (datetime('now')),
    verified_at      TEXT,
    PRIMARY KEY (blob_id, backend_name, storage_path)
);

CREATE TABLE IF NOT EXISTS file_chunks (
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    blob_id     TEXT NOT NULL REFERENCES blobs(blob_id),
    pt_offset   INTEGER NOT NULL,
    pt_size     INTEGER NOT NULL,
    PRIMARY KEY (file_id, seq)
);

CREATE TABLE IF NOT EXISTS backends (
    name          TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    config_json   TEXT NOT NULL,
    quota_bytes   INTEGER,
    last_health   TEXT,
    last_seen_ok  TEXT
);

CREATE TABLE IF NOT EXISTS op_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    op_type         TEXT NOT NULL,
    target_json     TEXT NOT NULL,
    precondition    TEXT,
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS integrity_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    check_type    TEXT NOT NULL,
    status        TEXT NOT NULL,
    total_files   INTEGER,
    checked_files INTEGER,
    failed_files  INTEGER,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at  TEXT,
    details       TEXT
);

CREATE TABLE IF NOT EXISTS recovery_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    status      TEXT NOT NULL,
    details     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIEW IF NOT EXISTS backend_usage AS
SELECT backend_name, SUM(stored_size) AS total_bytes
FROM blob_storage
GROUP BY backend_name;
"""

SQL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_files_sha256      ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_source      ON files(source_root);
CREATE INDEX IF NOT EXISTS idx_files_deleted     ON files(deleted_at);
CREATE INDEX IF NOT EXISTS idx_files_version     ON files(version_group);
CREATE INDEX IF NOT EXISTS idx_chunks_blob       ON file_chunks(blob_id);
CREATE INDEX IF NOT EXISTS idx_storage_blob      ON blob_storage(blob_id);
CREATE INDEX IF NOT EXISTS idx_storage_backend   ON blob_storage(backend_name);
CREATE INDEX IF NOT EXISTS idx_oplog_status      ON op_log(status);
CREATE INDEX IF NOT EXISTS idx_oplog_started     ON op_log(started_at);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    relative_path, content_text,
    content='files',
    content_rowid='id'
);
"""

SQL_WAL_PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA journal_size_limit = 67108864;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA cache_size = -64000;
"""

MIGRATIONS: dict[int, str] = {}


def get_schema_version(db) -> int:
    row = db.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] if row and row[0] else 0


def apply_migrations(db):
    for pragma in SQL_WAL_PRAGMAS.strip().split(";"):
        p = pragma.strip()
        if p:
            db.execute(p + ";")
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    current = get_schema_version(db)
    if current == 0:
        for stmt in SQL_SCHEMA.strip().split(";"):
            s = stmt.strip()
            if s:
                db.execute(s + ";")
        for stmt in SQL_INDEXES.strip().split(";"):
            s = stmt.strip()
            if s:
                db.execute(s + ";")
        db.execute("INSERT INTO db_meta (id, generation, instance_id) VALUES (1, 0, ?)", (__import__("uuid").uuid4().hex,))
        db.execute("INSERT INTO schema_version (version) VALUES (1)")
        db.commit()
        return
    for ver in sorted(MIGRATIONS):
        if ver > current:
            db.execute(MIGRATIONS[ver])
            db.execute("INSERT INTO schema_version (version) VALUES (?)", (ver,))
    db.commit()
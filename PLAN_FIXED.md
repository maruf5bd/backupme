# backupme — Distributed Backup System (Fixed Plan)

> This is a corrected version of `PLAN.md`. Each section flags the original bug
> and explains the fix. Search `FIX:` to jump to changes.

## Overview

A Python backup system that stores files across **multiple storage backends**
(local, FTP/SFTP, SMB, SSH, rclone) with **content-addressed dedup**, **N-copy
redundancy**, **chunked large-file support**, **upload verification**, and a
**dual CLI + web dashboard** interface.

The metadata DB is a **single-writer SQLite** with **periodic, snapshot-based
replication** to backends — not a consensus system. This is the single biggest
architectural correction from the original plan.

---

## 0. Summary of Architectural Corrections

| # | Original PLAN bug | Fix |
|---|---|---|
| B1 | "Consensus" across replicas of a single-writer DB | Replaced with snapshot-based DR replication |
| B2 | Pre-write fingerprint (size+mtime+head/tail) on a WAL DB | Use post-checkpoint serial number + page-1 hash |
| B3 | Split-brain handling for a single-writer system | Removed; replaced with "newer wins" rule |
| B4 | Uploading full DB after every write | Replicate WAL frames + periodic full snapshots |
| B5 | Deterministic AES-GCM nonce | Random 96-bit nonce stored alongside ciphertext |
| B6 | `blob_id = SHA256(stored blob)` ambiguity | `blob_id = plaintext SHA256`; storage path encodes transform |
| B7 | Per-blob compression in `dedup_hashes` (one row) | Move transform metadata to `blob_storage` (per blob × variant) |
| B8 | Ambiguous chunk offset/size (plaintext vs stored) | Separate fields: `pt_offset`, `pt_size`, `stored_size` |
| B9 | `chunks.seq = 0 means no chunking` sentinel | Always 0..N-1; add `files.chunked` boolean |
| B10 | "Rebuild from backend scan" loses metadata | Store per-file manifest sidecar with full metadata |
| B11 | Mirror failover only on `health()` | Fall back on download/verify failure too |
| B12 | Recompression deletes old before commit | Write-new → DB-commit → GC-old |
| B13 | Convert `--delete` before durable index | Require N successful replications before delete |
| B14 | "Retry ×3" with no backoff | Exponential backoff with jitter, per-backend circuit breaker |
| B15 | Op-log replay without idempotency contract | Every op carries idempotency key + precondition |
| B16 | Section numbering broken (8.1 under §15, two §10s) | Renumbered |
| B17 | `dedup_chunks` table never used | Removed |
| B18 | `backend_blob_claims` duplicates `chunks` | Removed; derive from `chunks` via view |
| B19 | Crash between primary commit and replica push could "rollback" via majority | Replica is never authoritative; only manual or marked-good snapshots can |
| B20 | Pointer file format underspecified for rebuild | Versioned pointer schema with full chunk manifest |

---

## 1. System Architecture

### Directory Layout

```
backupme/
├── main.py                     # CLI entry point (click)
├── config.py
├── PLAN_FIXED.md
├── requirements.txt
├── config.yaml.example
│
├── core/
│   ├── database.py             # SQLite primary; WAL replication driver
│   ├── schema.py               # Tables + migrations
│   ├── hasher.py
│   ├── compress.py
│   ├── crypto.py               # AES-256-GCM, random nonces, key mgmt
│   ├── dedup.py                # plaintext-hash dedup index
│   ├── manifest.py             # FIX(B10): per-file sidecar manifests
│   ├── op_log.py               # idempotent op log
│   ├── state.py                # crash checkpoint
│   ├── snapshot.py             # VACUUM INTO + WAL frame export
│   ├── replicator.py           # FIX(B1,B4): replicate to backends
│   ├── recover.py              # local recovery + rebuild
│   ├── verify.py               # random-restore verification
│   ├── selector.py             # backend selection
│   ├── uploader.py
│   ├── downloader.py
│   ├── retry.py                # FIX(B14): backoff + circuit breaker
│   └── converter.py
│
├── backends/
│   ├── base.py                 # Abstract StorageBackend
│   ├── local.py
│   ├── ftp_sftp.py
│   ├── smb.py
│   ├── ssh.py
│   └── rclone.py
│
├── cli/                        # subcommand modules
└── web/                        # FastAPI dashboard
```

---

## 2. Metadata Database & Replication

### 2.1 Authoritative Model (FIX: B1, B3, B19)

There is **one writer**: the local primary SQLite at `./backupme.db`.
Backend copies are **disaster-recovery replicas**, not voters.

```
              ┌──────────────┐
              │  Primary DB  │  ← single writer
              │ ./backupme.db│
              └──────┬───────┘
                     │ WAL frames + periodic snapshots
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   backend A    backend B    backend C
  (replica)    (replica)    (replica)
```

**Rules**:
- The primary is always authoritative while it passes integrity checks.
- A replica is *never* used to overwrite a healthy primary.
- A replica is used to recover the primary only when:
  1. Primary fails `PRAGMA integrity_check`, OR
  2. Primary file is missing, OR
  3. User explicitly invokes `backupme db restore --from <backend>`.

This eliminates the "split brain" problem (which couldn't actually occur with
one writer) and the data-loss bug where a stale replica majority could
overwrite a fresh primary.

### 2.2 Generation Counter (FIX: B2)

The primary maintains a monotonically-increasing **generation** column in a
1-row `db_meta` table, bumped inside every write transaction:

```sql
CREATE TABLE db_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    generation  INTEGER NOT NULL,           -- bumped per commit
    instance_id TEXT NOT NULL,              -- random UUID, set at init
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

- `generation` is the canonical "is this newer?" signal — not file mtime, not
  head/tail hash, both of which are unreliable on WAL DBs.
- `instance_id` detects accidentally pointing two installs at the same
  backend root (each fresh install gets a new UUID).

### 2.3 Replication Strategy (FIX: B4)

Replicating the full DB on every write does not scale. Use a tiered strategy:

| Trigger | Action | Cost |
|---|---|---|
| Every commit | Append committed WAL frames to `pending_wal/` queue | µs |
| Every 60 s OR 1000 ops OR 64 MB queued | Upload accumulated WAL bundle to each backend as `__backupme_db__/wal/{generation}.wal.zst` | sec |
| Every hour (or on demand) | `VACUUM INTO` snapshot, upload to `__backupme_db__/snapshots/`, prune old WAL bundles older than 2 snapshots | min |
| Every 24 h | Verify replica chain by replaying snapshot+WAL on a tmp DB and comparing `generation` | min |

The replicator runs **asynchronously**. Backups never block on replica upload.
A failed backend just falls behind in its WAL chain; on recovery it catches up.

### 2.4 Backend Layout for DB

```
{backend_root}/
└── __backupme_db__/
    ├── instance.json               # {instance_id, schema_version}
    ├── snapshots/
    │   ├── snap-{gen}-{ts}.db.zst
    │   └── snap-{gen}-{ts}.db.zst.sha256
    ├── wal/
    │   ├── wal-{gen_from}-{gen_to}.zst
    │   └── ...
    └── corrupt/
        └── backupme.db.corrupt.{ts}
```

`__backupme_db__` is a reserved prefix; never used for user files.

### 2.5 Local Recovery Procedure

```
recover_database():
  if PRAGMA integrity_check passes: return OK

  preserve_corrupt_copy()

  candidates = []
  for each backend in healthy(backends):
      candidates.append(reconstruct_from(backend))   # snapshot + WAL replay

  if no candidates:
      return rebuild_from_manifests()                # FIX(B10), see §10

  pick the candidate with the highest generation
  swap into ./backupme.db
  log recovery_event
```

Notes:
- We choose **highest generation**, not majority. Majority is meaningless
  when one writer was the source.
- If two candidates tie, pick the one whose `instance_id` matches local;
  warn if mismatch.

### 2.6 Removed: Split Brain, Pre-Write Consensus

Both removed. Single-writer model means:
- No pre-write check needed (no other writers to disagree with).
- No split brain (no concurrent writers).

---

## 3. Database Schema

### 3.1 Tables

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;            -- FIX: was NORMAL; we need durability
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA cache_size = -64000;

-- Generation/instance metadata (see §2.2)
CREATE TABLE db_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    generation  INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Files
CREATE TABLE files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_root     TEXT NOT NULL,
    relative_path   TEXT NOT NULL,
    sha256          TEXT NOT NULL,            -- plaintext SHA256 of full file
    size            INTEGER NOT NULL,
    mtime           REAL,
    chunked         INTEGER NOT NULL,         -- FIX(B9): explicit boolean
    redundancy      INTEGER NOT NULL DEFAULT 1,
    version_group   TEXT NOT NULL,
    version_seq     INTEGER NOT NULL DEFAULT 1,
    content_text    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at      TEXT,
    UNIQUE(source_root, relative_path, version_seq)
);

-- Plaintext-content registry. blob_id = plaintext SHA256. (FIX B6)
CREATE TABLE blobs (
    blob_id     TEXT PRIMARY KEY,             -- = SHA256(plaintext)
    pt_size     INTEGER NOT NULL,
    ref_count   INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per (blob × backend × storage variant). FIX(B7)
-- A "variant" is a specific (compression, encryption) encoding of the blob.
-- The same plaintext may be stored in different variants on different backends
-- (e.g. one backend has an old gzip blob, another has a new zstd blob).
CREATE TABLE blob_storage (
    blob_id          TEXT NOT NULL REFERENCES blobs(blob_id),
    backend_name     TEXT NOT NULL,
    storage_path     TEXT NOT NULL,           -- path on backend
    stored_size      INTEGER NOT NULL,
    stored_sha256    TEXT NOT NULL,           -- SHA256 of bytes-on-disk (post-transform)
    compression_algo TEXT,                    -- 'zstd'|'gzip'|'lz4'|NULL
    compression_level INTEGER,
    encrypted        INTEGER NOT NULL DEFAULT 0,
    nonce            BLOB,                    -- FIX(B5): random GCM nonce per blob
    auth_tag         BLOB,                    -- GCM auth tag
    uploaded_at      TEXT NOT NULL DEFAULT (datetime('now')),
    verified_at      TEXT,
    PRIMARY KEY (blob_id, backend_name, storage_path)
);

-- Chunk map: a file is reassembled from N chunks; each chunk is one blob. FIX(B8)
CREATE TABLE file_chunks (
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,             -- 0..N-1, dense
    blob_id     TEXT NOT NULL REFERENCES blobs(blob_id),
    pt_offset   INTEGER NOT NULL,             -- offset in reassembled plaintext
    pt_size     INTEGER NOT NULL,             -- plaintext size of this chunk
    PRIMARY KEY (file_id, seq)
);

-- Backend metadata
CREATE TABLE backends (
    name          TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    config_json   TEXT NOT NULL,
    quota_bytes   INTEGER,
    last_health   TEXT,
    last_seen_ok  TEXT
);

-- Idempotent op log. FIX(B15)
CREATE TABLE op_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,     -- caller-generated UUID
    op_type         TEXT NOT NULL,            -- 'upload_blob', 'delete_blob', 'index_file', ...
    target_json     TEXT NOT NULL,            -- full op payload (idempotent on replay)
    precondition    TEXT,                     -- e.g. "blob X must not already exist"
    status          TEXT NOT NULL,            -- 'pending'|'committed'|'rolled_back'|'failed'
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    last_error      TEXT
);

-- Integrity / recovery audit
CREATE TABLE integrity_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    check_type    TEXT NOT NULL,              -- 'random'|'full'|'scrub'
    status        TEXT NOT NULL,
    total_files   INTEGER,
    checked_files INTEGER,
    failed_files  INTEGER,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at  TEXT,
    details       TEXT
);

CREATE TABLE recovery_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    status      TEXT NOT NULL,
    details     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Removed tables (FIX B17, B18):
- `dedup_chunks` — never used; chunk dedup is just blob dedup.
- `backend_blob_claims` — derivable from `blob_storage` via view.
- `backend_usage` — replaced with view that sums `blob_storage.stored_size`
  per backend (no drift between cached value and reality).

```sql
CREATE VIEW backend_usage AS
SELECT backend_name, SUM(stored_size) AS total_bytes
FROM blob_storage
GROUP BY backend_name;
```

### 3.2 Indexes

```sql
CREATE INDEX idx_files_sha256       ON files(sha256);
CREATE INDEX idx_files_source       ON files(source_root);
CREATE INDEX idx_files_deleted      ON files(deleted_at);
CREATE INDEX idx_files_version      ON files(version_group);
CREATE INDEX idx_chunks_blob        ON file_chunks(blob_id);
CREATE INDEX idx_storage_blob       ON blob_storage(blob_id);
CREATE INDEX idx_storage_backend    ON blob_storage(backend_name);
CREATE INDEX idx_oplog_status       ON op_log(status);
CREATE INDEX idx_oplog_started      ON op_log(started_at);

CREATE VIRTUAL TABLE files_fts USING fts5(
    relative_path, content_text,
    content='files', content_rowid='id'
);
```

---

## 4. Storage Backends

### 4.1 Abstract Interface

```python
class StorageBackend:
    name(self) -> str
    type(self) -> str

    upload(self, local_path, remote_rel_path) -> str    # returns stored_sha256
    download(self, remote_rel_path, local_path) -> None
    delete(self, remote_rel_path) -> None
    exists(self, remote_rel_path) -> bool
    list_files(self, prefix="") -> Iterator[dict]       # streaming
    walk(self, prefix="") -> Iterator[dict]             # recursive

    free_space(self) -> int | None
    total_space(self) -> int | None
    quota(self) -> int | None

    hash(self, remote_rel_path) -> str | None           # None if backend can't hash
    health(self) -> bool
    fingerprint(self, remote_rel_path) -> dict          # {size, mtime, etag?}

    # DB replication helpers
    db_upload_chunk(self, local_path, remote_path) -> None
    db_download(self, remote_path, local_path) -> None
    db_list(self, kind: Literal["snapshots","wal"]) -> list[str]
```

`hash()` returns `None` for backends that can't compute server-side hashes
without a download. Caller handles the fallback.

### 4.2 Backend Implementations

| Backend | Library | free_space | hash | Notes |
|---|---|---|---|---|
| `local` | `shutil`, `os` | `shutil.disk_usage()` | direct read | reference impl |
| `ftp_sftp` | `ftplib`, `paramiko` | FTP `STAT` / SFTP `statvfs` | None for FTP, server checksum if available | reconnect on disconnect |
| `smb` | `smbprotocol` | SMB query info | None (download+hash) | replaces deprecated `pysmb` |
| `ssh` | `paramiko` | remote `df` | remote `sha256sum` | key-based auth |
| `rclone` | subprocess CLI | `rclone about --json` | `rclone hashsum SHA256` | 50+ providers |

---

## 5. On-Backend Folder Layout

```
{backend_root}/
├── __backupme_db__/             # see §2.4
│
├── objects/                     # content-addressed blob store (variant suffix)
│   └── {b0}/{b1}/
│       ├── {plaintext_sha256}.zst.aes      # zstd + AES-GCM
│       ├── {plaintext_sha256}.zst          # zstd, no encryption
│       ├── {plaintext_sha256}.aes          # raw + AES-GCM
│       └── {plaintext_sha256}              # raw
│
├── manifests/                   # FIX(B10): per-file sidecar manifests
│   └── {b0}/{b1}/{file_uuid}.manifest.json
│
└── tree/                        # browsable mirror of source paths
    └── {source_root_id}/...
        └── relative/path/file.ext.backupme    # tiny pointer file
```

### 5.1 Pointer Files (FIX: B20)

```json
{
  "v": 1,
  "file_uuid": "01HABC...",
  "source_root": "C:/Users/Maruf/Docs",
  "relative_path": "report.pdf",
  "size": 1048576,
  "sha256": "abc...",
  "mtime": 1736899200.0,
  "version_group": "g_xyz",
  "version_seq": 3,
  "manifest_path": "manifests/ab/cd/01HABC.manifest.json"
}
```

### 5.2 Per-File Manifest (FIX: B10)

```json
{
  "v": 1,
  "file_uuid": "01HABC...",
  "source_root": "...",
  "relative_path": "...",
  "sha256": "...",
  "size": 1048576,
  "mtime": 1736899200.0,
  "version_group": "g_xyz",
  "version_seq": 3,
  "redundancy": 2,
  "chunks": [
    {
      "seq": 0,
      "blob_id": "sha256-of-plaintext",
      "pt_offset": 0,
      "pt_size": 65536,
      "storage": [
        { "backend": "usb_drive", "path": "objects/ab/cd/...zst.aes",
          "stored_size": 41234, "stored_sha256": "...", "algo": "zstd",
          "level": 3, "encrypted": true }
      ]
    }
  ]
}
```

**This makes "rebuild from backend scan" actually possible.** Walk
`manifests/`, parse each, recreate `files` + `file_chunks` + `blobs` +
`blob_storage` rows. The DB becomes a derived index of authoritative
manifests.

---

## 6. Compression

### 6.1 Pipeline

```
plaintext_chunk → detect_compressibility() → maybe_compress → maybe_encrypt → upload
```

Compress THEN encrypt — encrypted output is incompressible.

### 6.2 File-Type Detection

| Category | Extensions | Action |
|---|---|---|
| Media | `.jpg .png .mp4 .mp3 .mov .webp .heic` | Skip |
| Archives | `.zip .7z .rar .gz .bz2 .xz .zst` | Skip |
| Text | `.txt .md .log .csv .json .xml .yaml .yml` | zstd -19 |
| Code | `.py .js .ts .html .css .c .h .rs .go .java` | zstd -10 |
| Unknown low-entropy | (sample 64 KB, entropy < 7.0) | zstd -3 |
| Unknown high-entropy | (entropy ≥ 7.0) | Skip |

Magic-byte check overrides extension for a few high-impact cases (zip, gzip,
zstd) to handle misnamed files.

### 6.3 Adaptive Levels

```python
def choose_compression_level(pt_size):
    if pt_size <  64 * 1024:           return ('zstd', 19)
    if pt_size <   1024 * 1024:        return ('zstd', 10)
    if pt_size <  64 * 1024 * 1024:    return ('zstd', 3)
    return ('zstd', 1)
```

### 6.4 Recompression Migration (FIX: B12)

Order matters for crash safety:

```
For each blob to recompress:
  1. Recompress plaintext locally → new bytes
  2. Upload new variant to backend at {blob}.zst.aes (or v2 path)
  3. Verify new variant
  4. INSERT new row into blob_storage (commit)
  5. (only now) DELETE old variant from backend
  6. DELETE old blob_storage row (commit)
```

Until step 4 commits, the system can use either variant. If we crash between
step 5 and 6 we have an orphaned row pointing nowhere; the next scrub deletes
it. We never lose readable data.

---

## 7. Encryption (FIX: B5)

### 7.1 Key Management

```
master_password → argon2id(salt, memory=512MB, ops=3, parallelism=4)
                → 256-bit key-encryption-key (KEK)

KEK + random 256-bit data-encryption-key (DEK)
    → ~/.backupme/key.enc = AES-GCM(KEK, DEK)
```

DEK never leaves memory. Rotating master password re-encrypts the small
key file but does not require re-encrypting any blobs.

### 7.2 Per-Blob Encryption

```
blob_id = SHA256(plaintext)         ← stable across compression/encryption
nonce  = csprng(96 bits)             ← random, unique per encryption
ct, tag = AES-256-GCM(DEK, nonce, plaintext_or_compressed)
stored_bytes = nonce || ct || tag    ← 12 + N + 16 bytes
```

Storage row records `nonce` and `auth_tag` separately for clarity, but the
blob file on disk is the concatenation.

**Why this is correct**: GCM requires a unique (key, nonce) pair per
encryption. A random 96-bit nonce gives ~2⁻³² collision probability per
2⁶⁴ encryptions — safe in practice. The original "deterministic nonce =
SHA256(position)" reuses (key, nonce) the moment the same plaintext is
re-uploaded, which catastrophically breaks GCM.

**Tradeoff**: We lose convergent encryption (identical plaintext → identical
ciphertext). That is fine: dedup happens before encryption via `blob_id =
SHA256(plaintext)`. We never store two encrypted variants of the same
blob unless the user explicitly recompresses.

### 7.3 Optional Convergent Mode

For users who *want* convergent encryption across machines:

```yaml
encryption:
  mode: convergent          # default: random
```

In convergent mode the nonce is `HMAC-SHA256(DEK, plaintext_sha256)[:12]`.
This is safer than `SHA256(position)` because it depends on the key. Document
the privacy tradeoff: equality of stored blobs leaks plaintext equality.

---

## 8. Retention & Versioning

```yaml
retention:
  enabled: true
  versions: 5
  daily: 7
  weekly: 4
  monthly: 12
```

On re-backup:
1. Compute new file's plaintext SHA256.
2. If unchanged → no new version, only update `mtime`/`updated_at`.
3. If changed → insert new `files` row with same `version_group`,
   `version_seq + 1`. Do not delete old row; pruner handles GC.

Pruner (runs daily):
1. Group `files` by `version_group`.
2. Apply retention windows.
3. Soft-delete pruned rows (`deleted_at = now`).
4. Decrement `blobs.ref_count` for each chunk of pruned files.
5. Blobs with `ref_count = 0` are GC'd from backends in a separate sweep
   (delayed by `gc_grace_period` default 7 days, in case we need to undo).

---

## 9. Backend Selection

### 9.1 Auto-Select

```
healthy = [b for b in backends if b.health()]
healthy.sort(key=lambda b:
    (b.priority,                              # explicit priority first
     -b.recent_failure_count,                 # fewer failures = better
     b.free_space() * b.weight))              # then by space
return healthy[:N]                            # N = redundancy
```

For redundancy ≥ 2: enforce backends are different *types* when possible
(prefer `local + cloud` over `local + local`).

### 9.2 Manual `--to`

Existing logic preserved. Fuzzy match via `difflib` on miss.

---

## 10. Upload Flow

### 10.1 Single Copy

```
1. Compute file SHA256 streaming (chunked)
2. Look up in blobs table:
   ├── EXISTS with refcount > 0 → just insert files row + file_chunks → DONE
   └── NEW or refcount == 0 → continue
3. Decide: chunked (size ≥ chunk_min_size, default 1 MiB) or single blob
4. For each chunk (or whole file):
   a. Compute chunk plaintext SHA256 → blob_id
   b. If blob exists in blob_storage on a healthy backend → skip upload
   c. Else: maybe_compress → maybe_encrypt → upload to selected backend
   d. Verify (see §10.3)
   e. INSERT blob_storage row
5. INSERT files row, file_chunks rows, increment blobs.ref_count
6. Write per-file manifest (§5.2) and pointer file (§5.1) to all backends
7. Append op_log.committed; bump db_meta.generation
```

All steps inside one DB transaction except the upload itself, which is
recorded via op_log idempotency key (§3 `op_log`).

### 10.2 N-Copy Redundancy

Generalized from "mirror = 2" to arbitrary `redundancy = N`:

```
For each chunk:
  targets = selector.pick(n=N, exclude=[])
  parallel_upload(chunk_bytes, targets)
  parallel_verify(targets)
  for t in targets: INSERT blob_storage row
```

If fewer than N healthy backends are available, upload to all healthy ones,
record a `partial_redundancy` flag in `files`, and emit a warning. Daemon
re-replicates when more backends come online.

### 10.3 Upload Verification (FIX: B14)

```python
def upload_with_verify(backend, local_chunk, remote_path):
    expected_hash = sha256(local_chunk)
    for attempt in retry.exponential(max_attempts=4, base=1.0, max=30.0):
        try:
            backend.upload(local_chunk, remote_path)
            remote_hash = backend.hash(remote_path)
            if remote_hash is None:
                # backend can't hash server-side
                with tmp() as t:
                    backend.download(remote_path, t)
                    remote_hash = sha256_file(t)
            if remote_hash == expected_hash:
                return True
        except TransientError as e:
            attempt.note(e)
    selector.report_failure(backend)
    return False     # caller falls back to next backend
```

Per-backend circuit breaker: after 5 consecutive failures, mark unhealthy
for `cooldown_seconds` (default 300) and skip in selection.

---

## 10.5 Convert Flow (FIX: B13)

```
backupme convert BACKEND_NAME [--prefix /path] [--target BACKEND]
                              [--delete] [--resume]
```

```
1. Walk(prefix) → stream of files
2. For each file (skip __backupme_db__/, manifests/, objects/, tree/ prefixes):
   a. Resume check: rel_path already in files for this backend? skip
   b. Stream-hash plaintext → blob_id
   c. If --target differs, route uploads to target
   d. Run normal upload pipeline (compress/encrypt/upload/verify/index)
   e. Write manifest + pointer
   f. If --delete:
        REQUIRE: blob_storage shows ≥ redundancy copies, all verified
        REQUIRE: manifest replicated to ≥ redundancy backends
        Only then delete original.
   g. Checkpoint after every 50 files
```

The original plan's `--delete` was unsafe because it deleted before any
durable, redundant index existed.

---

## 11. Download Flow

### 11.1 Normal (FIX: B11)

```
1. Resolve file (id, source_root+rel_path, or pointer)
2. Read file_chunks ordered by seq
3. For each chunk:
   for backend in candidates(blob_id):     # ordered by health, latency, type
       try:
           data = backend.download(blob_storage.path)
           data = maybe_decrypt(data, nonce, tag)
           data = maybe_decompress(data, algo)
           if sha256(data) == blob_id: break    # plaintext check
       except (DownloadError, IntegrityError) as e:
           log.warn(...); continue
   else:
       raise UnrecoverableError(blob_id)
4. Reassemble in seq order; final SHA256 must equal files.sha256
```

Failover triggers on **actual failure** (download error OR plaintext hash
mismatch), not just `health()` returning false.

### 11.2 Verify-Restore

```
backupme verify [--count 5] [--all] [--recent 100] [--backend NAME]
```

Full restore-and-hash, results into `integrity_checks`. Daemon runs weekly.

---

## 12. Crash Protection

### 12.1 State File

```json
{
  "operation": "backup",
  "pid": 12345,
  "started_at": "2026-05-29T10:00:00Z",
  "source": "C:/Users/Maruf/Docs",
  "current_file": "report.pdf",
  "completed_files": 47,
  "total_files": 103
}
```

Updated atomically (write to `.tmp`, rename) every 30 ops.

### 12.2 Startup Recovery

```
1. If state.json exists with PID not running:
   a. Scan op_log for status='pending'
   b. For each pending op:
      - Check precondition (still applicable?)
      - Verify external state (is the blob actually on the backend?)
      - If "yes, upload succeeded" → mark committed, run post-commit DB updates
      - If "no, upload absent" → mark rolled_back
   c. Prompt "Resume interrupted backup? [Y/n]" (or auto-resume in daemon)
   d. Clear state.json
```

Idempotency key on every op (§3 `op_log`) means replay is safe.

---

## 13. Full-Text Search

FTS5 indexes path + extracted text content. Extraction allowlist:
`.txt .md .log .csv .json .xml .yaml .yml .py .js .ts .html .css`. PDFs and
DOCX optional via `pdftotext` / `python-docx` if installed; degrade silently
if not.

```
backupme search "budget 2026"               # path/name
backupme search --content "meeting notes"   # full text
backupme search --source photos "beach"
```

---

## 14. Integrity Report

```
backupme report [--format html|json|text]
GET /api/report
```

Report covers: file count, total/dedup/compressed sizes, redundancy
coverage, last verification, per-backend health, recent recovery events,
recommendations. Computes from views over current schema (no cached
mismatches).

---

## 15. Snapshot & Recovery System

### 15.1 Automatic DB Snapshots (FIX: B16, renumbered)

```
Every snapshot_interval (default 1 h):
  1. PRAGMA wal_checkpoint(TRUNCATE)
  2. VACUUM INTO snapshots/snap-{generation}-{ts}.db
  3. Compress with zstd -3 → .db.zst, append .sha256 sidecar
  4. Upload to each backend's __backupme_db__/snapshots/
  5. Prune local snapshots older than retention_days (default 7) AND
     keeping at least min_local_snapshots (default 3)
  6. On backends, prune snapshots older than 2 newer snapshots and
     all WAL bundles superseded by them
```

### 15.2 Auto-Recovery (Replaces "consensus")

```python
def recover_database():
    if integrity_check_passes(): return

    preserve_corrupt_copy()

    candidates = []
    for backend in all_backends():
        try:
            with tmp_dir() as tmp:
                dl_latest_snapshot(backend, tmp)
                replay_wal_bundles(backend, tmp)
                if integrity_check(tmp / "backupme.db"):
                    gen = read_generation(tmp / "backupme.db")
                    candidates.append((gen, tmp.copy_to_safe()))
        except BackendUnavailable:
            continue

    if not candidates:
        return rebuild_from_manifests()    # see §15.3

    # FIX(B19): pick highest generation, not majority
    best = max(candidates, key=lambda c: c[0])
    swap_into_primary(best[1])
    log_recovery("db_restore", "ok", f"restored from gen={best[0]}")
```

### 15.3 Rebuild From Manifests (FIX: B10)

```
rebuild_database_from_backends():
  1. Create fresh DB with schema
  2. For each backend, walk(manifests/):
       parse manifest → INSERT files, file_chunks, blobs (refcount=1 if new),
                        blob_storage rows
  3. For files appearing on multiple backends:
       use highest version_seq, merge storage rows
  4. Recompute blobs.ref_count from file_chunks
  5. Verify: random sample of files, downstream restore + hash
  6. Bump generation, set new instance_id, log rebuild_event
```

Because manifests carry full file metadata, the rebuilt DB is functionally
equivalent (modulo non-essential audit history).

---

## 16. CLI

```bash
# Backup
backupme backup [--source PATH] [--to BACKEND] [--redundancy N]

# Convert
backupme convert BACKEND [--prefix PATH] [--delete] [--target BACKEND] [--resume]

# Restore / Download
backupme download <file_id|path> [--from BACKEND] [--dest PATH]
backupme restore [--source ROOT] [--dest PATH]

# List / Search
backupme list [--source ROOT] [--backend NAME] [--hash VALUE]
backupme search <query> [--content] [--source ROOT]
backupme status

# Verify
backupme verify [--count N] [--backend NAME] [--all] [--recent N]

# Encryption
backupme encrypt init
backupme encrypt change-password
backupme encrypt status

# Compression
backupme recompress [--algo zstd] [--level 3] [--backend NAME]

# Reports
backupme report [--format html|json|text]

# DB
backupme db check [--full]
backupme db snapshot
backupme db restore [--from BACKEND] [--snapshot FILE]
backupme db oplog [--pending]
backupme db info
backupme db rebuild

# Integrity
backupme check-consistency
backupme scrub [--backend NAME]

# Daemon / Web
backupme daemon start|stop|status
backupme web [--port 8080] [--host 0.0.0.0]

# Config
backupme config check
```

---

## 17. Web Dashboard

### 17.1 Pages

| Route | Page | Notes |
|---|---|---|
| `/` | Dashboard | counts, dedup ratio, compression ratio, per-backend usage chart, DB health |
| `/files` | Files | searchable (FTS-backed) table; per-row download; redundancy column shows N/N |
| `/backends` | Backends | health, free/total/quota, last seen, circuit-breaker state |
| `/logs` | Activity | filtered op_log + recovery_events |
| `/database` | DB Health | integrity status, snapshot/WAL list, recovery history; manual buttons |
| `/report` | Report | full integrity report with charts |
| `/settings` | Settings | view config, toggle daemon, trigger backup |

### 17.2 API

```
GET /api/status
GET /api/backends
GET /api/files?q=&backend=&page=
GET /api/files/{id}
GET /api/sources
GET /api/logs?level=&limit=
GET /api/db/info
GET /api/db/snapshots
GET /api/db/recovery
GET /api/report
POST /api/backup
POST /api/verify
```

---

## 18. Open Questions / Future

- Erasure coding (Reed-Solomon) instead of N-copy redundancy for large
  archives — same durability at fewer bytes.
- Streaming encryption (chunk-level GCM) for very large blobs to avoid
  whole-blob memory residency.
- Server-side "lite agent" for SSH backends to compute hashes without
  full download.

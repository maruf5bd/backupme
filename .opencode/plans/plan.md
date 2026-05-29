# backupme — Distributed Backup System

## Overview

A Python backup system that stores files across **multiple storage backends** (local, FTP/SFTP, SMB, SSH, rclone) with **global SHA256 dedup**, **2-copy mirror mode**, **chunked large-file support**, **upload verification**, and a **dual CLI + web dashboard** interface.

Every component is built around a **consensus-replicated SQLite database** that ensures data integrity across all backends.

---

## 1. System Architecture

### Directory Layout

```
backupme/
├── main.py                      # CLI entry point (click)
├── config.py                    # YAML config loader + env vars
├── plan.md                      # This file
├── requirements.txt
├── config.yaml.example
│
├── core/
│   ├── __init__.py
│   ├── database.py              # Primary SQLite + consensus check
│   ├── schema.py                # All table definitions + migrations
│   ├── hasher.py                # Chunked SHA256 reader
│   ├── compress.py              # File-type detection + adaptive compression
│   ├── crypto.py                # AES-256-GCM encrypt/decrypt, key management
│   ├── dedup_store.py           # dedup_hashes + dedup_chunks tables
│   ├── index.py                 # files + chunks + backend_usage + FTS tables
│   ├── op_log.py                # Append-only operation log
│   ├── state.py                 # Crash protection checkpoint file
│   ├── snapshot.py              # VACUUM INTO snapshots + retention
│   ├── recover.py               # Corruption detection + restore + rebuild
│   ├── verify.py                # Random-file restore verification
│   ├── selector.py              # Backend selection (auto/manual/quota)
│   ├── uploader.py              # Upload orchestrator
│   ├── downloader.py            # Download orchestrator
│   └── converter.py             # Convert existing backend files to backupme format
│
├── backends/
│   ├── base.py                  # Abstract StorageBackend (12 methods)
│   ├── local.py
│   ├── ftp_sftp.py
│   ├── smb.py
│   ├── ssh.py
│   └── rclone.py
│
├── cli/
│   ├── backup_cmd.py
│   ├── download_cmd.py
│   ├── list_cmd.py
│   ├── search_cmd.py            # Full-text search
│   ├── convert_cmd.py           # Convert existing backend files to backupme format
│   ├── db_cmd.py
│   ├── web_cmd.py
│   ├── daemon_cmd.py
│   ├── encrypt_cmd.py           # Key management
│   └── report_cmd.py            # Integrity report
│
├── mirror.py                    # Redundancy-2 logic + consistency
├── scheduler.py                 # Daemon watcher
│
└── web/
    ├── app.py                   # FastAPI + uvicorn
    ├── routes.py                # Pages + API endpoints
    ├── templates/               # Jinja2 (7 pages)
    │   ├── base.html
    │   ├── dashboard.html
    │   ├── files.html
    │   ├── backends.html
    │   ├── logs.html
    │   ├── database.html
    │   └── report.html          # Integrity report page
    └── static/                  # CSS + JS
```

---

## 2. Dual Database Consensus Protocol

This is the **most critical system** in backupme. Data integrity depends on it.

### 2.1 What & Where

| DB | Type | Location | Purpose |
|---|---|---|---|
| **Primary** | SQLite | `./backupme.db` | Active read/write database |
| **Replicas** | SQLite | `__backupme_db__/backupme.db` on **every backend** | Disaster recovery, consensus |

### 2.2 Pre-Write Consensus Check (Every Write)

Before every write transaction, backupme verifies that all DB copies agree on the current state.

```
PRE-WRITE PROTOCOL:
1. Take a "fingerprint" of the primary DB: size + mtime + SHA256(first 4KB + last 4KB)
   → This is fast, no full download needed
2. For each backend replica, fetch its fingerprint (size, mtime, head/tail hash)
   → For remote backends: use a lightweight head request or partial read
3. Compare all fingerprints against the cached "last known good" fingerprint
   → All match → proceed with write (consensus OK)
   → Any mismatch → escalate to FULL CONSENSUS CHECK
```

```
FULL CONSENSUS CHECK (triggered on mismatch or suspicion):
1. FULLY DOWNLOAD the DB from every backend replica
2. Compute SHA256 of every copy (primary + all replicas)
3. Group by SHA256 — the largest group is the "truth majority"
4. Apply the majority version:
   a. If PRIMARY is NOT in the majority → replace primary with majority version
   b. For each REPLICA not in the majority → replace with majority version
   c. For any CORRUPT replica (integrity_check fails) → 
      - Rename to backupme.db.corrupt.{timestamp}
      - Replace with majority version
5. If NO majority exists (split brain, e.g. 4 backends, 2v2):
   a. Find the LATEST SNAPSHOT that matches any group
   b. Apply that snapshot as the truth
   c. Replay op_log entries after snapshot time
   d. If still unresolved → log CRITICAL, require manual intervention
6. Log the entire event to recovery_events table
```

### 2.3 Post-Write Replication

After every successful write transaction:

```
POST-WRITE PROTOCOL:
1. Flush WAL: PRAGMA wal_checkpoint(TRUNCATE)
2. Upload primary DB to EVERY backend as __backupme_db__/backupme.db
   → Overwrites the old replica
3. Verify upload: download + SHA256 compare
4. Update cached "last known good" fingerprint
5. If any backend upload fails → log warning, retry on next scheduled cycle
```

### 2.4 Optimization: Tiered Checks

| Check Level | Frequency | Cost | What It Does |
|---|---|---|---|
| **Fast** | Every write | ~milliseconds | Compare fingerprint (size+mtime+head/tail hash) vs cached |
| **Medium** | Every 10 writes | ~seconds | Full SHA256 of primary vs SHA256 of all replicas (remote hash only, no download) |
| **Full** | Health check failure | ~minutes | Full download + integrity_check of every replica |

This avoids downloading the full DB from every backend on every write.

### 2.5 DB Location on Backends

Each backend stores the DB at a reserved path:

```
Backend root:
├── __backupme_db__/
│   ├── backupme.db                # Current replica
│   ├── snapshots/
│   │   ├── backupme-v20260528-120000.db
│   │   ├── backupme-v20260528-180000.db
│   │   └── ...
│   └── corrupt/
│       └── backupme.db.corrupt.20260528-120500  # Preserved for forensics
├── user_files/
│   └── ...
```

The `__backupme_db__` prefix is **reserved** — no user file path will ever start with this.

### 2.6 Recovery Scenarios

| Scenario | Action |
|---|---|
| Primary DB corrupted | Restore from majority replica → log recovery event |
| One replica corrupted | Save as `.corrupt.{ts}` → replace with majority copy |
| All replicas + primary corrupted | **Full rebuild**: scan all backends for files + chunks, rebuild DB from scratch |
| Split brain (no majority) | Use latest snapshot → replay op_log → if still split, manual intervention |
| Backend offline during write | Skip that replica, retry on next write cycle, log warning |
| Backend permanently lost | Integrity check detects missing replica → log critical, flag for user |

---

## 3. Database Schema (10 Tables)

```sql
-- Schema versioning
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- File metadata — one entry per backed-up file
CREATE TABLE files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path     TEXT NOT NULL,
    relative_path   TEXT NOT NULL,
    source_root     TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    size            INTEGER NOT NULL,
    mtime           REAL,
    redundancy      INTEGER NOT NULL DEFAULT 1,   -- 1=normal, 2=mirror
    version_group   TEXT,                          -- groups versions of same file path
    version_seq     INTEGER DEFAULT 1,              -- 1=oldest, N=newest
    content_text    TEXT,                           -- extracted text for FTS search (nullable)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at      TEXT,
    UNIQUE(source_root, relative_path)
);

-- Chunk mapping — one row per chunk per file per backend
CREATE TABLE chunks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id           INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL,            -- 0=no chunking, 1,2,3...
    backend_name      TEXT NOT NULL,
    blob_id           TEXT NOT NULL,
    offset            INTEGER NOT NULL,
    size              INTEGER NOT NULL,
    plaintext_size    INTEGER NOT NULL,
    sha256            TEXT NOT NULL,
    plaintext_sha256  TEXT,
    encrypted         INTEGER NOT NULL DEFAULT 0,
    uploaded_at       TEXT NOT NULL DEFAULT (datetime('now')),
    verified          INTEGER NOT NULL DEFAULT 0,
    verify_attempts   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(file_id, backend_name, blob_id)
);

-- Global plaintext dedup — SHA256 → reference count
CREATE TABLE dedup_hashes (
    sha256              TEXT PRIMARY KEY,           -- plaintext SHA256
    compressed_sha256   TEXT,                        -- SHA256 of compressed blob (nullable)
    size                INTEGER NOT NULL,            -- plaintext size
    compressed_size     INTEGER,                      -- compressed blob size (nullable)
    compression_algo    TEXT,                         -- 'zstd', 'gzip', 'lz4', NULL
    compression_level   INTEGER,                      -- compression level used
    encrypted           INTEGER NOT NULL DEFAULT 0,   -- 1 if blob is encrypted
    ref_count           INTEGER NOT NULL DEFAULT 0,
    first_seen          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Chunk-level dedup (optional)
CREATE TABLE dedup_chunks (
    plaintext_sha256    TEXT PRIMARY KEY,
    size                INTEGER NOT NULL,
    ref_count           INTEGER NOT NULL DEFAULT 0,
    first_seen          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Self-tracked backend storage usage
CREATE TABLE backend_usage (
    backend_name    TEXT PRIMARY KEY,
    total_bytes     INTEGER NOT NULL DEFAULT 0,
    last_updated    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Reference counting per blob on each backend
CREATE TABLE backend_blob_claims (
    backend_name    TEXT NOT NULL,
    blob_id         TEXT NOT NULL,
    size            INTEGER NOT NULL,
    claim_count     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (backend_name, blob_id)
);

-- Integrity check history
CREATE TABLE integrity_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    check_type      TEXT NOT NULL,
    status          TEXT NOT NULL,
    total_files     INTEGER,
    checked_files   INTEGER,
    failed_files    INTEGER,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    details         TEXT
);

-- Recovery audit log
CREATE TABLE recovery_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,     -- 'db_restore', 'consensus_fix', 'split_brain', 'rebuild'
    status          TEXT NOT NULL,
    details         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Operation log for replay after snapshot restore
CREATE TABLE op_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    op_type         TEXT NOT NULL,     -- 'upload', 'download', 'delete', 'verify'
    entity_type     TEXT NOT NULL,     -- 'file', 'chunk', 'backend', 'db'
    entity_id       TEXT,
    sha256          TEXT,
    status          TEXT NOT NULL,     -- 'started', 'committed', 'rolled_back', 'failed'
    metadata        TEXT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT
);
```

### Indexes

```sql
CREATE INDEX idx_files_sha256      ON files(sha256);
CREATE INDEX idx_files_source      ON files(source_root);
CREATE INDEX idx_files_deleted     ON files(deleted_at);
CREATE INDEX idx_files_version     ON files(version_group);
CREATE INDEX idx_chunks_file       ON chunks(file_id);
CREATE INDEX idx_chunks_backend    ON chunks(backend_name);
CREATE INDEX idx_chunks_pt_sha     ON chunks(plaintext_sha256) WHERE plaintext_sha256 IS NOT NULL;
CREATE INDEX idx_oplog_status      ON op_log(status);
CREATE INDEX idx_oplog_type        ON op_log(op_type);
CREATE INDEX idx_oplog_started     ON op_log(started_at);

-- Full-text search virtual table
CREATE VIRTUAL TABLE files_fts USING fts5(
    source_path,
    relative_path,
    content_text,
    content='files',
    content_rowid='id'
);
```

### SQLite Configuration

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA journal_size_limit = 67108864;    -- 64MB max WAL
PRAGMA wal_autocheckpoint = 1000;
PRAGMA cache_size = -64000;              -- 64MB page cache
```

---

## 4. Storage Backends

### 4.1 Abstract Interface (`backends/base.py`)

```python
class StorageBackend:
    name(self) -> str
    type(self) -> str                     # 'local', 'ftp', 'smb', 'ssh', 'rclone'

    upload(self, local_path, remote_rel_path) -> str      # returns blob_id
    download(self, remote_rel_path, local_path) -> None
    delete(self, remote_rel_path) -> None
    exists(self, remote_rel_path) -> bool
    list_files(self, prefix="") -> list[dict]             # {path, size, mtime}

    free_space(self) -> int | None        # None if unsupported
    total_space(self) -> int | None       # None if unsupported
    quota(self) -> int | None             # from config, None if no quota

    hash(self, remote_rel_path) -> str    # SHA256 of remote file
    health(self) -> bool                  # ping/connectivity test

    fingerprint(self, remote_rel_path) -> dict  # {size, mtime, head_tail_sha256}

    # DB consensus helpers
    db_upload(self, local_db_path) -> str
    db_download(self, remote_db_path, local_path) -> None
    db_list_snapshots(self) -> list[str]
```

### 4.2 Backend Implementations

| Backend | Library | free_space | hash | Notes |
|---|---|---|---|---|
| `local` | `shutil`, `os` | `shutil.disk_usage()` | Direct file read | Simplest |
| `ftp_sftp` | `ftplib`, `paramiko` | FTP: `STAT` / SFTP: `statvfs` | Download + hash or server checksum | Retry on disconnect |
| `smb` | `pysmb` | SMB `statvfs` equivalent | Read remote + hash | Windows-native |
| `ssh` | `paramiko` | Remote `df` command | Remote `sha256sum` | Key-based auth |
| `rclone` | subprocess CLI | `rclone about` | `rclone hashsum SHA256` | 50+ providers |

---

## 5. Folder Layout on Disk (HDD/SSD Optimized)

Files are stored in a **two-zone layout** for maximum HDD performance and cross-backend portability:

```
{backend_root}/
├── __backupme_db__/
│   ├── backupme.db
│   └── snapshots/
│
├── objects/                              # Content-addressable dedup blob store
│   ├── a1/b2/
│   │   ├── a1b2c3d4e5f6...7890.zst      # compressed blob
│   │   └── a1b2c3d4e5f6...7891          # uncompressed blob (no ext)
│   └── ...
│
└── tree/                                 # User-browsable original folder tree
    └── {source_root_id}/
        └── home/user/Documents/
            ├── report.pdf.backupme       # tiny pointer file (~60 bytes)
            └── photo.jpg.backupme        # points to objects/{hash}
```

- `objects/` is hash-sharded (`XX/YY/{full_hash}`) — ≤256 entries per dir, HDD-friendly
- `tree/` mirrors original source paths, each file is a tiny JSON pointer to the blob
- Pointer contents: `{"sha256":"...","size":...,"compression":"zstd"}`
- Works on FAT32, NTFS, ext4, FTP, SMB, S3 — no symlinks or hardlinks needed
- Compression indicated by file extension (`.zst`, `.gzip`, `.lz4`, no ext = uncompressed)

---

## 6. Compression Strategy

### 6.1 Compression Pipeline

```
plaintext_chunk → detect_compressibility() → compress (if beneficial) → encrypt → upload
    ↓                                            ↓
  plaintext_sha256                          compressed_sha256 stored in dedup_hashes
  (dedup key, stable forever)
```

**Order matters:** Compress THEN encrypt (encrypted data is incompressible).

### 6.2 File-Type Detection

Uses extension + magic bytes to skip already-compressed files:

| Category | Extensions | Action |
|---|---|---|
| Media | `.jpg`, `.png`, `.mp4`, `.mp3`, `.mov`, `.webp` | Skip |
| Archives | `.zip`, `.7z`, `.rar`, `.gz`, `.bz2`, `.xz`, `.zst` | Skip |
| Text | `.txt`, `.md`, `.log`, `.csv`, `.json`, `.xml`, `.yaml` | zstd -19 |
| Code | `.py`, `.js`, `.ts`, `.html`, `.css`, `.c`, `.h`, `.rs`, `.go` | zstd -10 |
| Binary (compressible) | Unknown, low-entropy | zstd -3 |
| Binary (incompressible) | Unknown, high-entropy | Skip |

If magic bytes indicate already-compressed content, skip compression even if extension is unknown.

### 6.3 Adaptive Level Selection

```python
def choose_compression_level(file_size):
    if file_size < 64KB:     return ('zstd', 19)   # tiny → max ratio
    if file_size < 1MB:      return ('zstd', 10)   # small → high
    if file_size < 64MB:     return ('zstd', 3)    # medium → balanced
    else:                    return ('zstd', 1)    # large → fast
```

### 6.4 Recompression Migration

```bash
backupme recompress                         # upgrade all uncompressed blobs to zstd -3
backupme recompress --algorithm zstd --level 19
backupme recompress --backend usb_drive      # single backend only
```

Process: download old blob → decompress → recompress → upload new → update `dedup_hashes` → delete old.

---

## 7. Encryption

### 7.1 Key Management

```
Master password → argon2id (slow KDF) → AES-256 key
Key file stored at: ~/.backupme/key.enc
Key file contents: {salt, nonce, encrypted_aes_key}
```

- Key file itself is encrypted by the derived key
- Without the master password, the key file is useless
- CLI: `backupme encrypt init | change-password | status`

### 7.2 Per-Chunk Encryption

```
Compressed chunk → AES-256-GCM encrypt → {nonce, ciphertext, tag} → upload
Download → decrypt → decompress → plaintext
```

Nonce is deterministic (SHA256 of plaintext position) — ensures re-encrypting same data produces same ciphertext for dedup. **But** this leaks equality; for stronger privacy, use random nonces and store nonce alongside blob.

Encryption is **optional** and disabled by default.

---

## 8. Retention & Versioning

Files tracked by `version_group` + `version_seq`:

```yaml
retention:
  enabled: true
  versions: 5           # keep last 5 versions of each file
  daily: 7              # keep 1/day for 7 days
  weekly: 4             # keep 1/week for 4 weeks
  monthly: 12           # keep 1/month for 12 months
```

On re-backup of a changed file: old version's `deleted_at` is set → new version gets same `version_group` with `version_seq+1`. Pruning schedule removes expired versions (blob kept if another file references it via dedup).

---

## 9. Selection Logic (`core/selector.py`)

### Auto-Select (Default)

```
1. Query all backends: health() + free_space()
2. Filter out unhealthy/unreachable backends
3. Sort by: (free_space * weight + priority) DESC
4. Return the best backend
5. For mirror mode: return best 2 DIFFERENT backends
```

### Manual Select (`--to` flag)

```
1. User provides backend name(s): --to usb_drive or --to usb_drive,gdrive
2. Look up exact match in configured backends
3. If NO match:
   a. Run difflib.get_close_matches(name, all_names, n=3, cutoff=0.4)
   b. Show suggestions interactively
   c. If none close → list all available backends
4. If match found → verify health, fail with clear error if offline
```

### Config Pinned

```yaml
backups:
  - source: /home/user/photos
    backend: gdrive          # always use this backend
```

---

## 10. Upload Flow

### 10.1 Normal Upload (Single Copy)

```
1. INPUT: source file path
2. SHA256(file) → check dedup_hashes table
   ├── EXISTS → add files entry, increment ref_count → DONE
   └── NEW →
3. IF file > chunking.min_size (default 1MB):
   a. Split into chunking.chunk_size (default 64MB) blocks
   b. FOR each chunk:
      - Detect compressibility (extension + magic bytes)
      - IF compressible: compress chunk (zstd, adaptive level)
                       → compressed_sha256 = SHA256(compressed)
      - ELSE: use raw chunk data, compressed_sha256 = NULL
      - IF encryption enabled: encrypt compressed/raw data
      - Select best backend
      - Upload to backend → get blob_id (= SHA256 of stored blob)
      - Verify: SHA256(remote blob) == expected hash
        → FAIL: retry ×3 → fallback to next backend → FAIL: log critical
      - Record chunk in chunks table
   c. Add dedup_hashes entry (plaintext + compressed hashes + algo)
   d. Update backend_usage
   ELSE:
   a. Single blob mode (seq=0)
   b. Same compress → encrypt → upload → verify flow
4. Post-write: replicate DB to all backends
```

### 10.2 Mirror Upload (2 Copies)

Same as normal, but EACH chunk goes to 2 different backends.

```
1. SHA256(file) → dedup check (same as normal)
2. If new content:
   FOR each chunk:
     backend_a = selector.best()
     backend_b = selector.best(exclude=[backend_a])
     
     compressed_data = compress(chunk) if compressible else chunk
     blob_hash = SHA256(compressed_data)  # same for both backends
     
     blob_id_a = upload_to(backend_a, compressed_data)
     verify(backend_a, blob_id_a)
     
     blob_id_b = upload_to(backend_b, compressed_data)
     verify(backend_b, blob_id_b)
     
     record_chunk(file_id, seq, backend_a, blob_id_a)
     record_chunk(file_id, seq, backend_b, blob_id_b)
```

### 10.3 Upload Verification

```python
def verify_upload(backend, remote_path, local_chunk):
    for attempt in range(1, retries + 1):
        remote_hash = backend.hash(remote_path)
        local_hash = sha256_of(local_chunk)
        if remote_hash == local_hash:
            return True
        backend.upload(local_chunk, remote_path)
    return False
```

On failure: `selector.next_best()` → re-upload + verify on new backend.

---
## 10.1 Convert Flow (Import Existing Backend Files)

Converts existing files on a backend into backupme's dedup layout without re-uploading from the source machine.

```
backupme convert BACKEND_NAME [--prefix /path] [--delete] [--target BACKEND] [--resume]
```

### Workflow

```
1. INPUT: backend name (source), optional prefix filter
2. Walk the backend recursively: for each file:
   a. Download to temp (remote backends) or use direct path (local)
   b. Compute SHA256
   c. Check dedup_hashes — if already exists, skip blob upload, just index
   d. Check if file already indexed (rel_path + backend_name) — skip if yes (resume)
   e. Apply compression if beneficial (same config as backup)
   f. Chunk if larger than chunking.min_size
   g. Store blob(s) to objects/xx/yy/sha256 on same (or --target) backend
   h. Index in files + dedup_hashes tables
   i. If --delete: remove original file after successful verify
   j. Save checkpoint with last converted rel_path
3. Resume: checkpoint file + DB query for already-indexed paths
```

### Checkpoint / Resume

```
checkpoint file: converter_checkpoint.json
{
  "backend": "remote_ftp",
  "last_path": "/old_data/photos/2024/vacation.jpg",
  "converted_count": 1847,
  "total_count": 12400,
  "errors": ["/old_data/corrupt/file.bin"]
}
On restart:
  - If --resume flag: skip files with rel_path already in files table for this backend
  - Checkpoint auto-saved every 50 files
  - Errors logged but not fatal — continue to next file
```

### Edge Cases

| Case | Behavior |
|---|---|
| File already in dedup_hashes | Index-only, no blob upload |
| Compression disabled on file type | Store raw |
| Download fails mid-way | Log error, continue next file, retry at end |
| Delete original fails | Log warning, continue |
| --target backend specified | Store on target, index source for origin tracking |
| Interrupted mid-batch | Resume skips completed files via DB query |
| File changes during conversion | SHA256 mismatch → warn, skip |

### Backend `walk()` Method Addition

```python
def walk(self, prefix: str = "") -> list[dict]:
    """Returns [{rel_path, size, mtime}, ...] recursively"""
    # Needed by converter, not just list() which returns paths only
```

---

## 11. Download Flow

### 11.1 Normal Download

```
1. INPUT: file_id or source_path
2. Look up files table → get sha256 + size
3. Get all chunks sorted by seq from chunks table
4. FOR each chunk:
   a. Get backend_name + blob_id from chunk record
   b. Get compression info from dedup_hashes (compressed_sha256, algo)
   c. Download from backend
   d. IF encryption enabled: decrypt blob
   e. IF compression: decompress (using stored algo + level)
   f. If backend fails → try mirror copy (if redundancy=2)
   g. Verify SHA256 of plaintext matches files.sha256
5. Reassemble chunks in order → write to destination
```

### 11.2 Mirror Failover

```
download(file_id):
    chunks = get_chunks(file_id)
    for chunk in chunks:
        # Try primary backend first
        if chunk.backend.health():
            data = download_and_decompress(chunk)
        else:
            # Find mirror copy (same seq, different backend)
            mirror_chunk = get_mirror_chunk(file_id, chunk.seq, exclude=chunk.backend)
            data = download_and_decompress(mirror_chunk)
            log.info(f"Failover: used {mirror_chunk.backend} for chunk {chunk.seq}")
```

### 11.3 Verify-Restore

Randomly selects files from the index, fully downloads and verifies them:

```bash
backupme verify [--count 5] [--backend usb_drive]
backupme verify --all                     # every single file
backupme verify --recent 100              # last 100 files
```

Results logged to `integrity_checks` table. Scheduler runs weekly in daemon mode.

---

## 12. Crash Protection / Power Failure Recovery

### 12.1 Checkpoint File

```json
// backupme.state.json — written every 30 operations
{
  "operation": "backup",
  "source": "C:/Users/Maruf/Docs",
  "current_file": "report.pdf",
  "completed_files": 47,
  "total_files": 103,
  "started_at": "2026-05-28T22:00:00Z"
}
```

### 12.2 Startup Recovery

```
1. Check if backupme.state.json exists and its PID is stale
2. If stale:
   a. Read op_log for uncommitted entries (started without committed)
   b. For each: verify if blob actually exists on backend
     - Exists → mark committed (DB write failed but upload succeeded)
     - Not exists → mark rolled_back
   c. Prompt: "Resume interrupted backup? [Y/n]"
   d. Clear state file
```

---

## 13. Full-Text Search

SQLite FTS5 virtual table indexes file paths + extracted text content:

```sql
CREATE VIRTUAL TABLE files_fts USING fts5(
    source_path, relative_path, content_text,
    content='files', content_rowid='id'
);
```

- Text extracted from `.txt`, `.md`, `.log`, `.csv`, `.json`, `.xml`, `.py` on upload
- Binary files (PDF, DOCX): optional extraction via external tools
- Indexed at upload time, no background maintenance needed

CLI:
```bash
backupme search "budget 2026"                    # filename search
backupme search --content "meeting notes"        # inside files
backupme search --source photos "beach"          # scoped
```

---

## 14. Backup Integrity Report

Detailed report of all system health, accessible via:

```
Dashboard: /report
CLI:       backupme report [--format html|json|text]
API:       GET /api/report
```

### Report Contents

```
Backup Integrity Report — 2026-05-28 22:00
═════════════════════════════════════════════

OVERVIEW
  Total files:        12,847
  Total chunks:       18,432
  Total stored:       342 GB
  After dedup:        214 GB (37% saved)
  After compression:  158 GB (54% total savings)
  Mirror coverage:    1,204/1,204 files (100%)

INTEGRITY
  Last verification:  2026-05-28 20:00 (PASSED ✓)
  Verified chunks:    18,432 / 18,432 (100%)
  Failed:             0

BACKENDS
  usb_drive:          ONLINE  ✓   87/500 GB used
  gdrive:             ONLINE  ✓   14/15 GB used
  nas:                ONLINE  ✓   42/200 GB used

RECENT RECOVERY EVENTS: None

RECOMMENDATIONS
  ✓ All backends healthy
  ✓ Mirror coverage complete
  ✓ No corruption detected
```

---

## 15. Snapshot & Recovery System

### 8.1 Automatic Snapshots

```
Interval: every 3600 seconds (configurable)
Process:
1. PRAGMA wal_checkpoint(TRUNCATE)      # flush WAL
2. VACUUM INTO 'snapshots/backupme-v{ts}.db'
3. Upload snapshot to every backend's __backupme_db__/snapshots/
4. Remove local snapshots beyond retention (168 = 7 days)
```

### 8.2 Auto-Recovery

```python
def recover_database():
    """Called when PRAGMA integrity_check fails."""
    log.critical("Database corruption detected. Entering recovery...")
    
    # 1. Preserve corrupted DB
    shutil.copy("backupme.db", f"backupme.db.corrupt.{timestamp()}")
    
    # 2. Run full consensus check across all replicas
    consensus = run_consensus_check()  # downloads all replicas
    
    # 3. If majority found → restore from it
    if consensus.majority_version:
        restore_from(consensus.majority_version.path)
        log_recovery("db_restore", "completed", 
                     f"Restored from majority replica ({consensus.majority_count}/{consensus.total})")
        return True
    
    # 4. If split brain → use latest snapshot + replay op_log
    if consensus.split_brain:
        snapshot = find_latest_snapshot()
        restore_from(snapshot.path)
        replay_operation_log(since=snapshot.timestamp)
        log_recovery("split_brain", "resolved", 
                     f"Used snapshot {snapshot.name} + op_log replay")
        return True
    
    # 5. If all corrupted → full rebuild from backend scan
    rebuild_database_from_backends()
    log_recovery("rebuild", "completed", "Full rebuild from backend scan")
    return True
```

### 8.3 Full Database Rebuild

If ALL copies are corrupted:

```
rebuild_database_from_backends():
1. Create fresh DB with schema
2. FOR each backend:
   a. backend.list_files(prefix="") → all stored paths
   b. FOR each file path:
      - Skip paths starting with __backupme_db__ (DB files)
      - Download file
      - Compute SHA256
      - Look up in dedup_store
      - Reconstruct files + chunks entries
3. Rebuild dedup hash counts
4. Rebuild backend_usage totals
5. Log rebuild event
```

---

## 17. CLI Commands

```bash
# Backup
backupme backup [--source path] [--to backend] [--mirror]

# Convert (import existing backend files)
backupme convert BACKEND_NAME [--prefix path] [--delete] [--target BACKEND] [--resume]

# Mirror
backupme mirror [--source path] [--to A,B]

# Download / Restore
backupme download <file_id> [--from backend] [--dest path]
backupme restore [--source root] [--dest path]

# List / Status / Search
backupme list [--source root] [--backend name] [--hash value]
backupme search <query> [--content] [--source root]
backupme status

# Verification
backupme verify [--count N] [--backend name] [--all] [--recent N]

# Encryption
backupme encrypt init
backupme encrypt change-password
backupme encrypt status

# Compression
backupme recompress [--algorithm zstd] [--level 3] [--backend name]

# Report
backupme report [--format html|json|text]

# Database Management
backupme db check [--full]
backupme db snapshot
backupme db backup
backupme db restore --snapshot file.db
backupme db oplog
backupme db info
backupme db rebuild

# Integrity
backupme check-consistency
backupme scrub [--backend name]

# Daemon
backupme daemon start|stop|status

# Web Dashboard
backupme web [--port 8080] [--host 0.0.0.0]

# Config
backupme config check
```

---

## 10. Web Dashboard

### 18.1 Pages

| Route | Page | Content |
|---|---|---|
| `/` | Dashboard | Cards: total files, total size, dedup savings, compression ratio, backend count. Bar chart: per-backend usage. DB health indicator |
| `/files` | Files | Searchable table with filename, path, size, hash, backend, date. Download button per row. Mirror status column (2/2 ✅) |
| `/backends` | Backends | Table: name, type, free/total space, quota, online/offline, last health check |
| `/logs` | Activity | Filterable log of uploads, verifications, errors, recovery events |
| `/database` | DB Health | Integrity status, snapshot list with timestamps, recovery history table. Manual buttons: Check, Snapshot, Backup |
| `/report` | Report | Full integrity report with charts, recommendations, export button |
| `/settings` | Settings | View config.yaml, toggle daemon, trigger manual backup |

### 10.2 API Endpoints

```
GET    /api/status              → { backends, total_files, total_size, dedup_count, compression_savings, db_status }
GET    /api/backends            → list of backends with health + usage
GET    /api/files?q=&backend=   → paginated file list (FTS-backed)
GET    /api/files/{id}          → file metadata + all chunk locations
GET    /api/sources             → backed-up source roots
GET    /api/logs?level=&limit=  → activity log
GET    /api/db/info             → DB size, snapshot count, integrity status
GET    /api/db/snapshots        → list of snapshots
GET    /api/db/recovery         → recovery event history
GET    /api/report              → full integrity report
GET    /api/search?q=&scope=    → FTS search results
POST   /api/backup              → trigger backup
POST   /api/download/{file_id}  → stream file download
POST   /api/db/check            → run integrity check
POST   /api/db/snapshot         → create snapshot
POST   /api/db/backup           → push DB to all backends
POST   /api/report/generate     → generate fresh report
POST   /api/encrypt/init        → initialize encryption
POST   /api/daemon/start        → start watcher
POST   /api/daemon/stop         → stop watcher
```

### 10.3 Tech Stack

| Layer | Technology |
|---|---|
| Framework | FastAPI |
| Templates | Jinja2 (server-rendered) |
| Frontend | Vanilla HTML/CSS + HTMX |
| Charts | Chart.js (CDN) |
| Server | uvicorn |

CLI and Dashboard call the **same core modules** (`core/uploader.py`, `core/downloader.py`, `core/database.py`, etc.) — no code duplication.

---

## 19. Configuration (`config.yaml`)

```yaml
backups:
  - source: C:\Users\Maruf\Documents
    exclude: ["*.tmp", "node_modules", "__pycache__"]
    mirror: false
    backend: null            # null = auto-select

  - source: C:\Users\Maruf\Photos
    mirror: true

backends:
  local:
    - name: usb_drive
      path: E:\backup\
      priority: 100
      weight: 10

  ftp:
    - name: remote_ftp
      host: ftp.example.com
      port: 21
      username: user
      password: "{{ FTP_PASS }}"       # env var
      quota: 500GB
      priority: 10

  ssh:
    - name: linux_server
      host: 192.168.1.100
      username: maruf
      key_file: ~/.ssh/id_rsa
      path: /mnt/backup/

  smb:
    - name: nas
      host: 192.168.1.50
      share: backups
      username: maruf
      password: "{{ NAS_PASS }}"

  rclone:
    - name: gdrive
      remote: "gdrive:/backupme"
      bwlimit: "4M"
      quota: 15GB

chunking:
  enabled: true
  min_size: 1MB           # files smaller than this → single blob
  chunk_size: 64MB        # each chunk
  parallel_uploads: 4

compression:
  enabled: true           # V2: enabled by default
  default_algorithm: zstd
  default_level: 3
  min_size: 1024
  intelligent_skip: true
  overrides:
    - ext: .log
      algorithm: zstd
      level: 19
    - ext: .jpg
      skip: true

encryption:
  enabled: false
  method: aes-256-gcm
  key_file: ~/.backupme/key.enc

verification:
  enabled: true
  retries: 3
  fallback_on_fail: true
  verify_restore:
    count: 5               # random files to verify weekly
    schedule: weekly

retention:
  enabled: true
  versions: 5
  daily: 7
  weekly: 4
  monthly: 12

database:
  snapshots:
    interval: 3600
    retention: 168
  auto_backup:
    enabled: true
    backends: all

notifications:
  on_failure: true
  desktop: true
```

---

## 20. Requirements

```
click>=8.1
pyyaml>=6.0
watchdog>=4.0
paramiko>=3.4
pysmb>=1.2
fastapi>=0.110
uvicorn[standard]>=0.29
jinja2>=3.1
python-multipart>=0.0
httpx>=0.27
cryptography>=41.0
```

---

## 21. Implementation Order

| Phase | Focus | Files | Est. |
|---|---|---|---|---|
| 1 | Scaffold + Config | 12 | 1h |
| 2 | Database (schema, WAL, consensus, snapshots, op_log, state) | 6 | 4h |
| 3 | Backends (base, local, ftp, smb, ssh, rclone) | 6 | 5h |
| 4 | Core Engine (hasher, compress, crypto, dedup, index, selector, upload, download, verify) | 10 | 8h |
| 5 | Converter + walk() backend method | 2 | 2h |
| 6 | Mirror + Scheduler | 2 | 2h |
| 7 | CLI (all commands incl. search, report, encrypt, recompress, convert) | 10 | 3h |
| 8 | Dashboard (all pages + report + FTS) | 11 | 5h |
| 9 | Polish (edge cases, logging, integration testing) | — | 2h |
| | **Total** | **~60 files** | **~32h** |

---

## 22. Edge Cases & Protections

| Edge Case | Protection |
|---|---|
| DB corruption | Consensus check → restore from majority → replay op_log |
| Upload fails mid-chunk | Retry ×3 → fallback backend → all-or-nothing per chunk |
| Backend goes offline | Skip in selector, retry on next cycle, log warning |
| Duplicate file, same hash | Dedup → add index entry only, no blob upload |
| Duplicate file, different hash | Treat as separate file (different content) |
| Source file changed (re-backup) | New version linked via version_group, old retained per retention |
| Power failure mid-write | State checkpoint → op_log scan → resume/rollback prompt |
| Concurrent backup runs | WAL mode + busy_timeout, second process waits |
| Very large file (100GB+) | 64MB chunks, parallel upload, resumable per-chunk |
| Backend fills up mid-upload | Remaining chunks route to next-best backend |
| Split brain DB (2v2) | Use latest snapshot + op_log replay |
| All DB copies corrupted | Full rebuild from backend scan |
| Encryption key lost | No recovery possible — log critical, warn user |
| No backends configured | Error on startup, refuse to run |
| Network timeout | Per-chunk timeout + retry + fallback |
| Disk full during snapshot | `min_free_space` guard, skip snapshot |
| Source root renamed | Indexed by `source_root`, new path = new files entry |
| Encryption key rotation | Recompress command re-encrypts all blobs with new key |

---

## 23. Key Design Decisions Summary

| Decision | Choice | Why |
|---|---|---|
| Language | Python | Best library support for all backends |
| DB Engine | SQLite + WAL + consensus replicas | Embedded, ACID, distributed integrity |
| DB Consensus | Pre-write fingerprint check, full check on mismatch | Balances safety vs performance |
| Dedup Scope | Global across all backends | Maximum storage efficiency |
| Dedup Key | SHA256 of plaintext | Cryptographic, deterministic, stable across compression changes |
| Compression | zstd per-chunk, adaptive level, intelligent skip | Best speed/ratio, file-type aware |
| Compression x Dedup | Store both plaintext_sha256 + compressed_sha256 | Dedup stable, can recompress later |
| Encrypt vs Compress | Compress THEN encrypt | Encrypted data is incompressible |
| Encryption | AES-256-GCM, argon2id key derivation | Industry standard, optional |
| Mirror Semantics | Exactly 2 copies, different backends | RAID-1 style redundancy |
| Chunking | 64MB, parallel uploads, independent placement | Large file support, resume |
| Upload Verification | SHA256 post-upload, 3 retries, fallback | Content integrity guarantee |
| Verify-Restore | Periodic random-file full cycle test | End-to-end trust |
| File Search | SQLite FTS5 on filenames + extracted text | Zero extra dependencies, fast |
| Retention | Version groups + prune schedule | Prevents accidental data loss |
| Quota Handling | Config quota + self-tracked usage | Works for any backend type |
| Manual Selection | Fuzzy suggest (`difflib`) on miss | User-friendly |
| CLI + Dashboard | Shared core modules | No code duplication |
| Folder Layout | Hash-sharded `objects/` + `tree/` pointers | HDD-optimized, portable across backends |
| Backup Schedule | On-demand + daemon watcher | Both manual and automatic |
| Crash Protection | State file + op_log replay on restart | Power failure safe |
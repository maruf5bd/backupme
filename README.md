# backupme — Distributed Backup System

A Python backup system that stores files across **multiple storage backends** (local, FTP/SFTP, SMB, SSH, rclone) with **content-addressed deduplication**, **N-copy redundancy**, **chunked large-file support**, **upload verification**, and a **dual CLI + web dashboard** interface.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Installation](#installation)
- [Configuration](#configuration)
- [CLI Commands](#cli-commands)
- [Architecture](#architecture)
- [Core API Reference](#core-api-reference)
- [Backend Reference](#backend-reference)
- [Web Dashboard](#web-dashboard)
- [PLAN_FIXED Compliance](#plan_fixed-compliance)

---

## Quick Start

```bash
# Install
pip install -e .

# Initialize encryption key
backupme encrypt init

# Backup a directory
backupme backup ~/Documents -b local --recursive

# List backed-up files
backupme list

# Start web dashboard
backupme web --port 8080
```

---

## Installation

```bash
git clone <repo>
cd backupme
pip install -e .
```

### Requirements

- Python 3.10+
- See `requirements.txt` for dependencies
- Optional: `zstandard` for zstd compression (auto-detected)
- Optional: `smbprotocol` for SMB backends
- Optional: `paramiko` for SFTP/SSH backends

---

## Configuration

Create `config.yaml`:

```yaml
backends:
  local:
    - name: local_usb
      type: local
      path: D:/backups
      priority: 100
  ftp:
    - name: my_ftp
      type: ftp
      host: ftp.example.com
      username: user
      password: "{{FTP_PASS}}"   # env var substitution
      priority: 50

backups:
  - source: C:/Users/Maruf/Documents
    backend: local_usb
    mirror: true

encryption:
  enabled: true
  key_file: ~/.backupme/key.enc

compression:
  enabled: true
  default_algorithm: zstd
  default_level: 3

retention:
  enabled: true
  versions: 5
  daily: 7
  weekly: 4
  monthly: 12
```

Environment variables in config values are auto-resolved: `{{VAR_NAME}}` → `os.environ["VAR_NAME"]`.

---

## CLI Commands

### Backup

```bash
backupme backup [PATHS]... [--backend NAME] [--redundancy N] [--recursive/-r]
```

Uploads files to backends. Small files (1KB–64KB) are auto-batched into compressed tar bundles for faster transfer.

| Option | Description |
|---|---|
| `PATHS` | Files or directories to backup |
| `--backend/-b` | Target backend (auto-select if omitted) |
| `--redundancy/-n` | Number of copies (default: 1) |
| `--recursive/-r` | Recurse into directories |

### Upload Bundle (small-file optimization)

```bash
backupme upload-bundle DIR [--backend NAME] [--redundancy N]
```

Explicitly batch-upload all small files (1KB–64KB) in a directory as a single compressed tar. Gives 3-5× better compression and 1/N round-trips.

### Download

```bash
backupme download <file_id|path> [--from BACKEND] [--dest PATH]
backupme restore [--source ROOT] [--dest PATH]
```

### List / Search

```bash
backupme list [--source ROOT] [--backend NAME] [--hash VALUE]
backupme search "query" [--content] [--source ROOT]
backupme status
```

| Command | Description |
|---|---|
| `list` | List backed-up files with filters |
| `search` | Full-text search (FTS5-powered) |
| `search --content` | Search file contents |
| `status` | Show backup status summary |

### Hash Lookup

```bash
backupme hash-lookup <sha256>
```

Look up which files use a given plaintext SHA-256 hash.

### Convert (Re-upload backend files)

```bash
backupme convert BACKEND_NAME [--prefix PATH] [--target BACKEND] [--delete] [--resume]
```

Walks a backend, re-uploads files to the dedup layout with manifests. Supports resumption via checkpoint file.

| Option | Description |
|---|---|
| `--delete` | Delete originals after verifying ≥1 replicated copy + manifest |
| `--resume` | Resume from last checkpoint |
| `--target` | Store converted blobs on a different backend |

### Database Management

```bash
backupme db check [--full]           # Integrity check
backupme db snapshot                 # Create snapshot now
backupme db restore [--from BACKEND] # Restore from replica
backupme db rebuild                  # Rebuild DB from manifests
backupme db oplog [--pending]       # Show operation log
backupme db info                    # DB statistics
```

### Verification & Reports

```bash
backupme verify [--count N] [--backend NAME] [--all] [--recent N]
backupme report [--format html|json|text]
backupme check-consistency
backupme scrub [--backend NAME]
```

### Encryption

```bash
backupme encrypt init                 # Generate encryption key
backupme encrypt change-password     # Re-encrypt key with new password
backupme encrypt status             # Check key status
```

### Recompress

```bash
backupme recompress [--algo zstd] [--level 3] [--backend NAME]
```

Recompress all blobs to a new algorithm/level using the safe write-new→commit→delete-old pipeline (PLAN_FIXED B12).

### Daemon

```bash
backupme daemon start|stop|status
```

Starts the scheduler daemon that periodically runs backups, snapshots, and retention.

### Web Dashboard

```bash
backupme web [--port 8080] [--host 0.0.0.0]
```

---

## Architecture

```
┌──────────────┐
│  Primary DB  │  ← single writer (./backupme.db)
└──────┬───────┘
       │ WAL frames + periodic snapshots
┌──────┴────────┬────────────┬────────────┐
▼               ▼            ▼            ▼
Backend A    Backend B   Backend C   ...   (disaster-recovery replicas)
```

### Key Design Decisions

| Feature | Implementation |
|---|---|
| **Single-writer DB** | One primary SQLite, replicas are DR-only (not voters) |
| **Generation counter** | Monotonic counter in `db_meta` table — no split-brain |
| **WAL replication** | Async WAL frame queue → periodic snapshots (not full-DB upload) |
| **Deduplication** | `blobs` table — `blob_id = SHA256(plaintext)`, ref-counted |
| **Storage** | `blob_storage` — per `(blob_id, backend, path)` variant |
| **Chunking** | 64MB blocks for large files; small files batched in tar bundles |
| **Compression** | zstd with adaptive levels: 19 (<64KB), 10 (<1MB), 3 (<64MB), 1 (≥64MB) |
| **Encryption** | AES-256-GCM, random 96-bit nonces, Argon2id key derivation |
| **Manifest sidecars** | Per-file JSON in `manifests/` — enables full rebuild from backends |
| **Op-log** | Idempotent, with preconditions and status state machine |
| **FTS5** | Full-text search on file paths and content |

---

## Core API Reference

### `config.py`

| Function | Signature | Description |
|---|---|---|
| `load_config` | `load_config(path="config.yaml") -> Config` | Load and validate YAML config with env-var resolution |

**Config dataclasses:** `Config`, `BackendDef`, `BackupDef`, `ChunkingConfig`, `CompressionConfig`, `EncryptionConfig`, `RetentionConfig`, `DatabaseConfig`, `VerificationConfig`

---

### `core/hasher.py`

| Function | Signature | Returns |
|---|---|---|
| `sha256` | `sha256(data: bytes) -> str` | Hex digest of SHA-256 |
| `sha256_file` | `sha256_file(path: str, chunk_size=64MB) -> str` | Streaming file hash |
| `sha256_chunked` | `sha256_chunked(data: bytes) -> str` | Chunked hash variant |

---

### `core/compress.py`

| Function | Signature | Returns |
|---|---|---|
| `should_skip` | `should_skip(data: bytes, ext="") -> bool` | True if already compressed (magic bytes + extension check) |
| `choose_compression_level` | `choose_compression_level(file_size: int) -> tuple[str, int]` | Adaptive: zstd-19 (<64KB), zstd-10 (<1MB), zstd-3 (<64MB), zstd-1 (≥64MB) |
| `compress` | `compress(data: bytes, algorithm="zstd", level=3) -> bytes` | Compress with zstd/gzip/bz2/lzma |
| `decompress` | `decompress(data: bytes, algorithm="zstd") -> bytes` | Decompress |
| `recompress_blobs` | `recompress_blobs(algo="zstd", level=3, backend_name=None, key=None) -> dict` | Safe recompression migration (write-new→commit→GC-old) |

**Supported algorithms:** `zstd` (preferred), `gzip`, `bz2`, `lzma`/`xz`

---

### `core/crypto.py`

| Function | Signature | Returns |
|---|---|---|
| `generate_key` | `generate_key() -> bytes` | Generate new 256-bit key, encrypted with Argon2id-derived KEK |
| `encrypt` | `encrypt(data: bytes, key: bytes) -> tuple[bytes, bytes, bytes]` | AES-256-GCM encrypt → (ciphertext, nonce, auth_tag) |
| `decrypt` | `decrypt(data: bytes, key: bytes) -> bytes` | AES-256-GCM decrypt |
| `load_key` | `load_key(path: str) -> bytes` | Load decryption key from encrypted file |
| `save_key` | `save_key(key: bytes, path: str)` | Save key encrypted with Argon2id |

**Nonces:** Random 96-bit per encryption (GCM safety). Convergent mode available via config.

---

### `core/schema.py`

Defines all database tables via `SQL_SCHEMA` string:

| Table | Purpose |
|---|---|
| `db_meta` | Generation counter, instance_id (single-writer model) |
| `files` | File metadata: source_root, relative_path, sha256, size, chunked, redundancy, versioning |
| `blobs` | Deduplication index: `blob_id = SHA256(plaintext)`, ref_count |
| `blob_storage` | Per-backend storage: path, stored_size, stored_sha256, compression, encryption nonce/tag |
| `file_chunks` | Chunk map: file_id → seq, blob_id, pt_offset, pt_size |
| `backends` | Backend registry with quota |
| `op_log` | Idempotent operation log with preconditions |
| `integrity_checks` | Verification audit trail |
| `recovery_events` | Recovery audit trail |

**Views:** `backend_usage` — sums stored_size per backend (no drift vs reality)

**FTS5:** `files_fts` virtual table on path + content_text

---

### `core/database.py`

| Function | Signature | Returns |
|---|---|---|
| `get_connection` | `get_connection(db_path=None) -> Connection` | Thread-local WAL connection |
| `initialize` | `initialize()` | Create DB, set instance_id, bump generation |
| `execute` | `execute(sql: str, params=()) -> Cursor` | Execute SQL |
| `commit` | `commit()` | Commit transaction |
| `get_generation` | `get_generation() -> int` | Read generation counter |
| `bump_generation` | `bump_generation()` | Increment generation counter |
| `get_instance_id` | `get_instance_id() -> str` | Read instance UUID |
| `check_integrity` | `check_integrity() -> list[str]` | Run `PRAGMA integrity_check` |
| `wal_checkpoint` | `wal_checkpoint()` | Flush WAL via `PRAGMA wal_checkpoint(TRUNCATE)` |

---

### `core/dedup.py`

| Function | Signature | Returns |
|---|---|---|
| `record_blob` | `record_blob(blob_id: str, pt_size: int) -> dict` | Upsert blob, increment ref_count |
| `unref_blob` | `unref_blob(blob_id: str) -> int` | Decrement ref_count, delete if ≤0 |
| `get_blob` | `get_blob(blob_id: str) -> dict \| None` | Lookup blob by plaintext SHA-256 |
| `list_orphans` | `list_orphans() -> list[dict]` | Blobs with ref_count ≤ 0 |
| `total_dedup_savings` | `total_dedup_savings() -> dict` | Raw vs stored bytes |

---

### `core/manifest.py`

| Class / Function | Signature | Description |
|---|---|---|
| `Manifest` | `@dataclass`: file_uuid, source_root, relative_path, sha256, size, version_group, chunks | Per-file manifest sidecar |
| `ManifestChunk` | `@dataclass`: seq, blob_id, pt_offset, pt_size, storage: list[ChunkStorageEntry] | Chunk entry in manifest |
| `ChunkStorageEntry` | `@dataclass`: backend, path, stored_size, stored_sha256, algo, level, encrypted | Backend storage entry |
| `write_manifest` | `write_manifest(backend, manifest: Manifest) -> str` | Write manifest JSON to backend |
| `read_manifest` | `read_manifest(backend, path: str) -> Manifest \| None` | Read manifest from backend |
| `rebuild_database_from_manifests` | `rebuild_database_from_manifests(backends: list) -> dict` | Full DB rebuild from manifest sidecars |

---

### `core/op_log.py`

| Function | Signature | Returns |
|---|---|---|
| `append` | `append(op_type, target, idempotency_key=None, precondition=None, status="pending") -> dict` | Append op with idempotency |
| `mark_committed` | `mark_committed(op_id: int)` | Transition to committed |
| `mark_failed` | `mark_failed(op_id: int, error: str)` | Transition to failed |
| `mark_rolled_back` | `mark_rolled_back(op_id: int)` | Transition to rolled_back |
| `get_pending` | `get_pending() -> list[dict]` | List pending ops (for crash recovery) |
| `replay_since` | `replay_since(op_log_id=0) -> list[dict]` | Replay ops from given ID |
| `verify_precondition` | `verify_precondition(precondition: str \| None) -> bool` | Check idempotency precondition |
| `get_status_stats` | `get_status_stats() -> dict` | Count by status |
| `prune` | `prune(before_id: int)` | Delete old log entries |

**Precondition syntax:** `"not_exists:table:column:value"` or `"gen_lt:N"`

---

### `core/state.py`

| Function | Signature | Returns |
|---|---|---|
| `save_checkpoint` | `save_checkpoint(op_type: str, data: dict)` | Atomic write (tmp+rename) |
| `load_state` | `load_state() -> dict \| None` | Read last checkpoint |
| `get_last_checkpoint` | `get_last_checkpoint() -> dict \| None` | Last checkpoint only |
| `clear_state` | `clear_state()` | Delete state file |

---

### `core/snapshot.py`

| Function | Signature | Returns |
|---|---|---|
| `create_snapshot` | `create_snapshot(retention_days=7) -> dict` | VACUUM INTO + zstd compress + upload to backends |
| `prune_snapshots` | `prune_snapshots(retention_days=7, min_keep=3)` | Delete old local snapshots |
| `auto_backup` | `auto_backup(config: dict)` | Run scheduled snapshot + prune |

---

### `core/recover.py`

| Function | Signature | Returns |
|---|---|---|
| `find_replicas` | `find_replicas(backends: list) -> list[dict]` | Locate DB replicas on backends |
| `recover_database` | `recover_database(backends: list) -> dict` | Full recovery: integrity check → rebuild from snapshots+WAL → pick highest generation |
| `rebuild_from_manifests` | `rebuild_from_manifests() -> dict` | Rebuild DB from manifest sidecars |
| `restore_from_majority` | `restore_from_majority(backends: list) -> dict` | Alias for `recover_database` |

---

### `core/verify.py`

| Function | Signature | Returns |
|---|---|---|
| `run_integrity_check` | `run_integrity_check(check_type="random", count=5, backend_name=None) -> dict` | Verify random/recent/all files |
| `run_verify_restore` | `run_verify_restore() -> dict` | Full restore-and-hash check |
| `get_report` | `get_report() -> dict` | Integrity report with recommendations |
| `list_reports` | `list_reports(limit=10) -> list[dict]` | Recent integrity checks |

---

### `core/selector.py`

| Function | Signature | Returns |
|---|---|---|
| `auto_select_backend` | `auto_select_backend(config, required_capacity=0) -> tuple[str, str] \| None` | Pick healthiest backend by priority+space |
| `fuzzy_find_backend` | `fuzzy_find_backend(query: str, config) -> list[tuple[str, str, float]]` | Fuzzy match backend name |
| `manual_select_backend` | `manual_select_backend(query: str, config) -> tuple[str, str] \| None` | Manual selection with fuzzy fallback |
| `list_available_backends` | `list_available_backends(config) -> list[dict]` | All backends with health status |

---

### `core/uploader.py`

| Function | Signature | Returns |
|---|---|---|
| `upload_file` | `upload_file(source_root, relative_path, local_path, backends, config, redundancy=1, version_seq=1, version_group=None) -> dict` | Upload single file with dedup+compress+encrypt+verify |
| `upload_bundle` | `upload_bundle(files: list[dict], backends, config, redundancy=1) -> list[dict]` | Batch small files into tar bundle for faster transfer |

**Upload pipeline:** hash → dedup check → compress (adaptive zstd) → encrypt (AES-256-GCM) → upload to N backends (with exponential backoff + circuit breaker) → verify → write manifest + pointer → record blobs/chunks → bump generation

**Small-file optimization:** Files 1KB–64KB auto-bundled into tar archives (up to 16MB per bundle), compressed together for 3-5× better ratio and 1/N round-trips.

---

### `core/downloader.py`

| Function | Signature | Returns |
|---|---|---|
| `download_file` | `download_file(file_id, dest_path, backend_name=None, version=None) -> dict` | Download with N-copy failover, decrypt, decompress, verify |
| `download_by_sha256` | `download_by_sha256(sha256, dest_path, backend_name=None) -> dict` | Download by plaintext hash |

**Download pipeline:** locate blob_storage entries → try backends with failover → download → decrypt (AES-256-GCM) → decompress → verify plaintext SHA-256 → reassemble chunks → write to dest

Bundle support: extracts individual files from tar bundles using `@offset:length` path syntax.

---

### `core/converter.py`

| Function | Signature | Returns |
|---|---|---|
| `convert_backend` | `convert_backend(backend_name, prefix="", target_backend_name=None, delete=False, resume=False)` | Walk backend, re-upload to dedup layout with manifests |

**Safety:** `--delete` requires ≥1 verified replica + manifest replication before deleting originals. Resume via `converter_checkpoint.json`.

---

### `core/retry.py`

| Class | Key Methods / Fields |
|---|---|
| `ExponentialBackoff` | `@dataclass`: max_attempts=4, base=1.0, max=30.0, jitter=True; `__iter__` yields (attempt, delay) |
| `CircuitBreaker` | `@dataclass`: max_failures=5, cooldown=300.0; `record_failure(name)`, `record_success(name)`, `is_tripped(name) -> bool` |
| `TransientError` | Exception — marker for retryable errors |

---

### `core/replicator.py`

| Class | Key Methods |
|---|---|
| `Replicator` | `start()`, `stop()`, `enqueue_wal_frames()`, `flush_wal_bundle()`, `create_and_upload_snapshot()`, `_prune_old_wal()` |

**Replication strategy:**
- Every commit → WAL frames queued (μs)
- Every 60s / 1000 ops / 64MB → flush WAL bundle to backends
- Every hour → VACUUM INTO snapshot, upload, prune old WAL
- Async — backups never block on replica upload

---

### `core/index.py`

| Function | Signature | Returns |
|---|---|---|
| `index_file` | `index_file(source_root, relative_path, sha256, size, mtime, chunked, redundancy, version_seq, version_group, content_text=None) -> int` | Insert/update file metadata + sync FTS5 |
| `find_by_path` | `find_by_path(source_root, relative_path) -> list[dict]` | All versions of a file |
| `find_by_sha256` | `find_by_sha256(sha256) -> list[dict]` | Files matching hash |
| `get_latest_version` | `get_latest_version(source_root, relative_path) -> dict \| None` | Latest version |
| `search_files` | `search_files(query: str, source_root=None) -> list[dict]` | FTS5 search |
| `get_all_files` | `get_all_files() -> list[dict]` | All non-deleted files |
| `file_count` | `file_count() -> int` | Total file count |
| `delete_file` | `delete_file(file_id)` | Soft-delete + FTS5 sync |

---

### `scheduler.py`

| Class / Function | Signature | Description |
|---|---|---|
| `BackupScheduler` | `__init__(config)`, `start()`, `stop()` | Daemon thread for periodic tasks |
| `apply_retention` | `apply_retention(config: dict)` | Apply retention policy, GC old blobs |

**Periodic loop (every `scheduler.interval` seconds):** run backups → snapshot → retention

---

### `mirror.py`

| Function | Signature | Returns |
|---|---|---|
| `replicate_to_n` | `replicate_to_n(blob_id: str, target_backends: list, config: dict) -> int` | Replicate blob to N backends |
| `mirror_file` | `mirror_file(file_id: int, target_backends: list, source_backend: str, config: dict) -> dict` | Ensure N-copy redundancy for a file |
| `check_consistency` | `check_consistency() -> dict` | Verify replicas match across backends |

---

## Backend Reference

### Abstract Interface (`backends/base.py`)

All backends implement `StorageBackend`:

| Method | Signature | Description |
|---|---|---|
| `name` | `-> str` | Backend name |
| `type` | `-> str` | Backend type |
| `connect` | `-> bool` | Establish connection |
| `disconnect` | | Close connection |
| `upload` | `(local_path, remote_rel_path) -> str` | Upload file, return stored SHA-256 |
| `download` | `(remote_rel_path, local_path)` | Download file |
| `delete` | `(remote_rel_path)` | Delete file |
| `exists` | `(remote_rel_path) -> bool` | Check existence |
| `list_files` | `(prefix="") -> Iterator[dict]` | List files (streaming) |
| `walk` | `(prefix="") -> Iterator[dict]` | Recursive listing |
| `free_space` | `-> int \| None` | Free space in bytes |
| `total_space` | `-> int \| None` | Total capacity |
| `quota` | `-> int \| None` | Configured quota |
| `hash` | `(remote_rel_path) -> str \| None` | Server-side hash (None if unsupported) |
| `health` | `-> bool` | Check backend availability |
| `fingerprint` | `(remote_rel_path) -> dict` | `{size, mtime, etag?}` |
| `db_upload_chunk` | `(local_path, remote_path)` | Upload WAL/snapshot chunk |
| `db_download` | `(remote_path, local_path)` | Download DB file |
| `db_list` | `(kind: "snapshots"\|"wal") -> list[str]` | List DB replicas |

### Implementations

| Backend | Module | Library | Notes |
|---|---|---|---|
| `local` | `local.py` | `shutil`, `os` | Reference impl; `shutil.disk_usage()` for space |
| `ftp` | `ftp_sftp.py` | `ftplib` | Auto-reconnect, `STAT` for free space |
| `sftp` | `ftp_sftp.py` | `paramiko` | `statvfs` for space, `listdir_attr` for walk |
| `smb` | `smb.py` | `smbclient` | Windows SMB shares; space query via SMB info |
| `ssh` | `ssh.py` | `paramiko` + subprocess | Remote `df` for space, `sha256sum` for hash |
| `rclone` | `rclone.py` | subprocess CLI | 50+ cloud providers via `rclone about`, `rclone hashsum` |

---

## Web Dashboard

Start with `backupme web --port 8080`, then open `http://localhost:8080`.

### Pages

| Route | Page | Description |
|---|---|---|
| `/` | Dashboard | File count, dedup ratio, compression ratio, per-backend usage chart, DB health, recent files |
| `/files` | Files | Searchable (FTS-backed) table; per-row download; redundancy column |
| `/backends` | Backends | Health status, free/total/quota, last seen, circuit-breaker state |
| `/logs` | Activity | Filtered op_log + recovery_events |
| `/database` | DB Health | Integrity status, snapshot/WAL list, generation/instance_id, recovery history |
| `/report` | Report | Full integrity report with charts and recommendations |
| `/settings` | Settings | View config, toggle daemon, trigger backup |

### API Endpoints

```
GET  /api/status
GET  /api/backends
GET  /api/files?q=&backend=&page=
GET  /api/files/{id}
GET  /api/sources
GET  /api/logs?level=&limit=
GET  /api/db/info
GET  /api/db/snapshots
GET  /api/db/recovery
GET  /api/report
POST /api/backup
POST /api/verify
POST /api/db/replicate
```

---

## PLAN_FIXED Compliance

All 20 architectural corrections (B1–B20) from `PLAN_FIXED.md` are implemented:

| Fix | Description | Status |
|---|---|---|
| B1 | Consensus → Single-writer model | ✅ |
| B2 | Fingerprint → Generation counter | ✅ |
| B3 | Split-brain handling removed | ✅ |
| B4 | WAL frame replication (not full-DB upload) | ✅ |
| B5 | Random GCM nonces (not deterministic) | ✅ |
| B6 | `blob_id = plaintext SHA256` (stable across transforms) | ✅ |
| B7 | Schema: `blobs` + `blob_storage` + `file_chunks` (not `dedup_hashes`) | ✅ |
| B8 | Separate `pt_offset`/`pt_size` (plaintext) vs `stored_size` (on-disk) | ✅ |
| B9 | `files.chunked` boolean (not sentinel seq=0) | ✅ |
| B10 | Per-file manifest sidecars for rebuild | ✅ |
| B11 | Download failover on download/verify failure (not just `health()`) | ✅ |
| B12 | Recompression: write-new → commit → GC-old | ✅ |
| B13 | `--delete` requires N replicas + manifest check | ✅ |
| B14 | Exponential backoff + circuit breaker | ✅ |
| B15 | Idempotent op_log with preconditions | ✅ |
| B16 | Section numbering fixed | ✅ |
| B17 | `dedup_chunks` table removed | ✅ |
| B18 | `backend_blob_claims` removed (use view) | ✅ |
| B19 | Replica never authoritative (highest-gen restore) | ✅ |
| B20 | Versioned pointer file format | ✅ |

---

## Project Structure

```
backupme/
├── main.py                     # CLI entry point (click)
├── config.py                    # YAML loader + validation + env vars
├── scheduler.py                 # Daemon scheduler
├── mirror.py                    # N-copy redundancy
├── requirements.txt
├── config.yaml.example
│
├── core/
│   ├── schema.py               # Tables + migrations + FTS5
│   ├── database.py             # SQLite primary; generation counter
│   ├── hasher.py              # SHA-256 content hashing
│   ├── compress.py            # zstd/gzip/bz2/lzma + recompress
│   ├── crypto.py              # AES-256-GCM, Argon2id, random nonces
│   ├── dedup.py               # Plaintext-hash dedup index
│   ├── manifest.py            # Per-file sidecar manifests
│   ├── op_log.py             # Idempotent op log
│   ├── state.py               # Crash checkpoint (atomic writes)
│   ├── snapshot.py           # VACUUM INTO + WAL export
│   ├── replicator.py         # WAL replication to backends
│   ├── recover.py            # Single-writer recovery
│   ├── verify.py             # Random-restore verification
│   ├── selector.py           # Backend selection
│   ├── uploader.py           # Upload orchestrator + bundle batching
│   ├── downloader.py         # Download with N-copy failover
│   ├── converter.py          # Backend → dedup layout converter
│   ├── retry.py              # Exponential backoff + circuit breaker
│   └── index.py             # File metadata index + FTS5
│
├── backends/
│   ├── base.py               # Abstract StorageBackend
│   ├── local.py
│   ├── ftp_sftp.py
│   ├── smb.py
│   ├── ssh.py
│   └── rclone.py
│
├── cli/                      # Click command modules
│   ├── base.py
│   ├── backup_cmd.py
│   ├── download_cmd.py
│   ├── list_cmd.py
│   ├── search_cmd.py
│   ├── convert_cmd.py
│   ├── db_cmd.py
│   ├── web_cmd.py
│   ├── daemon_cmd.py
│   ├── encrypt_cmd.py
│   ├── report_cmd.py
│   └── __init__.py
│
└── web/                      # FastAPI dashboard
    ├── app.py
    ├── routes.py
    ├── templates/
    │   ├── base.html
    │   ├── dashboard.html
    │   ├── files.html
    │   ├── backends.html
    │   ├── logs.html
    │   ├── database.html
    │   ├── report.html
    │   ├── settings.html
    │   └── 404.html
    └── static/
```

---

## License

[Specify your license here]

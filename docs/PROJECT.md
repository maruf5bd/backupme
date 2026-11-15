# BackupMe — Project Documentation

BackupMe is a Python 3.13 deduplicating backup system for Windows: a `click` CLI, a
FastAPI web dashboard, pluggable storage backends, and a single SQLite hot index with
optional Parquet cold history. It backs up live directories and ingests foreign
archives (tar/zip/7z/rar) extract-first, deduplicates everything content-addressed,
verifies every stored byte by hash, and can rebuild its index from replicated
manifests if the database is ever lost.

Authoritative companions to this document: `docs/MANUAL.md` (full command reference and
walkthroughs), `docs/BACKLOG.md` (improvement register), `brain/` (living operational
state: status, wiring, defects, decisions, lessons), and `docs/archive/` (historical
plans and QA reports).

## What it does

- **Dedup, content-addressed storage.** Every blob is named by its SHA-256. Identical
  content is stored once, everywhere. Reference counts track each blob's users, with
  cascade for bundle members (`dedup.add_ref` / `unref_blob`).
- **Bundles and chunks.** Small files are batched into tar bundles (members live
  inside the parent as `path@offset:len` byte ranges); files above the chunking
  threshold become standalone chunked objects. Compression is zstd (configurable;
  gzip/bz2/lzma available), with intelligent skipping of incompressible formats
  detected by extension and magic bytes.
- **Encryption.** Optional AES-256-GCM with argon2id (fallback pbkdf2) key
  derivation. The envelope is self-describing — salt, nonce, and tag live inside the
  stored file, so a blob plus `backupme.key` is decryptable without the database.
- **Multiple backends with verification.** local, ftp, sftp (Hetzner-grade retry and
  non-truncating walks), smb, ssh, rclone, 9drive. Every upload is hash-verified
  (backend-native hash first, download-back as fallback). Database rows are written
  only after at least one backend succeeds (transactional upload accounting).
- **Archive ingest, extract-first.** tar/tar.gz/tgz/tar.bz2/tar.xz/zip members are
  extracted and uploaded through the normal pipeline (full cross-index dedup); 7z via
  py7zr and rar via the machine's WinRAR, each with caps on depth/members/size.
  Archive manifests (fingerprint-keyed, with per-member chunk recipes) are replicated
  to every backend as DB-loss insurance.
- **Database guard.** Daily daemon tick ships a fresh DB snapshot to every backend
  under `guard/snapshots/`, size-verified and partial-tolerant. `db
  recover-from-manifests` rebuilds an index from replicated manifest sidecars with
  decompress-hash verification.
- **DB lifecycle.** Cold history exports to monthly Parquet files (zstd-3) with a
  hard-purge watermark; DuckDB queries answer history questions; snapshot pruning,
  WAL replication, and migrations round out the lifecycle.
- **Scheduler daemon.** One periodic loop drives backups → retention → ingest →
  purge → cold export → gc → verify. Each tick has a non-blocking single-flight lock
  and its own exception guard; failure notifications dispatch through
  `core/notify.py`. Scheduled GC is report-only unless `scheduler.gc_apply: true`.
- **Web dashboard.** FastAPI + Jinja at `http://127.0.0.1:8081`: Dashboard, Files
  (search/preview/restore, content + hash modes), Ingest, Archives, Backends,
  Activity log, DB Health, Data Health (reachability scan, purge candidates,
  confirm-gated purge), Report, Settings. Dark/light themes, global Ctrl+K search,
  SSE progress for background ops, op_log audit trail for every mutating action.
  Destructive endpoints require an explicit `confirm` flag.

## How a backup works

1. **Scan** — the scheduler (or `backupme backup`) collects changed candidates from
   the source root (size + mtime comparison against the index), applying skip rules
   (`core/skip.py`).
2. **Hash** — each candidate is read exactly once; its SHA-256 drives dedup.
3. **Dedup** — a single donor with the same hash supplies existing chunks; new
   content is bundled (tar, ≤ `chunking.min_size`) or chunked, compressed, and
   encrypted if configured.
4. **Upload** — batches go to `upload_bundle`, which pushes to the configured
   backends, verifies hashes, and records `blob_storage` rows in one transaction per
   batch. Partial backend failures are reported per file, never silent.
5. **Index** — manifests and tree pointers are written; chunk recipes reference
   blobs; refcounts update; op_log entries record the operation.

Restore is the inverse: the index (or a replicated manifest sidecar) supplies the
chunk recipe; blobs are fetched (member ranges resolved through their parent tar),
decrypted, decompressed, and reassembled — byte-exact by construction, since the
stored hash gate is on the verify path.

## Architecture map

| Module | Purpose |
|---|---|
| `backupme/cli/` | click command groups; lazy core imports keep startup fast |
| `backupme/core/uploader.py` | `upload_bundle` — batches, dedup, upload, accounting |
| `backupme/core/downloader.py` | restore path; legacy-envelope support; range validation |
| `backupme/core/dedup.py` | blob registry, refcounts, cascade unref, reconcile |
| `backupme/core/database.py` | single-writer SQLite: WAL, thread-local connections |
| `backupme/core/gc.py` | remote orphan detection; dry-run default, gated apply |
| `backupme/core/data_health.py` | issue buckets, purge candidates, scan plans |
| `backupme/core/archives.py` | extract-first ingest, fingerprint manifests, stats |
| `backupme/core/db_lifecycle.py` | Parquet cold export, hard purge, DuckDB history |
| `backupme/core/scheduler.py` | daemon loop, batching, retention, notifications |
| `backupme/core/recover.py` | WAL replay and DB recovery tooling |
| `backupme/core/crypto.py` | AES-256-GCM envelope, KDF, capability introspection |
| `backupme/backends/` | one module per backend type; fingerprint-keyed instance cache |
| `backupme/web/` | FastAPI app: `routes/` package (10 domain modules), templates, static |
| `backupme/config.py` | dataclass-per-YAML-section loader; unknown keys warn, never crash |

## Trust and safety model

- **One database writer at a time.** WAL + thread-local connections; every writer
  commits explicitly. Tests never touch the real database (an autouse fixture swaps
  in a scratch DB), and CLI invocations isolate per `--db-path`.
- **Refcount invariant.** `ref_count == file_chunks rows + bundle member rows`, with
  bundle→parent cascade; `reconcile_refcounts` repairs drift linearly.
- **Deletion is gated, twice.** Remote GC is dry-run by default; `--apply` is only
  run after a three-step verification (matcher health, category sweep across all
  referencing tables, zero-reference proof) and explicit user approval. Dashboard
  purges soft-delete via one shared path, re-verify at purge time, and write op_log
  audit entries.
- **Secrets stay local.** `config.yaml` and `*.key` are gitignored; backend
  credentials have exactly one allowed home.
- **Nothing silent.** Walk failures surface (`walk_errors` counters), failed batches
  are counted and reported, unknown config keys warn on startup, and the activity
  page exposes stale/zombie operations honestly.

## Running it

```bash
# install
/c/Python313/python.exe -m pip install -r requirements.txt

# tests (scratch DB per test; never touches the real database)
/c/Python313/python.exe -m pytest tests/ -q

# dashboard (also: dashboard.bat or index.bat menu)
/c/Python313/python.exe main.py web --host 127.0.0.1 --port 8081

# daemon (scheduled backups, retention, guard snapshots, gc, verify)
/c/Python313/python.exe main.py daemon

# a few everyday commands (full reference: MANUAL.md section 10)
backupme backup <source>
backupme restore <file-id-or-query>
backupme verify --count 10
backupme gc                     # dry run
backupme archive stats
backupme db check --repair
```

Configuration lives in `config.yaml` (gitignored; see `config.example.yaml` for
every section: chunking, compression, encryption, verification, retention, database,
notifications, cpanel ingest, db lifecycle, scheduler, guard, backends, web).

## Repository layout

```
backupme/            package: cli, core, backends, web
tests/               pytest suite (isolated fixtures; see tests/conftest.py)
docs/                this document + MANUAL.md + BACKLOG.md; docs/archive/ holds
                     historical plans and QA reports
brain/               living knowledge base: status, wiring, defects, decisions, lessons
scripts/             user-facing operational tools
diagnostics/         gitignored home for one-off analysis scripts and their outputs
                     (root-sweep-2026-11-15/ holds the Nov 2025 root cleanup)
AGENTS.md            rules for AI sessions working in this repo
main.py / setup.py / pytest.ini / requirements.txt / config.example.yaml
dashboard.bat / index.bat   one-click launchers
backupme.db          the live SQLite index (gitignored runtime data)
backupme.key         encryption key (gitignored; keep a copy somewhere safe)
```

Conventions: one-off analysis artifacts go to `diagnostics/` (never committed);
significant findings go to `brain/` the moment they are known; historical planning
documents live in `docs/archive/`.

## Current status (2026-11-15)

- **Test suite**: 552 passing / 4 failing / 1 pre-existing skip. All 4 failures share
  one root cause: uncommitted work-in-progress in `backupme/backends/ftp_sftp.py`
  (its `FakeTransport` test double lacks the newly called `set_keepalive`), from
  another work stream — the last landed commit is green.
- **Backlog B01–B16 landed**; the defect register lives in `brain/defects.md`.
- **Verified repairs**: zero-storage-row blobs re-backed; corrupt-blob re-backup
  healed 10 of 17 affected files; consolidation unified blob copies (K drive now
  holds all 51,547 referenced blobs, 0 missing); legacy 2026-05 envelope objects
  restored via reverse-engineered support; GC reclaimed 21.96 GiB of zero-reference
  orphans (2026-11-10) plus earlier generations.
- **Archive feature**: 30 ingested Softaculous archives → 2.47 GB of archives reduce
  to 1.34 GB of unique logical content (76% member dedup, 45.9% saved vs storing the
  archives whole).

### Known issues and standing decisions (need user approval — do not proceed silently)

- `/` dashboard home loads in ~57 s (backend probes run inline on the request path)
  and `/database` runs a minutes-class integrity check per page load. A performance +
  UX wave is designed but parked pending approval.
- 7 SOURCE_GONE files reference corrupt blobs — unrecoverable unless sources
  reappear; purge candidates.
- 725 unfixable zero-storage-row files (716 source-gone, 9 source-changed); purge
  candidates.
- 4 junk "archives" await a purge decision (2FA.7z is the last copy in existence).
- Storage Box password rotation recommended (it appeared in plaintext in gitignored
  scratch scripts); the box is essentially empty off-site — filling it is a user
  decision.
- `restored_local_archives/` holds ~19 GB of restore-verification scratch output in
  the repo tree — safe to delete when the verification campaign is trusted done.
- The live DB carries ~14,977 stale `h local drive` pointer rows (every referenced
  blob exists on another backend; harmless, cleanup candidates for a future
  migration).

## Where to look next

- New to the system: `README.md`, then this document, then `docs/MANUAL.md` §10.
- Changing code: `AGENTS.md` (rules), `brain/wiring.md` (what connects to what),
  `docs/BACKLOG.md` (scoreboard).
- Trusting a backup: `backupme verify`, the dashboard Data Health page, and
  `brain/status.md` (repair history).

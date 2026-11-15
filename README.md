# BackupMe

A deduplicating backup tool for Windows: a click CLI and a FastAPI web
dashboard over a SQLite hot store, pluggable storage backends (local, FTP,
SFTP, SMB, SSH, rclone, 9drive), content-addressed deduplication with N-copy
redundancy, and monthly Zstd Parquet cold history queryable with DuckDB.

- Python 3.13
- License: MIT ([LICENSE](LICENSE))
- Changelog: [CHANGELOG.md](CHANGELOG.md)
- Full user guide: [docs/MANUAL.md](docs/MANUAL.md)
- Every configuration option, with comments: [config.example.yaml](config.example.yaml)

---

## Quick Start

```bash
# 1. Install (Python 3.13 required)
git clone <repo>
cd backupme
pip install -e .

# 2. Initialize a backup directory: scaffolds config.yaml from
#    config.example.yaml, creates backupme.db, prints next steps
backupme init

# 3. Configure where backups go (interactive; or edit config.yaml)
backupme backend add

# 4. Back something up
backupme backup <path> --recursive

# 5. Start the dashboard
backupme web
```

`backupme init` never overwrites an existing `config.yaml`: re-running it
prints `already exists — nothing changed` and exits 0 (`--json` reports
`"status": "already_initialized"`). A missing or empty `config.yaml` makes
every other command print a loud warning naming the path, so running with
defaults can never pass silently.

---

## Installation

```bash
pip install -e .
backupme --version
backupme --help
```

Requirements:

- Python 3.13 (enforced by `pyproject.toml`, `requires-python = ">=3.13"`)
- Dependencies are installed automatically from `pyproject.toml` /
  `requirements.txt`: `click`, `pyyaml`, `paramiko`, `smbclient`,
  `cryptography`, `argon2-cffi`, `zstandard`, `fastapi`, `uvicorn`, `jinja2`,
  `watchfiles`, `httpx`, `pydantic`, `pyarrow`, `duckdb`. None of them are
  optional — `zstandard` powers compression, `paramiko` powers SFTP/SSH.
- Dev extras: `pip install -e ".[dev]"` (pytest, build).

### Upgrading

Pull the latest code and re-install (`pip install -e .`). SQLite schema
migrations apply automatically the next time any command runs.

---

## Configuration

Run `backupme init` to copy the reference template, or create `config.yaml`
by hand — **[config.example.yaml](config.example.yaml) documents every
section and option the code reads** (it is pinned to the config dataclasses
by `tests/test_config_example_sync.py`). A minimal example:

```yaml
backends:
  local:
    - name: my_local
      path: D:/backups
      priority: 100

backups:
  - source: C:/Users/me/Documents
    backend: my_local

encryption:
  enabled: true
  key_file: ~/.backupme/key.enc
```

Secrets never need to be stored in plaintext: values support environment
variable substitution — `password: "{{FTP_PASS}}"` resolves from
`os.environ["FTP_PASS"]` at load time. `config.yaml` is gitignored; the
example file is the committed reference.

---

## CLI

Global options on every invocation: `--config/-c PATH`, `--db-path/--db PATH`,
`--verbose/-V`, `--version`. Most commands accept `--json` for
machine-readable output (human text stays on stdout, logs on stderr).
Run `backupme <command> --help` for the full option list of each command.

### Getting started

| Command | Description |
|---|---|
| `backupme init` | Scaffold `config.yaml` from `config.example.yaml`, create the DB, print next steps. Never overwrites. |
| `backupme backend add` | Add a storage backend interactively (local, sftp, ftp, smb, ssh, rclone, ninedrive) |
| `backupme backend list` | List configured backends |
| `backupme backend test NAME` | Test connectivity |
| `backupme backends` | Quick backend status summary |
| `backupme select-backend QUERY` | Fuzzy-find a configured backend by name |

### Backup and restore

| Command | Description |
|---|---|
| `backupme backup PATHS` | Backup files/directories; `-r` recurses, `-n` sets copy count, `-b` picks a backend, `--move` deletes sources after upload |
| `backupme upload-bundle DIR` | Batch-upload a directory's small files as tar bundles |
| `backupme download FILE_ID` | Restore one file by index id (`-o` output dir, `-v` version) |
| `backupme list` | List backed-up files (filters: `--backend`, `--hash`, `--source-root`) |
| `backupme search QUERY` | Search file paths (`--content` searches indexed text content) |
| `backupme hash-lookup SHA256` | Resolve a blob id or file sha256 (exact or unique prefix) |
| `backupme archive list/show/stats/restore` | Inspect and restore extract-first ingested archives |

### Verification and health

| Command | Description |
|---|---|
| `backupme verify` | Restore-and-hash verification; `--heal` repairs from donors/source; `--audit-disk` / `--audit-storage` audit physical presence |
| `backupme health` | Broken files, missing blobs, purge candidates |
| `backupme report verify/list/get/integrity` | Run and inspect stored integrity reports |
| `backupme stats` | Totals: files, blobs, dedup savings |
| `backupme daemon` | Run the scheduler in the foreground (backups, retention, ingest, purge, cold export, gc, verify ticks) |

### Maintenance

| Command | Description |
|---|---|
| `backupme db check/audit/info/oplog` | Database integrity, read-only vault audit, statistics, operation log |
| `backupme db snapshot/restore/rebuild` | DB snapshots, replica-based restore, manifest rebuild |
| `backupme db recover-from-manifests BACKEND` | Rebuild a fresh DB from archive manifest sidecars on a backend |
| `backupme db cold-export` / `db purge` / `db history "SQL"` | Export history to Parquet, hard-purge expired rows, query cold history with DuckDB SQL |
| `backupme db reconcile-refs` / `db reback-sources` / `db prune-oplog` / `db backfill-content` / `db vacuum` | Ref-count repair (report-only by default), source re-back, log pruning, content-index backfill, VACUUM |
| `backupme gc` | Reclaim objects no longer referenced by the index (dry run by default; `--apply` deletes) |
| `backupme convert BACKEND` | Re-upload a backend's files into the dedup layout (checkpointed) |
| `backupme recompress` | Recompress standalone blobs to a new algorithm/level |
| `backupme backend onboard/rebalance/drain/rescan` | Onboard, backfill, drain, and physically re-scan a backend |
| `backupme backend prune-phantom-rows NAME` | Find (and with `--apply` delete) DB rows claiming objects that do not exist on a backend |
| `backupme backend sync-namespace TARGET` | Copy manifests/tree/DB namespaces to another backend |
| `backupme backend remove NAME` | Remove a backend |
| `backupme encrypt init/status` | Generate and inspect the encryption key |

### cPanel ingest

| Command | Description |
|---|---|
| `backupme ingest-cpanel DROP_ROOT` | Stream `cpmove-*.tar.gz` members into the vault (no bulk extraction); `--watch` re-scans, `--dry-run` previews |
| `backupme ingest-status` | Per-account generations, dedup ratio, storage sizes |
| `backupme restore-cpanel ACC GEN` | Restore a cPanel account generation (loose files or re-assembled archive) |
| `backupme ingest-ftp` | Ingest files from an FTP source one by one |
| `backupme process-files` | Serial helper: download one file at a time from a backend |

See [docs/MANUAL.md](docs/MANUAL.md) sections 6 and 7b for backend and
cPanel-ingest configuration, and section 10 for every option of every
command.

---

## Web Dashboard

```bash
backupme web                 # http://127.0.0.1:8080
backupme web --port 8081     # what dashboard.bat uses
backupme web --host 0.0.0.0 --token "$DASHBOARD_TOKEN"
```

The dashboard binds to loopback by default and is open when `web.token` is
empty. Binding to a non-loopback address prints a loud warning — set a
bearer token (`web:` section of `config.yaml` or `--token`); every `/api/*`
request then requires `Authorization: Bearer <token>` while pages stay
public.

Pages: `/` (overview), `/files`, `/backends`, `/archives`, `/ingest`,
`/logs`, `/database`, `/report`, `/settings`, `/health`.

---

## Backends

All backends implement the same interface (see
`src/backupme/backends/base.py`): connect, upload/download/delete, streaming
walk, free/total space, server-side hash when available, health, fingerprint.

| Type | Module | Notes |
|---|---|---|
| `local` | `local.py` | Reference implementation |
| `ftp` / `sftp` | `ftp_sftp.py` | `ftplib` / `paramiko` |
| `smb` | `smb.py` | Windows SMB shares |
| `ssh` | `ssh.py` | Remote `df` / `sha256sum` |
| `rclone` | `rclone.py` | Any rclone-removable target |
| `ninedrive` | `ninedrive.py` | Self-hosted [9drive](https://github.com/zenhosta/9drive) pooling Google Drive + S3 accounts |

9drive example (fields verified against the backend module):

```yaml
backends:
  ninedrive:
    - name: pool1
      base_url: https://9drive.example.com
      api_key: "{{ NINEDRIVE_API_KEY }}"   # uploads; 9d_live_...
      email: backup@example.com            # or JWT login for list/download/delete
      password: "{{ NINEDRIVE_PASS }}"
      refresh_token: ""                    # optional: avoids storing the password
      folder: backupme                     # isolates blobs inside 9drive
```

Workflow for adding capacity: `backupme backend add`, then
`backupme backend onboard NAME --rebalance`. Retiring a backend:
`backupme backend drain NAME`, remove it from `config.yaml`, then
`backupme gc --apply`.

---

## How data is stored

```
file ──► sha256 ──► dedup by content (blobs table, ref-counted)
        ≤ chunking.min_size (default 10 MB): stored whole, batched into
          compressed tar bundles (~64 MB target); members addressed as
          bundle@offset:len
        > chunking.min_size: split into 64 MB content chunks
every blob ──► compress (adaptive zstd) ──► encrypt (AES-256-GCM) ──►
          upload to N backends ──► verify (download + hash) ──► manifest
SQLite hot store (live versions, single writer)
        ──► db cold-export ──► parquet/files_YYYY-MM.parquet (zstd-3)
        ──► db purge (past grace, below watermark) ──► db history "SQL" (DuckDB)
```

Key design decisions:

| Feature | Implementation |
|---|---|
| **Single-writer DB** | One primary SQLite (WAL mode, thread-local connections); every writer commits explicitly |
| **Generation counter** | Monotonic counter in `db_meta` — no split-brain |
| **Deduplication** | `blobs` table — `blob_id = SHA256(plaintext)`, ref-counted; donor-based re-mapping for identical content |
| **Chunking** | `chunking.min_size` (default 10 MB) decides whole-vs-chunked; 64 MB chunks |
| **Bundling** | Small files batched into tar bundles, ~64 MB target per bundle |
| **Compression** | Adaptive zstd levels: 19 (<64 KB), 10 (<1 MB), 3 (<64 MB), 1 (≥64 MB); gzip/bz2/lzma available |
| **Encryption** | AES-256-GCM, random 96-bit nonces, Argon2id key derivation |
| **Manifest sidecars** | Per-file JSON on each backend — enables full rebuild from storage |
| **Op log** | Idempotency keys + preconditions + status state machine |
| **GC safety** | `backupme gc` only deletes unreferenced objects past a grace period — a just-uploaded object is never eligible |
| **Atomic restores** | Write to temp sibling, rename only after the final SHA-256 matches |
| **Search** | FTS5 over file paths and (non-encrypted, small, UTF-8) content |

---

## Project structure

```
backupme/
├── config.example.yaml        # reference config — every option, commented
├── config.yaml                # your real config (gitignored)
├── pyproject.toml
├── docs/
│   ├── MANUAL.md              # full user guide
│   ├── PROJECT.md / BACKLOG.md
├── src/backupme/
│   ├── config.py              # YAML loader: dataclass per section, env-var substitution
│   ├── scheduler.py           # daemon: backups, retention, ingest, purge, export, gc, verify
│   ├── mirror.py              # N-copy redundancy helpers
│   ├── cli/                   # click command modules (base.py = entry point)
│   ├── core/                  # database, schema, dedup, uploader (chunking +
│   │                          # bundling), downloader, compress, crypto,
│   │                          # manifest, op_log, db_lifecycle, rebalance,
│   │                          # gc, verify, ...
│   ├── backends/              # local, ftp_sftp, smb, ssh, rclone, ninedrive
│   └── web/                   # FastAPI dashboard (routes/, templates/, static/)
└── tests/                     # pytest suite (env/backends fixtures in conftest.py)
```

## Testing

```bash
python -m pytest tests/ -q
```

---

## License

[MIT](LICENSE) — Copyright (c) 2026 BackupMe contributors.

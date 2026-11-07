# backupme — User Manual

Complete guide to using backupme for daily backups, restores, and administration.

---

## Table of Contents

1. [First-Time Setup](#1-first-time-setup)
2. [Basic Backup Workflow](#2-basic-backup-workflow)
3. [Restoring Files](#3-restoring-files)
4. [Searching Your Backups](#4-searching-your-backups)
5. [Web Dashboard](#5-web-dashboard)
6. [Backend Configuration](#6-backend-configuration)
7. [Advanced Features](#7-advanced-features)
8. [Maintenance & Monitoring](#8-maintenance--monitoring)
9. [Troubleshooting](#9-troubleshooting)
10. [Command Reference](#10-command-reference)

---

## 1. First-Time Setup

### 1.1 Install backupme

```bash
# Clone and install
git clone <your-repo-url>
cd backupme
pip install -e .

# Verify installation
backupme --help
```

### 1.2 Create Your Configuration

Create `config.yaml` in your project directory:

```yaml
# config.yaml — Minimal setup for local backups
backends:
  local:
    - name: my_usb_drive
      type: local
      path: D:/backups
      priority: 100

backups:
  - source: C:/Users/Maruf/Documents
    backend: my_usb_drive
    mirror: false

encryption:
  enabled: true
  key_file: ~/.backupme/key.enc

compression:
  enabled: true
  default_algorithm: zstd
```

### 1.3 Initialize Encryption (Recommended)

```bash
# Generate your encryption key
backupme encrypt init

# Verify it's set up
backupme encrypt status
```

Output:
```
Encryption key exists at C:\Users\Maruf\.backupme\key.enc
```

### 1.4 Run Your First Backup

```bash
backupme backup C:/Users/Maruf/Documents -b my_usb_drive --recursive
```

Output:
```
report.docx: file_id=142, dedup=False
budget2026.xlsx: file_id=143, dedup=False
photo.jpg: file_id=144, dedup=False inline=False
config.yaml: file_id=145, dedup=False inline=True
```

---

## 2. Basic Backup Workflow

### 2.1 Backup a Single File

```bash
backupme backup "C:/Users/Maruf/Documents/report.docx" -b my_usb_drive
```

### 2.2 Backup an Entire Directory

```bash
backupme backup C:/Users/Maruf/Documents -b my_usb_drive --recursive
```

### 2.3 Backup to Multiple Backends (Redundancy)

```bash
# Set up multiple backends in config.yaml
backends:
  local:
    - name: local_usb
      type: local
      path: D:/backups
  ftp:
    - name: office_ftp
      type: ftp
      host: ftp.example.com
      username: maruf
      password: "{{FTP_PASS}}"

# Backup with redundancy=2 (stored on both backends)
backupme backup ~/Documents --recursive --redundancy 2
```

### 2.4 What Gets Backed Up?

| File Size | How It's Stored | Notes |
|---|---|---|
| < 1 KB | Inline (text only) | Stored in DB as text, no separate blob |
| 1 KB – 64 KB | Tar bundle | Auto-batched for speed (3-5× better compression) |
| 64 KB – 1 MB | Single compressed blob | zstd level 10-19 |
| > 1 MB | Chunked (64 MB blocks) | Parallel upload, failover support |

### 2.5 Deduplication in Action

If you backup the same file twice, backupme detects it:

```bash
backupme backup report.docx -b my_usb_drive
# Output: report.docx: file_id=142, dedup=True  ← No upload needed!
```

---

## 3. Restoring Files

### 3.1 List Your Backed-Up Files

```bash
backupme list
```

Output:
```
 ID   Source Path                     Size     Redundancy
 ---  -----------                     ----     ----------
 142  C:/Users/Maruf/Documents/report.docx   45 KB    1
 143  C:/Users/Maruf/Documents/budget2026.xlsx  120 KB   1
 144  C:/Users/Maruf/Documents/photo.jpg    2.3 MB   1
```

### 3.2 Find a File's ID

Downloads take the numeric index id (find it with `backupme list` or
`backupme search`):

```bash
backupme list | grep "report.docx"
```

### 3.3 Download a File by ID

```bash
backupme download 142 -o ./restored/
```

### 3.4 Restore an Entire Directory or cPanel Account

Whole-directory restore runs from the web dashboard (Files → Browse →
"Restore this Folder"), and cPanel generations restore from the CLI:

```bash
backupme restore-cpanel mysite 3 --out ./restored_files/
```

### 3.5 Downloading from a Specific Backend

If the primary backend is unavailable, specify another:

```bash
backupme download 142 --backend office_ftp -o ./restored/
```

backupme automatically tries all healthy backends with failover!

---

## 4. Searching Your Backups

### 4.1 Search by Filename

```bash
backupme search "report"
```

Output:
```
 ID   Path                                    Size
 ---  ----                                    ----
 142  C:/Users/Maruf/Documents/report.docx   45 KB
 156  C:/Users/Maruf/Documents/old/report_old.docx  38 KB
```

### 4.2 Full-Text Search (Content Search)

```bash
backupme search --content "budget 2026"
```

Searches inside `.txt`, `.md`, `.py`, `.js`, `.html`, `.csv`, `.json`, `.xml`, `.yaml` files.

New uploads are indexed automatically: every unencrypted file of up to 64 KiB
whose bytes decode as UTF-8 (any extension, including cPanel-ingested members)
gets a `files.content_text` snippet at upload time, so content search matches
fresh data without a backfill; binary and encrypted backups are never indexed,
and re-uploading a file replaces the snippet with the new content's. Use
`backupme db backfill-content` to index rows that predate upload-time indexing
(see §10).

### 4.3 Search Within a Specific Source

```bash
backupme search "photo" --source C:/Users/Maruf/Pictures
```

### 4.4 Lookup by File Hash

```bash
backupme hash-lookup a1b2c3d4...
```

Resolves a SHA-256 hash to what is stored: a storage blob id, or the content
hash of one or more backed-up files (exact match first, then a hex prefix if
it uniquely identifies one file). File results show each version's id, path,
size, deleted status and the chunk-to-blob mapping with every backend copy.
An unknown or ambiguous hash prints `Hash not found` and exits 1; add `--json`
for a machine-readable payload.

---

## 5. Web Dashboard

### 5.1 Start the Dashboard

```bash
backupme web --port 8080
```

Then open `http://localhost:8080` in your browser.

**Protecting the API (recommended beyond loopback).** By default the dashboard
is open — safe because `backupme web` binds loopback-only and warns loudly if
you bind elsewhere. When exposing the port (LAN, container, reverse proxy),
set a bearer token; every `/api/*` request then requires
`Authorization: Bearer <token>` (401 otherwise). Static pages stay public so
the UI still loads:

```bash
backupme web --host 0.0.0.0 --token "$DASHBOARD_TOKEN"
```

or in `config.yaml`:

```yaml
web:
  token: "{{DASHBOARD_TOKEN}}"    # empty = open dashboard (loopback-only)
```

### 5.2 Dashboard Pages

| Page | URL | What You Can Do |
|---|---|---|
| **Dashboard** | `/` | Overview: file count, dedup ratio, compression ratio, per-backend usage chart |
| **Files** | `/files` | Searchable table of all files, download buttons, redundancy status |
| **Backends** | `/backends` | Health status, free/total space, circuit-breaker state, onboard/rebalance/drain |
| **Ingest** | `/ingest` | cPanel drop-folder queue with live progress, staging usage vs budget, per-account generations, history queries, pause/resume |
| **Logs** | `/logs` | Filterable operation log and recovery events |
| **Database** | `/database` | Integrity status, snapshots list, generation/instance info |
| **Report** | `/report` | Full integrity report with charts and recommendations |
| **Settings** | `/settings` | View config, toggle daemon, trigger manual backup |

### 5.3 API Access

The web dashboard also provides a REST API:

```bash
# Get backup status
curl http://localhost:8080/api/status

# List files (JSON)
curl http://localhost:8080/api/files?q=report

# Get integrity report
curl http://localhost:8080/api/report

# With a token configured (see 5.1):
curl -H "Authorization: Bearer $DASHBOARD_TOKEN" http://localhost:8080/api/status
```

**Destructive endpoints require explicit confirmation.** Server restart, DB restore/rebuild,
backend removal, and snapshot delete/prune reject requests (HTTP 400) unless the JSON body
carries `"confirm": true` — the dashboard sends this automatically after its confirm dialog,
and scripts must send it explicitly:

```bash
curl -X POST -H "Content-Type: application/json" \
     -d '{"confirm": true}' http://localhost:8080/api/db/rebuild
```

---

## 6. Backend Configuration

### 6.1 Local Backend (USB Drive, External Disk)

```yaml
backends:
  local:
    - name: usb_backup
      type: local
      path: D:/backups
      priority: 100          # Higher = preferred
```

### 6.2 FTP Backend

```yaml
backends:
  ftp:
    - name: my_ftp
      type: ftp
      host: ftp.example.com
      port: 21
      username: myuser
      password: "{{FTP_PASSWORD}}"   # From environment variable
      priority: 50
```

**Note:** Use `{{VAR_NAME}}` for passwords — backupme resolves them from environment variables automatically.

### 6.3 SFTP (SSH FTP) Backend

```yaml
backends:
  ftp:
    - name: my_sftp
      type: sftp
      host: sftp.example.com
      port: 22
      username: myuser
      key_file: ~/.ssh/id_rsa      # SSH key auth (recommended)
      priority: 50
```

### 6.4 SMB (Windows Network Share)

```yaml
backends:
  smb:
    - name: office_share
      type: smb
      share: //SERVER/Backups
      username: MARUF
      password: "{{SMB_PASS}}"
      priority: 60
```

### 6.5 SSH Backend (rsync/scp)

```yaml
backends:
  ssh:
    - name: remote_server
      type: ssh
      host: backup-server.example.com
      username: maruf
      key_file: ~/.ssh/id_rsa
      remote: /mnt/backups          # Remote path
      priority: 70
```

### 6.6 rclone Backend (50+ Cloud Providers)

First, set up rclone: `rclone config`

```yaml
backends:
  rclone:
    - name: google_drive
      type: rclone
      remote: gdrive:backups      # rclone remote:path
      bwlimit: 10M                # Bandwidth limit (optional)
      priority: 40
```

**Supported rclone remotes:** Google Drive, Dropbox, S3, B2, Azure, OneDrive, and 50+ more.

### 6.7 9drive Backend (Google Drive + S3 Pool)

If you self-host [9drive](https://github.com/zenhosta/9drive), its pooled Google Drive accounts and S3 buckets can serve as a backupme backend:

```yaml
backends:
  ninedrive:
    - name: ninedrive_pool
      type: ninedrive
      base_url: https://9drive.example.com   # your 9drive backend URL
      api_key: "{{ NINEDRIVE_API_KEY }}"     # 9d_live_... key (files:upload scope)
      email: backup@example.com              # JWT login for list/download/delete
      password: "{{ NINEDRIVE_PASS }}"
      refresh_token: ""                      # optional: skip password after first login
      folder: backupme                       # isolates blobs inside 9drive
      priority: 60
```

Notes:

- **Auth split:** uploads use the 9drive API key (`POST /api/v1/uploads`); listing, downloads, deletes, and quota queries need a JWT (email/password login or `refresh_token`). An API-key-only config runs in upload-only mode.
- Blobs land in the `backupme` folder inside your 9drive instance; blob paths map 1:1 to 9drive file names.
- backupme's 64 MB chunking keeps uploads well under 9drive's per-upload size cap (`MAX_UPLOAD_BYTES`, default 5 GB).
- 9drive force-shares Google Drive uploads as "anyone with link can edit". Enable backupme encryption so blob contents stay unreadable, or connect only S3 accounts to 9drive.
- Deletes are permanent (trash + purge) so `backupme gc` actually reclaims pool quota.

---

## 7. Advanced Features

### 7.1 N-Copy Redundancy (Mirroring)

Ensure every file is stored on N backends:

```yaml
# In config.yaml
backups:
  - source: ~/Documents
    backend: local_usb
    mirror: true          # Enable mirroring
```

Or specify at command line:

```bash
backupme backup ~/Documents --redundancy 3
```

### 7.2 Versioning & Retention

backupme keeps multiple versions of your files:

```yaml
retention:
  enabled: true
  versions: 5        # Keep last 5 versions of each file
  daily: 7           # Keep 1 version per day for 7 days
  weekly: 4          # Keep 1 version per week for 4 weeks
  monthly: 12        # Keep 1 version per month for 12 months
```

View older versions of a file:

```bash
backupme list | grep "report.docx"
```

### 7.3 Daemon Mode (Automatic Backups)

```bash
# Runs the scheduler in the FOREGROUND — it blocks until Ctrl+C.
# Run it in its own terminal or as a service.
backupme daemon
```

The daemon periodically:
- Scans your source directories for changes
- Runs new backups
- Creates database snapshots
- Applies retention policy

### 7.4 Convert Old Backend Files

If you have files on a backend that weren't backed up with backupme's dedup layout:

```bash
# Walk the backend and re-upload to dedup layout
backupme convert my_ftp --recursive

# Delete originals after verifying they're safely replicated
backupme convert my_ftp --recursive --delete

# Resume if interrupted
backupme convert my_ftp --resume
```

### 7.5 Recompression

Change compression settings for all stored blobs:

```bash
# Recompress all blobs to zstd level 3
backupme recompress --algo zstd --level 3

# Recompress only on a specific backend
backupme recompress --backend my_ftp --algo zstd --level 19
```

This uses the safe write-new→verify→commit→delete-old pipeline (no data loss risk).

### 7.6 Move Feature (Delete Source on Success)

If you want to move files to your backup backends rather than copying them (i.e. delete the original files from the local source disk after a successful backup), you can use the move option:

**Via CLI:**
```bash
# Add --move or -m option to delete local files upon successful upload
backupme backup ~/Docs -b my_usb_drive -r --move
```

**Via Config (for background scheduler/daemon):**
Add `move: true` to the backups list:
```yaml
backups:
  - source: D:\MySourceFolder
    target: hostraj Gdrive
    move: true                      # Deletes local files after verified replication
```

Deletions are highly secure: they are executed strictly after a successful database commit and verification signature check.

---

## 7b. cPanel Ingest

The ingest pipeline takes cPanel account archives (`cpmove-*.tar.gz`) and streams
their members into the dedup vault — one file at a time, with per-archive
checkpoints, idempotency, and optional raw-archive retention. History beyond
your retention window lives in monthly Zstd-compressed Parquet files that you
query read-only with DuckDB.

### How it works

1. cPanel (or you) drops `cpmove-*.tar.gz` files into the **drop root**
   (e.g. `/srv/drop/<account>/`).
2. `backupme ingest-cpanel /srv/drop` (or the daemon's ingest tick, every
   `scheduler.ingest_interval` seconds) discovers new archives and streams
   each member through the standard dedup pipeline: hashing, chunking,
   compression, encryption, upload.
3. Plaintext exists only in `staging/<archive-sha>/` for the lifetime of one
   member upload, then is deleted. Symlinks, devices, and unsafe paths are
   skipped. After success the archive moves to `<drop_root>/.done/<account>/`.
4. Re-ingesting the same archive is a no-op (content-hash idempotency); a
   changed archive ingests as **generation 2** — unchanged members are dedup
   hits, so the second generation costs near-zero new bytes.

### First-time setup

```yaml
# config.yaml
cpanel_ingest:
  enabled: true
  drop_root: /srv/drop
  staging_dir: staging          # keep on encrypted storage if possible
  staging_budget_gb: 10
  max_parallel: 2
  keep_raw: 0                   # set to 1+ to also store raw archives
  max_member_size_gb: 5
```

Optional, for cold history queries:

```bash
pip install pyarrow duckdb
```

On the cPanel host, push archives automatically:

```bash
*/15 * * * *  rsync -a --remove-source-files /backup/cpmove-*.tar.gz backupme-server:/srv/drop/\$USER/
```

### Daily use

```bash
backupme ingest-cpanel /srv/drop            # one-shot
backupme ingest-cpanel /srv/drop --watch    # keep re-scanning (Ctrl+C to stop)
backupme ingest-cpanel /srv/drop --dry-run --json   # what would happen
backupme ingest-status                      # accounts, generations, dedup ratio, storage sizes
```

Each archive result reports `excluded` (members matched by
`exclude_members` globs) and `skipped_oversize` (members larger than
`max_member_size_gb`) alongside `members`/`dupe`/`errors`; the generation
manifest carries the same counters, so a restore-gap is always explainable
from the manifest rather than only from log lines.

On the Ingest page, **Pause** stops the worker between archives (queued
archives are reported as `deferred` and the queue counters stay consistent).
While paused, `POST /api/ingest/scan` returns **409** — a pause is explicit
consent to not ingest, and only **Resume** re-enables scans.

### Restoring

```bash
# Loose files for generation 2 of account "acme":
backupme restore-cpanel acme 2 --out /tmp/acme-gen2

# A re-assembled tar.gz (member names preserved):
backupme restore-cpanel acme 2 --retar acme-gen2.tar.gz

# Just one member (fnmatch pattern):
backupme restore-cpanel acme 2 --member-filter "homedir/public_html/wp-config.php"
```

### Cold history (SQLite stays small)

```bash
backupme db cold-export      # copy new rows to parquet/files_YYYY-MM.parquet
backupme db purge            # hard-delete rows past grace that are already exported
backupme db history "SELECT relative_path, version_seq, size FROM files
                     WHERE source_root = 'cpanel/acme' ORDER BY version_seq DESC LIMIT 20"
```

The web dashboard's **Ingest** page shows the drop-folder queue with live
progress, staging usage vs budget, SQLite + Parquet health, per-account
generation drill-downs, and one-click restore. Settings has a "cPanel Ingest"
section for editing every knob.

### Safety notes

- Staging holds plaintext briefly — use an encrypted volume or tmpfs, mode 0700.
- `keep_raw` re-uploads the original archive (it dedups across generations).
- Ingest failures are recorded in `op_log` and surfaced on the Ingest page
  (or `backupme ingest-status --account NAME`) for alerting.

---

## 8. Maintenance & Monitoring

### 8.0 Data Health & Restore Verification

`backupme health` shows a live data-health summary computed from the index: files with no
stored chunks, files whose chunks reference missing blobs, same-name groups, and purge
candidates (files that can never restore). The same data is on the dashboard's Data Health
page.

```bash
backupme health                       # human-readable summary
backupme health --json                # machine-readable
backupme health --purge-candidates    # also list purge candidates
```

`backupme verify` restores real files to a temp location and compares sha256 against the
index (the strongest check — it proves content is actually recoverable):

```bash
backupme verify                       # sample size from config (verification.verify_restore.count)
backupme verify --count 20            # check 20 random live files
backupme verify --backend storagebox  # restrict to one backend
backupme verify --file 1234           # verify a single file id
```

Exit code is 0 when everything checked OK, 1 when any file failed. Broken files surface in
`backupme health --purge-candidates` and on the dashboard, where purging them requires
explicit confirmation and is soft-delete only.

**Failure notifications** (`notifications` config section): failures that always reach the
log — data-health regressions found by the daemon and cPanel ingest archive failures — also
fire a desktop toast when `notifications.on_failure` and `notifications.desktop` are both
enabled. The toast needs the optional `plyer` package (`pip install plyer`); without it the
failure is still logged.

### 8.1 Database Health Check

```bash
backupme db check
```

Output:
```
Integrity check passed.
Generation: 42
Instance ID: a1b2c3d4-5e6f-7890-abcd-ef1234567890
```

For a thorough check:
```bash
backupme db check --full
```

### 8.2 Create a Manual Snapshot

```bash
backupme db snapshot
```

### 8.3 View Database Statistics

```bash
backupme db info
```

Output:
```
Database: backupme.db
Generation: 42
Instance ID: a1b2c3d4-5e6f-7890-abcd-ef1234567890
Total files: 1,247
Total blobs: 892
Deduplication savings: 3.2 GB (68% reduction)
Compression savings: 1.8 GB (additional 22% reduction)
```

### 8.4 Verify Backups (Random Restore Test)

```bash
backupme verify --count 5
```

This randomly downloads 5 files, restores them, and verifies their hashes.
Exit code 1 on any failure. Restrict to one backend with `--backend NAME`,
or verify a single file with `--file ID`.

### 8.5 Integrity Report

```bash
# Run a check and store a report row
backupme report verify --count 10

# List stored reports / read one
backupme report list
backupme report get 4

# JSON summary of the most recent check
backupme report integrity
```

### 8.6 Check Backend Consistency

```bash
# Walk a backend and diff against the index
# (missing_on_box / extra_on_box / size_mismatch; exit 1 on problems)
backupme backend rescan NAME
```

### 8.7 Garbage Collection

```bash
backupme gc              # dry run: list orphaned remote objects per backend
backupme gc --apply      # delete them
```

`gc` deletes remote objects (under `objects/` and `bundle/`) that the index no
longer references — typically leftovers after retention purged old versions.
Objects **younger than one hour are never candidates**: an upload lands on the
backend before its database rows commit, so a just-written object can look
unreferenced for a short window, and deleting it would corrupt the in-flight
backup. Objects whose size listing carries no timestamp are skipped for the
same reason (a leak is recoverable; a deleted live blob is not).

---

## 9. Troubleshooting

### 9.1 "No backends available"

**Cause:** Backend configuration is missing or backends are unhealthy.

**Fix:**
```bash
# Check backend configuration
cat config.yaml

# Test backend connectivity
backupme list
# If this fails, check:
# - Backend credentials
# - Network connectivity
# - Backend path exists and is writable
```

### 9.2 "Failed to download chunk"

**Cause:** Backend is down or file is corrupted.

**Fix:**
```bash
# backupme automatically tries all backends with failover
# Check which backends are healthy:
backupme backends

# Manually specify a healthy backend:
backupme download 142 --backend healthy_backend_name
```

### 9.3 "Integrity mismatch"

**Cause:** File was corrupted on the backend.

**Fix:**
```bash
# Run verification to find corrupted files (exit 1 on any failure)
backupme verify --count 20

# Read the stored report
backupme report list
backupme report get 4

# If a backend is consistently corrupted, restore the DB from replicas:
backupme db restore
```

Restores are **atomic**: the file is written to a temporary sibling and
renamed into place only after every chunk downloaded and the final SHA-256
matched. A failed restore leaves the destination untouched (a previous good
copy survives) and no `.part-*` temp files behind.

### 9.4 "Operation log has pending entries"

**Cause:** backupme crashed during an operation.

**Fix:**
```bash
# Check pending operations
backupme db oplog --pending

# Resume by running the interrupted command again
# backupme automatically recovers from checkpoints
```

### 9.5 Encryption Key Issues

```bash
# Check key status
backupme encrypt status

# Re-initialize if lost (WARNING: existing encrypted files won't be readable!)
backupme encrypt init
```

### 9.6 Performance Issues

**Slow uploads?**
- Use `--redundancy 1` for faster single-backend uploads
- Enable compression: `compression: enabled: true` in config
- Small files are auto-bundled (under 10MB) into 64MB tar archives for 3-5× faster transfers

**Slow downloads?**
- Check backend health: `backupme backends`
- Use local backends when possible
- Download from fastest backend: `backupme download ID --backend fast_backend`

---

## 10. Command Reference

Verified against the code (CLI QA sweep, 40 leaf commands). Global options on
every invocation: `--config/-c PATH`, `--db-path/--db PATH`, `--verbose/-V`.

### Quick Command Summary

| Command | Description | Example |
|---|---|---|
| `backupme backup PATHS` | Backup files/directories (exit 1 on a missing path) | `backupme backup ~/Docs -b local --recursive --move` |
| `backupme upload-bundle DIR` | Batch-upload small files (1KB–64KB) as a bundle | `backupme upload-bundle ~/Photos -b local` |
| `backupme download FILE_ID` | Restore one file by index id | `backupme download 142 -o ./restored/` |
| `backupme restore-cpanel ACC GEN` | Restore a cPanel account generation | `backupme restore-cpanel mysite 3 --out ./restored/` |
| `backupme list` | List backed-up files | `backupme list --backend local --json` |
| `backupme search QUERY` | Search the index (path search; punctuation-safe) | `backupme search "report-2026"` |
| `backupme stats` | Totals: files, blobs, dedup savings | `backupme stats --json` |
| `backupme verify` | Restore-and-hash verification (exit 1 on failure) | `backupme verify --count 10` |
| `backupme health` | Data health summary (broken files, purge candidates) | `backupme health --json` |
| `backupme backend rescan NAME` | Walk a backend, diff vs index (exit 1 on missing/mismatch) | `backupme backend rescan storagebox` |
| `backupme report verify` | Run an integrity check, store a report | `backupme report verify --count 10` |
| `backupme convert BACKEND` | Convert backend storage layout | `backupme convert ftp --delete --resume` |
| `backupme db check` | Database integrity (read-only unless `--repair`) | `backupme db check --full` |
| `backupme db snapshot` | Create snapshot | `backupme db snapshot` |
| `backupme db restore` | Restore DB from backend replicas (consensus) | `backupme db restore` |
| `backupme db rebuild` | Rebuild index from manifests | `backupme db rebuild` |
| `backupme encrypt init` | Initialize encryption key | `backupme encrypt init` |
| `backupme recompress` | Recompress standalone blobs (bundles are skipped) | `backupme recompress --level 19` |
| `backupme daemon` | Run the scheduler in the FOREGROUND (blocks; Ctrl+C stops) | `backupme daemon` |
| `backupme web` | Start web dashboard | `backupme web --port 8080` |

### All CLI Options

```bash
# files & backup
backupme backup [PATHS]... [-b/--backend NAME] [-n/--redundancy N] [-r/--recursive]
                             [--no-skip] [--force] [-m/--move] [--json]
backupme upload-bundle DIRECTORY [-b/--backend NAME] [-n/--redundancy N]
backupme download FILE_ID [-o/--output DIR] [-v/--version N] [--backend NAME] [--json]
backupme list [-b/--backend NAME] [--hash SHA256] [--source-root ROOT]
              [-l/--limit N] [-o/--offset N] [--json]
backupme search QUERY [--content] [--source ROOT] [--json]
backupme hash-lookup SHA256 [--json]
                             # resolves blob ids AND file sha256 (exact match,
                             # or a unique hex prefix); exit 1 if not found or
                             # the prefix is ambiguous
backupme select-backend QUERY

# monitoring & verification
backupme stats [--json]
backupme backends [--json]
backupme verify [--count N] [--backend NAME] [--file ID]
backupme health [--json] [--purge-candidates] [--limit N]
backupme report list
backupme report get REPORT_ID            # exit 1 if missing
backupme report verify [--count N] [--backend NAME]
backupme report integrity                # JSON summary of the last check

# maintenance
backupme convert BACKEND_NAME [--prefix PATH] [--delete] [--target BACKEND] [--resume] [--json]
backupme recompress [--algo zstd] [--level 3] [-b/--backend NAME]
backupme gc [--apply] [--json]       # DESTRUCTIVE with --apply; default dry run
backupme db check [--full] [--repair] [--json]
backupme db snapshot [--json]
backupme db restore [--json]         # no options; consensus from replicas
backupme db rebuild [--json]
backupme db oplog [--pending] [--json]
backupme db info [--json]
backupme db cold-export [--dry-run] [--json]
backupme db purge [--grace-days N] [--json]
                                     # DESTRUCTIVE; soft-deleted rows above the
                                     # watermark are kept and reported as
                                     # above_watermark
backupme db history "SQL" [--json/--no-json]
                                     # JSON is the default; --no-json renders a
                                     # table plus a row-count summary
backupme db vacuum
backupme db prune-oplog [--days N] [--json]
backupme db backfill-content [--limit N] [--max-size BYTES] [--backend NAME]
                             [--dry-run] [--json]
                                     # small text files only; never runs when
                                     # encryption is enabled; resumable

# backends
backupme backend add [TYPE] [--name --host --port --username --password --key-file
                     --path --share --remote --base-url --api-key --email
                     --refresh-token --folder --bwlimit --quota --priority --weight]
                     [--test/--no-test]
backupme backend list [--json]
backupme backend remove NAME         # exit 1 if the name is unknown
backupme backend test NAME [--json]  # exit 1 on unknown name or failed connect
backupme backend onboard NAME [--rebalance] [--dry-run] [--json]
backupme backend rebalance NAME [--limit N] [--dry-run] [--json]
backupme backend drain NAME [--dry-run] [--json]
                                     # exit 1 on unknown name; never deletes
backupme backend rescan NAME [--json]

# encryption, ingest, servers
backupme encrypt init
backupme encrypt status
backupme ingest-cpanel DROP_ROOT [-n N] [--keep-raw N] [--dry-run] [--progress]
                         [--watch] [--interval N] [--json]
backupme ingest-status [--account NAME] [--drop-root PATH] [--json]
backupme restore-cpanel ACCOUNT GENERATION [--member-filter GLOB] [--out DIR]
                           [--retar FILE] [--json]
backupme recompress [--algo] [--level] [--backend]
backupme daemon                      # foreground scheduler; no start/stop/status
backupme web [--host 127.0.0.1] [--port 8080] [--token TOKEN]
```

### Content-Index Backfill

`backupme db backfill-content` fills `files.content_text` for rows that predate
upload-time indexing: small strict-UTF-8 text files only (default cap 64 KB), and
a no-op (`skipped_encrypted`) when encryption is enabled. It is non-destructive
and resumable — re-run until `remaining` is 0; `--dry-run` only counts candidates.

---

## Tips & Best Practices

1. **Always enable encryption** for sensitive files — `backupme encrypt init`
2. **Use redundancy=2 or higher** for critical data
3. **Run `backupme verify` weekly** to catch corruption early
4. **Monitor with the web dashboard** — `backupme web`
5. **Set up daemon mode** for automatic, scheduled backups
6. **Use environment variables** for passwords: `password: "{{MY_SECRET}}"`
7. **Small files are auto-bundled** — no need to manually optimize
8. **Check `backupme report` monthly** for health recommendations

---

*backupme User Manual — PLAN_FIXED fully implemented*

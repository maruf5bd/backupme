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

### 3.2 Download a File by Path

```bash
backupme download "C:/Users/Maruf/Documents/report.docx" --dest ./restored/
```

### 3.3 Download a File by ID

```bash
backupme download 142 --dest ./restored/report.docx
```

### 3.4 Restore an Entire Directory

```bash
backupme restore --source C:/Users/Maruf/Documents --dest ./restored_files/
```

### 3.5 Downloading from a Specific Backend

If the primary backend is unavailable, specify another:

```bash
backupme download 142 --from office_ftp --dest ./restored/
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

### 4.3 Search Within a Specific Source

```bash
backupme search "photo" --source C:/Users/Maruf/Pictures
```

### 4.4 Lookup by File Hash

```bash
backupme hash-lookup a1b2c3d4...
```

Finds all files that match a given SHA-256 hash.

---

## 5. Web Dashboard

### 5.1 Start the Dashboard

```bash
backupme web --port 8080
```

Then open `http://localhost:8080` in your browser.

### 5.2 Dashboard Pages

| Page | URL | What You Can Do |
|---|---|---|
| **Dashboard** | `/` | Overview: file count, dedup ratio, compression ratio, per-backend usage chart |
| **Files** | `/files` | Searchable table of all files, download buttons, redundancy status |
| **Backends** | `/backends` | Health status, free/total space, circuit-breaker state |
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
backupme list --source ~/Documents | grep "report.docx"
```

### 7.3 Daemon Mode (Automatic Backups)

```bash
# Start the daemon (runs in background)
backupme daemon start

# Check status
backupme daemon status

# Stop the daemon
backupme daemon stop
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

## 8. Maintenance & Monitoring

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

For a full check:
```bash
backupme verify --all
```

### 8.5 Integrity Report

```bash
backupme report
```

Output includes:
- Total file count and sizes
- Deduplication and compression ratios
- Per-backend health status
- Recent verification results
- Recommendations

HTML report:
```bash
backupme report --format html > report.html
```

### 8.6 Check Backend Consistency

```bash
backupme check-consistency
```

Ensures all backends have consistent copies of mirrored files.

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
backupme status

# Manually specify a healthy backend:
backupme download 142 --from healthy_backend_name
```

### 9.3 "Integrity mismatch"

**Cause:** File was corrupted on the backend.

**Fix:**
```bash
# Run full verification to find all corrupted files
backupme verify --all

# Check the report for details
backupme report

# If a backend is consistently corrupted, restore from another:
backupme db restore --from healthy_backend
```

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

# Change password (re-encrypts key file)
backupme encrypt change-password
```

### 9.6 Performance Issues

**Slow uploads?**
- Use `--redundancy 1` for faster single-backend uploads
- Enable compression: `compression: enabled: true` in config
- Small files are auto-bundled (under 10MB) into 64MB tar archives for 3-5× faster transfers

**Slow downloads?**
- Check backend health: `backupme status`
- Use local backends when possible
- Download from fastest backend: `backupme download ID --from fast_backend`

---

## 10. Command Reference

### Quick Command Summary

| Command | Description | Example |
|---|---|---|
| `backupme backup PATHS` | Backup or move files/directories | `backupme backup ~/Docs -b local --recursive --move` |
| `backupme upload-bundle DIR` | Batch-upload small files | `backupme upload-bundle ~/Photos -b local` |
| `backupme download ID\|PATH` | Restore a file | `backupme download 142 --dest ./restored/` |
| `backupme restore` | Restore entire directory | `backupme restore --source ~/Docs --dest ./old/` |
| `backupme list` | List backed-up files | `backupme list --source ~/Docs` |
| `backupme search QUERY` | Search files | `backupme search "report" --content` |
| `backupme status` | Show backup status | `backupme status` |
| `backupme verify` | Verify random files | `backupme verify --count 10` |
| `backupme report` | Full integrity report | `backupme report --format html` |
| `backupme convert BACKEND` | Convert backend files | `backupme convert ftp --delete --resume` |
| `backupme db check` | Database integrity | `backupme db check --full` |
| `backupme db snapshot` | Create snapshot | `backupme db snapshot` |
| `backupme db restore` | Restore from replica | `backupme db restore --from ftp` |
| `backupme db rebuild` | Rebuild from manifests | `backupme db rebuild` |
| `backupme encrypt init` | Initialize encryption | `backupme encrypt init` |
| `backupme recompress` | Recompress blobs | `backupme recompress --level 19` |
| `backupme daemon start` | Start daemon | `backupme daemon start` |
| `backupme web` | Start web dashboard | `backupme web --port 8080` |

### All CLI Options

```bash
backupme backup [PATHS]... [--backend NAME] [--redundancy N] [--recursive/-r] [--move/-m]
backupme upload-bundle DIR [--backend NAME] [--redundancy N]
backupme download <file_id|path> [--from BACKEND] [--dest PATH]
backupme restore [--source ROOT] [--dest PATH]
backupme list [--source ROOT] [--backend NAME] [--hash VALUE]
backupme search "query" [--content] [--source ROOT]
backupme hash-lookup <sha256>
backupme status
backupme verify [--count N] [--backend NAME] [--all] [--recent N]
backupme report [--format html|json|text]
backupme check-consistency
backupme scrub [--backend NAME]
backupme convert BACKEND [--prefix PATH] [--target BACKEND] [--delete] [--resume]
backupme db check [--full]
backupme db snapshot
backupme db restore [--from BACKEND] [--snapshot FILE]
backupme db rebuild
backupme db oplog [--pending]
backupme db info
backupme encrypt init
backupme encrypt change-password
backupme encrypt status
backupme recompress [--algo zstd] [--level 3] [--backend NAME]
backupme daemon start|stop|status
backupme web [--port 8080] [--host 0.0.0.0]
```

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

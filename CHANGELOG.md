# Changelog

All notable changes to BackupMe are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## [2.1.0] — 2026-11-22

### Security
- CSRF middleware on all state-changing methods; constant-time token compare;
  token-armed pages gated via query token / header / HttpOnly cookie (D28).
- Config import requires literal-true confirm and writes a timestamped backup;
  encrypt/init refuses to replace an existing key without confirm; rotations
  keep the old key (D26).
- Restore destinations, snapshot download/delete, and source writes are guarded
  against traversal, UNC, and reserved/ADS names (D27, D36); uploads reject
  NTFS alternate streams, reserved device names, and literal .. segments
  (core path_guard).
- Config export redaction is word-part aware; backend details withhold every
  secret-shaped field; re-importing a redacted export is refused per-field (D29).
- Destructive operations (scan delete_source, convert delete, file delete,
  config/scheduler/source writes) require explicit confirm (D35).

### Fixed
- One backend resolver for web, CLI, and core: location-scoped instance cache,
  single-flight connects, 60s failure cooldown, honest unreachable reporting;
  the web/CLI divergence and double-SSH-session churn are gone (D30).
- Bundle memory cache scoped by backend root and cleared per test session —
  stale RAM bytes could previously bypass the sha256 gate on restores and
  verification (D37).
- Checkpoint writers use per-process tmp names; concurrent processes no longer
  crash an archive ingest with WinError 32 on Windows (D38).
- Retention: calendar-accurate monthly window; one malformed created_at row no
  longer stalls retention forever (D34).
- Re-uploading a path whose latest row is soft-deleted no longer rolls back the
  whole batch; the dead auto-heal source re-back works again (D36).
- Scheduler: tick guards + failure notifications on every tick; backups no
  longer abort wholesale on one vanished file (D33).
- cPanel ingest: partial member failures resume on the next run instead of
  stranding members; archives stay in the drop root until fully ingested (D21).
- Changed re-uploads keep chunk mappings consistent (D9-era fix hardened across
  the batch/donor/chunked/migration paths).
- CLI backend add stores sftp passwords and refuses an unusable ssh password
  loudly (D31).
- Legacy 2026-05 envelope objects and bundle members restore with full hash
  gating (D8-era downloader, hardened by the D37 cache fix).

### Changed
- The scheduler dispatches cadence ticks (ingest, purge, cold export, guard,
  gc, verify) to a worker pool — a long ingest no longer suspends other ticks;
  per-tick single-flight and all config gates are preserved (D33-SCH-2).
- Blocking handlers moved off the event loop (db restore/rebuild/replicate,
  report page); the web ingest worker shares the daemon single-flight lock (D35).
- All web/CLI/backend layers emit the same backend field set; rclone keeps
  config_path/extra_args/timeout_s across layers; {{ ENV }} templates resolve
  once in load_config (D30).
- Archive manifests are written with per-member chunk recipes; results are
  matched by local_path so manifest rows can never rotate (D32).

### Added
- Backend resolver contract: available/unreachable split, connect cooldown,
  per-instance connect error surfacing (D30).
- Guard: off-site DB snapshot shipping; archives: extract-first backup with
  manifest sidecars; dashboard: backend speed test, restore queue, archives
  page (v2.0.x waves).

## [2.0.0] — 2026-11-15

### Added
- Deduplicating backup core: content-addressed blob store, tar bundle batching for
  small files, ref-counted dedup by sha256, incremental versioning.
- Pluggable storage backends: local, SFTP/SSH, SMB, FTP, rclone (any remote),
  9drive; per-backend onboarding, rebalancing, draining, orphan rescan
  (`backend rescan --register`).
- cPanel ingest: extract-first streaming of cpmove archives with traversal
  sanitization and per-archive idempotency.
- FTP one-by-one ingest (`ingest-ftp`) and serial per-file backend processing
  (`process-files`) via CLI and dashboard.
- Integrity suite: restore-and-hash verification, physical presence audit
  (`verify --audit-disk / --audit-storage`), automated self-healing
  (`verify --heal`) with donor-backend and live-source resolution, vault audit
  (`db audit`), refcount reconciliation (`db reconcile-refs`), phantom-row
  pruning (`backend prune-phantom-rows`), source re-back (`db reback-sources`).
- Cold history: monthly zstd-compressed Parquet exports with DuckDB query CLI.
- Web dashboard: FastAPI + Jinja2 pages for files, backends, database ops,
  ingest, archives, health, scheduler; background-task progress with SSE,
  bearer-token auth option, backend speed test, restore queue panel.
- Packaging: standard src/ layout, `pip install -e .`, `backupme` console entry
  point, `--version` flag.

### Fixed
- Refcount drift and donor-selection bugs in the dedup pipeline.
- SFTP resilience: keepalive, dead-transport detection, 3-attempt upload retry.
- `backup` now exits 1 when any archive or file errors occur.

### Known issues
- Starlette `TemplateResponse` deprecation warning (web templates) — queued.
- No CI badge yet: GitHub Actions workflow added in this release.

## [1.x] — 2025–2026

Initial development line: single-node local backups, SQLite index, early
compression pipeline. Superseded by the 2.0 architecture.

# Changelog

All notable changes to BackupMe are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

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

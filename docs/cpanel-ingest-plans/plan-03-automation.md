# Plan 03 — Daemon Automation

## Goal
Daemon tick runs backups → retention → ingest scan → hard-purge → cold export → scheduled gc/verify, all exception-guarded and single-flight.

## Modified files
- `backupme/scheduler.py`
  - `BackupScheduler.__init__` reads `config["scheduler"]` (fixes hardcoded-3600 gap; `interval`, `ingest_interval=300`, `gc_interval_days=7`, `verify_interval_days=1`, `purge_grace_days`, `cold_export_after_days`)
  - `_periodic_loop`: backups → `_run_retention()` → `_run_ingest()` (every `ingest_interval`) → `_run_purge()` (daily) → `_run_cold_export()` (monthly) → scheduled `gc.run_gc(backends, apply=True)` → scheduled `verify.run_integrity_check(recent=N)`
  - `_run_ingest` / `_run_purge` / `_run_cold_export`: guard on `cpanel_ingest.enabled`, single-flight flags (no overlapping ticks), log exceptions like existing tasks
  - staging budget gate + token-bucket pacing live here (enforced in `ingest_tree` loop)

## Config
- `scheduler:` section in `config.yaml.example` documents all cadences

## Verification
- drop archive into watched folder → auto-ingested within `ingest_interval`
- retention prunes after 8 gens → cold export writes partition → purge removes rows → `gc --apply` reclaims orphans
- overlapping ticks never run concurrently (flag test)

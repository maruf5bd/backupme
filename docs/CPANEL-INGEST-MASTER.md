# cPanel Ingest Pipeline — Master Plan

Master document merging all six phase plans. Source phase files live in `docs/cpanel-ingest-plans/plan-0X-*.md`. Approved topology: SQLite hot + DuckDB over Zstandard Parquet.

---

# Plan 01 — Core Ingest (CLI one-shot) + Full CLI Upgrade

## Goal
`backupme ingest-cpanel DROP_ROOT` discovers `cpmove-*.tar.gz` archives, streams members one-at-a-time through the existing `upload_bundle` pipeline with explicit `version_group`/`version_seq`, checkpoints per archive, and dispositions raw archives per `keep_raw`. Plus cross-cutting CLI upgrades: progress display, `--json` machine output, `--watch` mode, richer status everywhere.

## New files
- `backupme/core/cpanel_ingest.py`
  - `discover(drop_root) -> QueueItems`: recursive walk; classify `.tar.gz`/`.tgz` vs plain files; `account` = sanitized first relative subfolder else `cpmove-(\w+)` regex else `default`; skip `.done/`, dotfiles
  - `sanitize_member(name) -> str | None`: reject absolute paths, `..`, backslashes, empty; normalize to `/`
  - `ingest_archive(archive_path, account, backends, config, redundancy, progress_cb=None) -> dict`:
    - idempotency: `archive_sha256` → skip when `op_log` has `cpanel_ingest` success with same key
    - streaming `tarfile.open(mode="r:gz")`; skip non-regular members, symlinks/devices, `exclude_members` globs (fnmatch)
    - per member: extract to `staging_dir/<archive_id>/`, build `file_info = {local_path, source_root: f"cpanel/{account}", relative_path, version_group: f"{account}|{relative_path}", version_seq: next_version_seq(...)}`, call `upload_bundle([file_info], ...)` with `content_text=None`, delete staging file, checkpoint every 25 members, fire `progress_cb(member, done, total)` per member
    - post-checks: indexed count vs archive regular-file count; write generation manifest row `manifest/<archive_sha>.json` (JSON: account, archive, members, version_seqs); `op_log` success/failed
    - raw disposition: `keep_raw > 0` → `upload_file(source_root=f"cpanel-raw/{account}", ...)` to raw backends (`.gz` skip override); archive → `drop_root/.done/<account>/`
  - `ingest_tree(drop_root, ..., progress_cb=None)`: `ThreadPoolExecutor(max_parallel)`, per-archive try/except (continue on failure + `op_log` error), staging budget gate + pacing hooks (implemented in Phase 3; stubs here)
  - `restore_cpanel(account, generation, member_filter, out, retar)`: query `files` by `source_root` + `version_seq`; `downloader.download_file(file_id, version=seq)`; optional re-tar preserving `homedir/...`, `mysql/...` names
  - `estimate_archive(archive_path) -> dict`: member count + uncompressed bytes WITHOUT extracting (tarfile listing only) — powers `--dry-run` and progress totals

## Modified files
- `backupme/core/index.py`: add `next_version_seq(source_root, relative_path)` = `MAX(version_seq)+1` over ALL rows (no `deleted_at` filter); add `hard_delete_file(file_id)` helper (delete chunks + unref blobs + hard delete row)
- `backupme/config.py`: add `IngestConfig` (enabled, drop_root, staging_dir, staging_budget_gb, max_parallel, bandwidth_limit_mbps, keep_raw, raw_backend, account_pattern, exclude_members, max_member_size_gb) + parse in `load_config`
- `backupme/cli/ingest_cmd.py` (new) + register in `cli/__init__.py`:
  - `ingest-cpanel DROP_ROOT [--once|--account X|--dry-run|--watch|--json|--progress]`: one-shot; `--watch` re-scans on interval until Ctrl+C; `--json` emits machine-readable result lines; `--progress` shows per-member progress bar (tqdm if available, fallback to percentage lines); lazy core imports per CLI convention
  - `restore-cpanel ACCOUNT --generation N [--member PATH] [--out DIR] [--retar OUT.tar.gz] [--json]`
  - `ingest-status [--account X] [--json]`: generations per account, staging usage, SQLite size + row counts, Parquet partitions, last checkpoint/purge/export
- `config.yaml.example`: `cpanel_ingest:` section with comments

## Full CLI upgrade (cross-cutting, all commands)
- Shared `--json` flag pattern: new `backupme/cli/output.py` helper — `emit(obj, as_json)`: JSON to stdout when `--json`, else human table via existing `_safe_echo` style. Applied to: `ingest-status`, `restore-cpanel`, `list`, `stats` (renamed fields stable), `db info`, `db oplog`, `backends`
- Progress display: `backupme/cli/progress.py` — thin wrapper: uses `tqdm` when installed, else falls back to `click.echo(f"\r...")` percentage lines; used by `ingest-cpanel --progress`, `restore-cpanel` (multi-file), `convert`, `db snapshot`
- `ingest-cpanel --watch INTERVAL`: loop `discover → ingest_tree` until Ctrl+C; prints per-tick summary line
- `list` command: add `--source` filter, `--json`, `--sort size|mtime|name`, `--long` (adds size/mtime/version columns)
- `stats` command: add cpanel section (accounts, generations, dedup ratio for `cpanel/%` sources), SQLite size + Parquet size when present, `--json`
- `db info`: add files/chunks row counts, Parquet partitions + codec/size, last purge/export timestamps, `--json`
- Consistent exit codes: `0` ok, `1` partial/errors, `2` config/usage error (documented in README CLI section)

## Tests (`tests/test_cpanel_ingest.py` + `tests/test_cli_output.py`)
- fake `cpmove-acct.tar.gz` with `homedir/index.php`, `homedir/config.php`, `mysql/db.sql`, symlink, `../evil` traversal
- gen1 ingest → members indexed, symlink/traversal skipped, op_log success, `content_text` NULL
- gen2 (1 changed file) → donor dedup hits for unchanged (no new blob), `version_seq=2` rows
- checkpoint resume: simulate crash → resume skips completed
- `keep_raw` disposition; `.done/` move; idempotent re-run skips
- `restore-cpanel --retar` produces valid tar.gz with identical member names
- `estimate_archive` returns member count without extraction
- `emit --json` produces parseable JSON for ingest-status/list/stats/db-info; human mode unchanged
- progress callback fires per member with monotonically increasing done counts

## Verification
- `pytest tests/test_cpanel_ingest.py tests/test_cli_output.py` + full suite green
- manual e2e with real cPanel-format archive: near-zero new bytes on second gen
- `backupme ingest-cpanel --dry-run --json` output parses as JSON

---

# Plan 02 — DB Lifecycle (SQLite hot + DuckDB over Zstd Parquet)

## Goal
Keep SQLite proportional to retention window + grace; full history in monthly Zstd-compressed Parquet partitions queried read-only via DuckDB.

## Data flow (locked)
1. SQLite (hot) → live versions only, sole writer (ingest, dedup, retention, op_log, `db_meta.generation`, replicator snapshots/WAL)
2. Daemon monthly + `backupme cold-export` → `parquet/files_YYYY-MM.parquet` (format 2.6, `compression='zstd'`, level 3, dictionary-encoded `source_root`/`relative_path`/`sha256`/`version_group`, `write_statistics=True`, ~128MB row groups)
3. Purge hard-deletes exported + grace-expired rows from SQLite
4. Queries → DuckDB `read_parquet('parquet/files_*.parquet')` (history search, audits, billing); JSONL holds only archived audit

## New files
- `backupme/core/db_lifecycle.py`
  - `hard_purge_versions(grace_days) -> dict`: select soft-deleted `files` with `deleted_at < now - grace_days` AND already cold-exported (watermark in `parquet/_manifest.json`); per row delete `file_chunks`, `unref_blob` per chunk (same protocol as `apply_retention`), hard `DELETE FROM files`; batched commits (1000 rows)
  - `cold_export_to_parquet() -> dict`: rows eligible by `id` watermark; `pyarrow.parquet.write_table(..., compression='zstd', compression_level=3, version='2.6', ...)`; update manifest watermark (`{partition: {max_id, rows, codec, bytes}}`); no-op with warning if `pyarrow` missing
  - `query_history_duckdb(sql) -> list`: read-only `duckdb.connect(read_only)` over `read_parquet`; no-op fallback message if `duckdb` missing
  - `vacuum_if_needed()`: monthly VACUUM (reuse snapshot timing); `prune_oplog()`: weekly prune + append to `jsonl/op_log_YYYY-MM.jsonl`

## Modified files
- `backupme/config.py`: `SchedulerConfig` gains `purge_grace_days=14`, `cold_export_after_days=30`, `parquet_dir="parquet"`, `parquet_compression="zstd"`, `parquet_compression_level=3`
- `backupme/cli/ingest_cmd.py`: add `backupme cold-export [--dry-run]`; `restore-cpanel` cold path falls back to DuckDB-over-Parquet/manifest lookup; `backupme history QUERY` (optional DuckDB SQL over Parquet)
- `requirements.txt`: add optional `pyarrow>=15`, `duckdb>=0.10` (guarded imports)

## Layout
```
backupme/
├── backupme.db (+ -wal/-shm sidecars, automatic)
├── parquet/files_YYYY-MM.parquet (+ _manifest.json)
└── jsonl/op_log_YYYY-MM.jsonl, recovery_events_YYYY-MM.jsonl
# DuckDB: in-process, read-only, no persistent files
```

## Tests (extend `tests/test_cpanel_ingest.py`)
- purge + cold-export sim: soft-deleted rows past grace → exported to Zstd Parquet (assert `compression == 'ZSTD'` via pyarrow metadata) then hard-deleted; refcounts drop; `files` count stays proportional
- DuckDB history test (skip if missing): query exported partition via `read_parquet`, member history resolves

## Verification
- export → purge → row counts verified; history query works; full suite green

---

# Plan 03 — Daemon Automation

## Goal
Daemon tick runs backups → retention → ingest scan → hard-purge → cold export → scheduled gc/verify, all exception-guarded and single-flight.

## Modified files
- `backupme/scheduler.py`
  - `BackupScheduler.__init__` reads `config["scheduler"]` (fixes hardcoded-3600 gap; `interval`, `ingest_interval=300`, `gc_interval_days=7`, `verify_interval_days=1`, `purge_grace_days`, `cold_export_after_days`)
  - `_periodic_loop`: backups → `_run_retention()` → `_run_ingest()` (every `ingest_interval`) → `_run_purge()` (daily) → `_run_cold_export()` (monthly) → scheduled `gc.run_gc(backends, apply=True)` (every `gc_interval_days`) → scheduled `verify.run_integrity_check(recent=N)`
  - `_run_ingest` / `_run_purge` / `_run_cold_export`: guard on `cpanel_ingest.enabled`, single-flight flags (no overlapping ticks), log exceptions like existing tasks
  - staging budget gate + token-bucket pacing live here (enforced in `ingest_tree` loop)

## Config
- `scheduler:` section in `config.yaml.example` documents all cadences

## Verification
- drop archive into watched folder → auto-ingested within `ingest_interval`
- retention prunes after 8 gens → cold export writes partition → purge removes rows → `gc --apply` reclaims orphans
- overlapping ticks never run concurrently (flag test)

---

# Plan 04 — Web GUI (`/ingest` page + Full Dashboard Upgrade)

## Goal
Dedicated Ingest page plus dashboard-wide upgrades: ingest widgets on the main dashboard, live SSE progress for ingest/restore, dedup charts, per-file drill-down, bulk actions, scheduling controls.

## Modified files
- `backupme/web/routes.py` (follow existing route patterns)
  - HTML route `GET /ingest` → `ingest.html`
  - `GET /api/ingest/status`: drop-folder config, queue depth, in-flight archives, staging usage, SQLite size + files/chunks row counts, Parquet partitions (+ codec/size/rows), per-account generation list (members, dedup bytes), last checkpoint/purge/export, `keep_raw`
  - `POST /api/ingest/scan`: immediate drop-folder scan, single-flight guarded; runs in background thread, progress via SSE
  - `POST /api/ingest/pause` / `POST /api/ingest/resume`: toggle daemon ingest task (shared `_ingest_state` dict + lock, same pattern as `_scan_state`)
  - `GET /api/ingest/stream`: SSE endpoint for live ingest progress (mirrors `/api/backup/scan/stream` pattern: state snapshot every 0.5s until completed/idle)
  - `GET /api/ingest/generations?account=X`: per-generation rows (gen N, member count, bytes, created, retained-by-policy flag) — powers drill-down table
  - `GET /api/ingest/history`: DuckDB SQL over Parquet, read-only, row-limited, allow-listed query templates (member history, per-account bytes over time) — no arbitrary SQL from browser
  - `GET /api/stats/ingest-trend?days=30`: per-day ingested bytes/members/archives (from `op_log` `cpanel_ingest` rows) — powers dashboard chart
- `backupme/web/templates/ingest.html` (new):
  - drop-folder status card (path, enabled, last scan, queue depth)
  - DB health card (SQLite size, row counts, Parquet history size + Zstd codec badge, last purge/export)
  - per-account table (account, generations, members, dedup ratio, last ingest) with row click → generation drill-down (generation table via `/api/ingest/generations`, per-generation restore button)
  - queue + in-flight list with live progress bars (SSE via `App.Poller` or EventSource, same as scan-drive modal)
  - staging usage bar (used/budget, pause-extract warning state)
  - trigger scan + pause/resume controls; keep_raw + scheduling summary
- `backupme/web/templates/base.html`: nav entry "Ingest" (Overview section, after Files)
- `backupme/web/templates/dashboard.html` (upgrade):
  - ingest stat card (today's ingested bytes/members, queue depth) linking to `/ingest`
  - ingest trend chart (30-day ingested bytes line chart via `/api/stats/ingest-trend`, Chart.js, same style as activity chart)
  - quick action "Ingest cPanel" → navigates to `/ingest` (or opens scan modal when already there)
- `backupme/web/templates/files.html` (upgrade):
  - source filter preset for `cpanel/*` roots (quick-pick dropdown entry)
  - per-file version timeline already exists; add "restore this generation" button wiring to `restore-cpanel` equivalent API (`POST /api/ingest/restore-generation`)
  - bulk action: "compare generations" for selected files (shows version_seq list side-by-side)
- `backupme/web/templates/settings.html` (upgrade):
  - new accordion section "cPanel Ingest": drop_root, staging_dir, budgets, parallelism, keep_raw, raw_backend, exclude_members, scheduler cadences — inline editing with per-field save (same pattern as existing sections)
  - scheduler section gains ingest_interval / purge / cold-export cadence fields

## API summary (new endpoints)
| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/ingest` | Ingest page |
| GET | `/api/ingest/status` | Full ingest + DB health snapshot |
| POST | `/api/ingest/scan` | Trigger scan (single-flight) |
| GET | `/api/ingest/stream` | SSE live progress |
| POST | `/api/ingest/pause|resume` | Toggle daemon ingest |
| GET | `/api/ingest/generations` | Per-generation drill-down |
| GET | `/api/ingest/history` | Templated DuckDB-over-Parquet queries |
| GET | `/api/stats/ingest-trend` | Daily ingest chart data |
| POST | `/api/ingest/restore-generation` | Restore one account generation to server path |

## Verification
- open `/ingest`: queue, generations, dedup stats, SQLite + Parquet health render
- manual scan triggers ingest with live SSE progress bars; pause/resume toggles daemon task
- history view queries Parquet via DuckDB (templated, row-limited)
- dashboard shows ingest card + trend chart; settings edits persist to config.yaml

---

# Plan 05 — Ops Hardening, Status, Docs

## Goal
Productionize: pacing refinement, `status` ingest summary, notifications, README/MANUAL chapters, full verification.

## Items
- Pacing token bucket refinement; staging budget enforcement tests at scale (simulated)
- `status` command (extend `list_cmd.py status` or `ingest-status`): cpanel ingest summary — generations, dedup ratio for `cpanel/` sources, SQLite size + Parquet size
- Docs:
  - README "cPanel Ingest" section: architecture + config + cPanel-side cron/rsync snippet + restore examples + DB topology/row-economics note (Zstd Parquet + DuckDB)
  - MANUAL chapter incl. the Ingest page
  - `config.yaml.example` comments for all new knobs
- Notifications: ingest failure → existing notifications config path (`op_log` + notification hook if configured)

## cPanel-side setup (documented, not code)
```bash
*/15 * * * *  rsync -a --remove-source-files /backup/cpmove-*.tar.gz backupme-server:/srv/drop/\$USER/
```

## Risks (documented, honest)
- MySQL dumps dedup poorly (daily full dumps differ): stored compressed + versioned; retention limits sprawl
- Staging holds plaintext: encrypted volume/tmpfs, 0700, immediate cleanup
- gzip CPU ~1 core/150MBs; Zstd export level 3 similarly cheap
- 9drive public-writer caveat: pipeline encryption (AES-256-GCM) covers blobs + raw archives
- >5GB members handled by 64MB chunking; `max_member_size` skip for pathological
- tar traversal: sanitized names, per-member rejection

## Verification (full)
1. `pytest tests/test_cpanel_ingest.py` + full suite green
2. manual e2e: real cPanel archive → dedup ratio → second gen near-zero bytes → `--retar` valid → optional test restore
3. crash test: kill mid-ingest → resume, no duplicate blobs
4. daemon test: auto-ingest → retention → export → purge → gc
5. GUI test: `/ingest` renders, scan/pause/resume work, history queries Parquet

---

# Plan 06 — Adding Storage (new backend onboarding + rebalance)

## Goal
Operator can add a new storage backend (e.g. second 9drive pool, new S3 via rclone, extra local disk) and progressively fill it with vault data — new ingest writes land there immediately, existing blobs backfill via rebalance, old backends drain via GC. No re-upload from sources, no downtime.

## How it works (reuse, no new copy engine)
- New writes: `upload_bundle` already targets `backends[:redundancy]` filtered by health/circuit-breaker — a healthy new backend with high `priority` immediately receives new blobs.
- Backfill: `mirror.replicate_to_n(blob_id, target_backends, config)` already downloads one copy and re-uploads to missing backends + records `blob_storage` rows. Rebalance = iterate blobs missing on the new backend, call `replicate_to_n`, batched + resumable.
- Drain: lowering an old backend's `priority` to 0 stops new writes; `gc.run_gc(apply=True)` reclaims remote objects only when unreferenced — safe drain = rebalance first, then remove backend from config, then GC.
- Safety: `check_consistency()` verifies replica hashes match before any backend removal.

## New files
- `backupme/core/rebalance.py`
  - `plan_rebalance(target_backend, source_filter=None) -> dict`: count blobs missing on target (query `blob_storage` anti-join), estimate bytes — powers `--dry-run`
  - `rebalance_to(target_backend, backends, config, limit=None, progress_cb=None) -> dict`: iterate missing blobs oldest-first, `replicate_to_n` per blob, checkpoint every 100 blobs to `rebalance_checkpoint.json` (converter pattern), per-blob try/except (continue on failure), resumable via checkpoint
  - `drain_backend(backend_name, backends, config, progress_cb=None) -> dict`: rebalance all blobs OFF the named backend onto remaining healthy backends (inverse direction), then report unreferenced remote bytes eligible for GC

## Modified files
- `backupme/cli/backend_cmd.py`:
  - `backend add` gains `--type/--name/...` non-interactive flags (for scripts) + `--json` output; existing interactive flow unchanged
  - new `backend onboard NAME [--rebalance] [--dry-run] [--json]`: test connection → show free/total → optionally run `rebalance_to` immediately with progress bar
  - new `backend rebalance NAME [--limit N] [--dry-run] [--json]`: backfill one backend (or all under-replicated blobs with `--all`)
  - new `backend drain NAME [--dry-run] [--json]`: copy everything off a backend, then print GC/removal runbook steps
  - `backend list` / `backends` command: add per-backend stored bytes, blob count, free space, dedup share (`--json`)
- `backupme/web/routes.py`:
  - `POST /api/backends/onboard`: test + optional immediate rebalance (background thread + SSE via existing scan-state pattern)
  - `GET /api/backends/rebalance/stream`: SSE live rebalance progress
  - `POST /api/backends/{name}/rebalance`, `POST /api/backends/{name}/drain`: trigger operations
  - `GET /api/backends/usage`: per-backend stored bytes, blob counts, free/total/quota, ingest bytes in last 24h (for the storage views)
- `backupme/web/templates/backends.html` (upgrade): per-backend usage bars (stored vs quota), "Onboard" wizard button (test → rebalance), per-card Rebalance/Drain buttons with progress modal, ingest-by-backend mini chart
- `backupme/web/templates/ingest.html`: per-backend ingest share (bytes written per backend in last 24h) — shows new backend receiving writes
- `config.yaml.example`: comment block showing multi-backend ingest example (two 9drive pools + local, priorities explained)

## Operator runbook (docs)
1. `backupme backend add` (interactive or flags) → `backend onboard NEW --rebalance` (or GUI Onboard wizard)
2. new ingest writes land on NEW immediately (priority); backfill runs in background, resumable
3. verify: `/api/backends/usage` shows NEW filling; `check_consistency` clean
4. to retire OLD: lower priority → `backend drain OLD` → remove from config → `gc --apply` reclaims remote objects

## Tests (`tests/test_rebalance.py`)
- two local backends; ingest to backend A only; `rebalance_to(B)` copies all blobs; `blob_storage` rows exist for both; checkpoint resume after simulated crash skips completed blobs
- `drain_backend(A)` with B healthy → all blobs present on B; dry-run reports bytes without copying
- `plan_rebalance` counts match anti-join query

## Verification
- onboard → rebalance → usage views show growth on new backend; drain → consistency clean → GC reclaims
- full suite green

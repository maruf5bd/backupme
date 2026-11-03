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

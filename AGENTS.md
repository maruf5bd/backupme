# Memory

## Language (mandatory)
- ALWAYS write in English. This applies to everything an agent produces: chat responses, commit messages, code comments, docstrings, docs (README/MANUAL/config examples), plan/brief/report files, progress ledgers, and subagent dispatch prompts. Never output any other language, in any file or message.

## Project Overview
See @README.md for project overview and @package.json for available npm/pnpm commands for this project.

BackupMe is a Python 3.13 (Windows) deduplicating backup tool: click CLI, FastAPI web dashboard, pluggable storage backends (local, ftp, sftp, smb, ssh, rclone, 9drive), SQLite hot store + monthly Zstd Parquet cold history (DuckDB queries).

## Code Style Guidelines
- Use descriptive variable names
- Follow existing patterns in the codebase
- Extract complex conditions into meaningful boolean variables
- Lazy imports of core modules inside command bodies (CLI keeps startup fast); module-level `log = get_logger("<module>")`
- Tests use the `env`/`backends` fixtures from `tests/conftest.py` (isolated config + DB + local backend per test)

## Architecture Notes
- **Single-writer SQLite** (`backupme/core/database.py`): WAL mode, thread-local connections; every writer module (uploader, ingest, retention, lifecycle) commits explicitly.
- **Dedup pipeline** (`core/uploader.py`): `upload_bundle` batches small files into tar bundles; content dedup via donor lookup by sha256; ref-counted `blobs` table (`dedup.add_ref`/`unref_blob`; `unref_blob` cascades bundle member -> parent).
- **cPanel ingest** (`core/cpanel_ingest.py`): streams cpmove tar.gz members one at a time through `upload_bundle`; `tar.extractfile` only (NEVER `tar.extract`); `sanitize_member` rejects traversal/absolute/backslash names; staging file deleted in `finally`; per-archive op_log idempotency key `cpanel_ingest:<sha256>`; archives move to `drop_root/.done/<account>/` on success.
- **Bundle member blobs** are byte ranges inside their parent bundle (`storage_path` = `bundle@offset:len`). Any blob-copy logic (rebalance/drain) must skip 5-part `bundle/a/b/tar/mem` ids — copying the parent bundle carries members.
- **DB lifecycle** (`core/db_lifecycle.py`): `cold_export_to_parquet` exports `files` rows past an id watermark to `parquet/files_YYYY-MM.parquet` (zstd-3, pyarrow); `hard_purge_versions` hard-deletes rows that are soft-deleted + past grace + below the watermark; `query_history_duckdb(sql, params)` registers a `files` view over `read_parquet`.
- **Daemon** (`scheduler.py`): `_periodic_loop` runs backups -> retention -> ingest (every `scheduler.ingest_interval`) -> purge (daily) -> cold export (every `db_lifecycle.cold_export_after_days`) -> gc -> verify; each tick has a non-blocking single-flight lock and its own exception guard; ingest/purge/export are gated on `cpanel_ingest.enabled`.
- **Rebalance** (`core/rebalance.py`): reuses `mirror.replicate_to_n`; checkpointed to `rebalance_checkpoint.json`; `drain_backend` never deletes (runbook: lower priority -> remove -> `gc --apply`).
- **Web** (`web/routes.py`): all routes registered inside `register_routes(app, config)`; background tasks use module-scoped state dicts + `threading.Lock` + SSE `StreamingResponse` (see `_scan_state`, `_ingest_state`, `_rebalance_state`). Route order matters: specific paths like `/api/backends/usage` must be registered BEFORE `/api/backends/{name}`.
- **Config** (`config.py`): dataclass per YAML section wired via `_filter_fields` (unknown keys warn, never crash). Sections: chunking, compression, encryption, verification, retention, database, notifications, ingest(cpanel_ingest), DBLifecycleConfig(db_lifecycle), SchedulerConfig(scheduler).

## Common Workflows
- Run tests: `/c/Python313/python.exe -m pytest tests/ -q` (pytest.ini adds `-q --basetemp=test_tmp_backend/pytest`)
- Single file: `/c/Python313/python.exe -m pytest tests/test_cpanel_ingest.py -q`
- Git: the shell defines broken `_lc` aliases for `git`/`pytest`/`grep` — always use `command git`; prefer `/usr/bin/grep` and writing command output to a file then reading it.
- Full CLI smoke: `backupme ingest-cpanel <drop_root> --dry-run --json` must exit 0 and parse as JSON.
- Ingest e2e (tests show the pattern): build tar.gz with `homedir/*` + `mysql/*` members, place under `drop/<acct>/`, run `cpanel_ingest.ingest_tree`, assert rows under `source_root='cpanel/<acct>'`, second identical archive is skipped, changed archive dedups.
- Cold storage e2e: `backupme db cold-export` -> `backupme db purge` -> `backupme db history "SELECT ... FROM files ..."`.
- Add a backend and fill it: `backupme backend add` -> `backupme backend onboard NAME --rebalance`; retire: `backupme backend drain NAME`, remove from config, `backupme gc --apply`.

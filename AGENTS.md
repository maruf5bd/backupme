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

## Subagents (mandatory discipline)
- **Fan out work to background subagents; run at most 2 at a time.** The harness rejects the 3rd concurrent agent ("user concurrency limit exceeded") — dispatch 2, and resume/launch the next the moment a slot frees instead of doing the work inline.
- **Exactly one agent may WRITE the database at a time.** Any other concurrent agent must be strictly read-only (queries, reports, file inventory). A copy-only job (e.g. blob consolidation) does not count as a writer.
- Always launch subagents with `run_in_background: true` so the main thread keeps working and can collect results as notifications arrive.
- Every subagent prompt must carry the shell constraints (broken startup script: no parentheses in bash commands, `command git`, write scripts via the Write tool then `cd /j/personal@hostraj.com/backupme && python script.py`), the English-only rule, and an explicit do-not-touch list for in-flight jobs and other agents' output files.

## Known broken (fix first — ignoring is NOT allowed)
- **Gdrive backend reachability — RESOLVED (was misdiagnosed once)**: backend root is `H:\Backupme` (fixed from stale `R:\`). True data loss is exactly **1 bundle (2 blobs, 1,146,184 B) affecting 1 live file** (`id=25554`, Google-Photos takeout jpg, source deleted). An earlier estimate of "51,644 lost rows" was WRONG: those are bundle-MEMBER rows (`storage_path = bundle/...@offset:len`) whose content lives inside parent tars — exact-path matching cannot see them. Lesson: reachability checks must resolve members through their parent bundle tar. The 9,671 parent + member rows are restore metadata — NEVER purge them. Full analysis: `gdrive_lost_impact.txt`.
- **24 corrupt stored files** at old roots `E:\hostraj\backupme` and `H:\New folder\backupme` (zstd frame damage) — refused by hash-checked consolidation; they affect 17 live files (9 sources still exist → re-backup; 8 gone). Do not copy these from old roots without decompress-hash verification.
- **Zero-storage-row blobs**: 1,303 referenced blobs had no physical copy. REPAIRED: 791 files re-backed via the real pipeline (new versions), 3 chunked objects healed on all 4 backends, 104 deduped; refcount drift 0; restore tests 3/3. Remaining 725 rows are unfixable (716 SOURCE_GONE incl. recycle-bin, 9 SOURCE_CHANGED dev files) — await user purge decision (`rep0_report.txt`).
- **4 junk "archives"** (a.zip 484 B, 2FA.7z 192 B, 2020-12-03 tar.gz 1,861 B, bitexchanger_v20.zip 414 B): vault copies intact, content not extractable; all blobs ref_count=1, no other referencing files. Purge plan ready in `junk_archives_impact.txt` — NOTE 2FA.7z is the last copy in existence (source deleted). Awaiting user decision; do not purge silently.
- Rule: never skip, silence, or work around a failing component. If something is broken, fix the root cause first; if genuinely unfixable in this session, record it here with root cause and the concrete unblock condition, then get explicit user approval before proceeding without it.

## Common Workflows
- Run tests: `/c/Python313/python.exe -m pytest tests/ -q` (pytest.ini adds `-q --basetemp=test_tmp_backend/pytest`)
- Single file: `/c/Python313/python.exe -m pytest tests/test_cpanel_ingest.py -q`
- Git: the shell defines broken `_lc` aliases for `git`/`pytest`/`grep` — always use `command git`; prefer `/usr/bin/grep` and writing command output to a file then reading it.
- Full CLI smoke: `backupme ingest-cpanel <drop_root> --dry-run --json` must exit 0 and parse as JSON.
- Ingest e2e (tests show the pattern): build tar.gz with `homedir/*` + `mysql/*` members, place under `drop/<acct>/`, run `cpanel_ingest.ingest_tree`, assert rows under `source_root='cpanel/<acct>'`, second identical archive is skipped, changed archive dedups.
- Cold storage e2e: `backupme db cold-export` -> `backupme db purge` -> `backupme db history "SELECT ... FROM files ..."`.
- Add a backend and fill it: `backupme backend add` -> `backupme backend onboard NAME --rebalance`; retire: `backupme backend drain NAME`, remove from config, `backupme gc --apply`.

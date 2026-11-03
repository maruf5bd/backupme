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

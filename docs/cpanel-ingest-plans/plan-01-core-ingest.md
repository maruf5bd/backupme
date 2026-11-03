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

# Task 6 Brief — CLI upgrades: --json + filters on list/stats/db info/backends

You are implementing Task 6 of the cPanel Ingest pipeline (Plan 01) for BackupMe. Cross-cutting CLI upgrades: progress display, `--json` machine output, richer status everywhere — applies to `list`, `stats`, `db check/info`, `backends list/test`.

## Environment
- Windows, Git Bash. Broken aliases: `command git`; `/c/Python313/python.exe -m pytest ... -q > /tmp/pt.txt 2>&1` then read file.
- TDD: write tests/test_cli_output.py additions (new test functions) FIRST, red, then implement, green. Existing tests: 128? (120 + 8 from Task 5). Run FULL suite at end.
- One commit at end: `command git add backupme/cli/list_cmd.py backupme/cli/db_cmd.py backupme/cli/backend_cmd.py backupme/cli/encrypt_cmd.py tests/test_cli_output.py && command git commit -m "feat(cli): --json + filters on list/stats/db/backends"` — ONLY those four files + tests.

## Existing infrastructure to reuse
- `backupme/cli/output.py`: `emit(data, as_json, human=None)` (Task 3).
- `backupme/cli/progress.py`: `Progress` (Task 3).
- `backupme/core/index.py`: `get_all_files(limit, offset)`, `find_by_sha256`, `file_count`, `find_by_path`, `find_all_files(source_root=None)`.
- `backupme/core/dedup.py`: `total_dedup_savings()`.
- `backupme/core/database.py`: `get_generation`, `check_integrity`, `get_connection`.
- `backupme/core/schema.py`: schema_version rows.
- `backupme/backends`: `get_all_backends`, `get_backend`.

## Changes required

### 1. `backupme/cli/list_cmd.py`

`list` command:
- Add options: `--source-root` (filter), `--limit` (default 50), `--offset` (default 0), `--json`.
- Change signature to `list_files(ctx, source_root, sha256_val, limit, offset, as_json)`.
- Build `files = index_mod.get_all_files(limit=limit, offset=offset)` (or `find_by_sha256` if sha256_val). Filter by source_root if given.
- Render each file as dict: `{id, source_root, relative_path, sha256, size, version_seq, version_group}`.
- `emit({"files": files, "count": len(files)}, as_json, human=...)` where human prints the existing one-line format.

`stats` command:
- Add `--json`.
- Build dict: `{total_files, generation, blobs, raw_bytes, stored_bytes, dedup_savings_bytes, ratio}`.
- `emit(dict, as_json, human=...)`.

### 2. `backupme/cli/db_cmd.py`

`db check`:
- Add `--json`.
- Build dict: `{integrity: "ok"|"failed", errors: [...], refcounts_repaired, inline_pending_or_migrated, unrecoverable_count}`.
- `emit(dict, as_json, human=...)` — human prints the existing lines.

`db restore` / `db rebuild` / `db snapshot`:
- Add `--json` to each; emit the existing result dicts.

### 3. `backupme/cli/backend_cmd.py`

`backends` (top-level list in list_cmd? Actually `backends` command lives in list_cmd.py lines 41-47):
- Add `--json`.
- Build list of dicts `{name, type, status, priority, weight}`.
- emit.

`backend list` (backend_cmd.py):
- Add `--json`.

`backend test`:
- Add `--json` (result: connected bool, free/total space).

### 4. `backupme/cli/encrypt_cmd.py` (if exists) and `report_cmd.py`
- Add `--json` to `encrypt` status and `report integrity` outputs where a dict is naturally returned. (Optional; skip if unclear.)

### 5. `backupme/cli/convert_cmd.py`, `download_cmd.py`
- Add `--json` to `download` and `convert` where a result dict exists. (Optional.)

## Tests — append to tests/test_cli_output.py

New test functions (reuse env/backends fixtures, CliRunner):
1. `list --json` → parseable JSON with "files" key and "count" key.
2. `list --source-root cpanel/acct` → only matching rows (after ingest).
3. `stats --json` → has total_files, generation, ratio.
4. `db check --json` → has integrity key.
5. `backends --json` (top-level) → list with name/type/status.
6. `backend list --json` → parseable.

Run FULL suite at end.

## Report
Write full report to `.superpowers/sdd/task-6-report.md`. Return: status, commit hash, one-line test summary, concerns (max 2 lines).

# Task 1+2 Report — index version helpers + cpanel_ingest config

Status: DONE
Commit: dc1ba0865ca4b3bd01e269415976a24ca5c0ee6b
Plan: cPanel Ingest Plan 01 (Tasks 1+2), brief at `.superpowers/sdd/task-1-2-brief.md`

## What was done

### Task 1 — `backupme/core/index.py`
Added two functions at the end of the module (before nothing; after `file_count_by_source`, just above the existing `delete_file`):

- `next_version_seq(source_root, relative_path) -> int` — `SELECT MAX(version_seq)` over rows matching `source_root`+`relative_path` with **no** `deleted_at` filter (a purged-but-undeleted history must never reuse a seq, per the `UNIQUE(source_root, relative_path, version_seq)` constraint). Returns 1 when no rows exist (`MAX` returns NULL → row[0] is None).
- `hard_delete_file(file_id)` — fetches `blob_id`s from `file_chunks`, deletes `file_chunks` rows, deletes the `files_fts` row by `rowid`, deletes the `files` row, then calls `dedup_mod.unref_blob(blob_id)` for each referenced blob (lazy import of `backupme.core.dedup` inside the function to avoid import cycles, matching the pattern used in `reconcile_refcounts`). Single `commit()` at the end. Refcount semantics are entirely delegated to `dedup.unref_blob` (refcount drop, blob+blob_storage deletion at 0, cascade to parent bundle blobs) — not reimplemented.

### Task 2 — `backupme/config.py` + `config.yaml.example`
- Added `IngestConfig` dataclass with exactly the fields/defaults from the brief (enabled=False, drop_root="", staging_dir="staging", staging_budget_gb=10.0, max_parallel=2, bandwidth_limit_mbps=0.0, keep_raw=0, raw_backend="", account_pattern="", exclude_members=list factory, max_member_size_gb=5.0), placed after `NotificationsConfig` and before `BackendDef`.
- `Config` gained `cpanel_ingest: IngestConfig = field(default_factory=IngestConfig)`.
- `load_config` gained `if "cpanel_ingest" in raw: cfg.cpanel_ingest = IngestConfig(**_filter_fields(IngestConfig, raw["cpanel_ingest"]))` — list fields pass through `_filter_fields` unchanged (same as `BackupDef.exclude`), so `exclude_members` needed no special handling.
- `config.yaml.example` got the `# --- cPanel Ingest ---` section verbatim from the brief, appended after `notifications:`.

### Tests — `tests/test_versioning.py` (new, 7 tests)
Written FIRST (red), then implementation (green):
1. `test_next_version_seq_unknown_path_is_1` — 1 for unknown path.
2. `test_next_version_seq_counts_indexed_rows` — 2 after one `index_file` (via `backup_bytes` → `upload_bundle`).
3. `test_next_version_seq_counts_soft_deleted_rows` — still 2 after `delete_file` soft-deletes the only row.
4. `test_hard_delete_file_removes_rows_and_unrefs_blob` — after `hard_delete_file`: `files` row gone, `file_chunks` gone, blob row + `blob_storage` rows deleted (refcount hit 0 via `unref_blob`).
5. `test_hard_delete_file_only_drops_refcount` — two files sharing content (same bundle member blob); hard-deleting one leaves the blob alive with `ref_count` decremented by 1.
6. `test_ingest_config_parses_all_fields` — YAML with a full `cpanel_ingest:` section; every field parsed with correct types (bool/int/float/list/str).
7. `test_ingest_config_defaults_without_section` — config without the section yields all defaults via `cfg.cpanel_ingest`.

Fixtures: `env` + `backends` from `tests/conftest.py`; `backup_bytes` from `tests/helpers.py`; config tests use `tmp_path` directly (matches `load_config(path)` signature; no DB needed).

## TDD evidence

Red phase (before implementation) — all 7 failed with AttributeError:
```
FAILED tests/test_versioning.py::test_next_version_seq_unknown_path_is_1 - AttributeError: module 'backupme.core.index' has no attribute 'next_version_seq'
FAILED tests/test_versioning.py::test_next_version_seq_counts_indexed_rows - ...
FAILED tests/test_versioning.py::test_next_version_seq_counts_soft_deleted_rows - ...
FAILED tests/test_versioning.py::test_hard_delete_file_removes_rows_and_unrefs_blob - AttributeError: ... 'hard_delete_file'. Did you mean: 'delete_file'?
FAILED tests/test_versioning.py::test_hard_delete_file_only_drops_refcount - ...
FAILED tests/test_versioning.py::test_ingest_config_parses_all_fields - AttributeError: 'Config' object has no attribute 'cpanel_ingest'
FAILED tests/test_versioning.py::test_ingest_config_defaults_without_section
7 F  [100%]
```

Green phase (new file only): `7 passed` (`.......  [100%]`, exit 0).

## Full suite

```
/c/Python313/python.exe -m pytest tests/   (pytest.ini addopts: -q --basetemp=test_tmp_backend/pytest)
EXIT=0
107 passed in 86.32s (0:01:26)
```

107 = 100 baseline + 7 new. No failures, no regressions.

## Files touched

- `J:\personal@hostraj.com\backupme\backupme\core\index.py` (added next_version_seq, hard_delete_file)
- `J:\personal@hostraj.com\backupme\backupme\config.py` (IngestConfig dataclass, Config field, load_config wiring)
- `J:\personal@hostraj.com\backupme\config.yaml.example` (cpanel_ingest section)
- `J:\personal@hostraj.com\backupme\tests\test_versioning.py` (new, 7 tests)

Nothing else modified.

## Deviations / notes

- None functional. Brief's snippets used verbatim.
- `next_version_seq` handles the empty-`MAX` case with an explicit `if row and row[0] is not None` branch (MAX() over zero rows returns one row with NULL, not no rows).
- `hard_delete_file` does NOT delete backend-side data (bundle tarballs / storage objects) — out of scope for Task 1; the ingest plan's GC/generation tasks own that. Blob *refcounting* is fully correct in the DB.
- The commit also picked up `.superpowers/sdd/progress.md` and the brief file via `git add -A` (untracked planning artifacts); harmless, they belong to this plan's workflow.
- Note for later tasks: pytest.ini has `-q` in addopts, so appending `-q` on the command line yields `-qq` and suppresses the summary line; run `pytest tests/` with no extra `-q` to see "N passed".

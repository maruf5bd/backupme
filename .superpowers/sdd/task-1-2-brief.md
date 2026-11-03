# Task 1+2 Brief — index.py helpers + IngestConfig

You are implementing Task 1 and Task 2 of the cPanel Ingest pipeline (Plan 01) for BackupMe, a deduplicating backup tool. These are foundation tasks: later tasks build the ingest engine on these helpers.

## Environment notes
- Windows, Git Bash. **The shell startup defines broken aliases for `git` and `pytest` — ALWAYS use `command git` and `/c/Python313/python.exe -m pytest` (write output to a file, then read the file, to avoid an intermittent `_lc` alias error).**
- Run tests: `/c/Python313/python.exe -m pytest tests/test_versioning.py -q > /tmp/pt.txt 2>&1; cat /tmp/pt.txt`
- Commit with `command git add ... && command git commit ...` (one commit is fine).
- TDD: write the test first, watch it fail, implement, watch it pass.

## Task 1 — `backupme/core/index.py` additions

Add two functions to the existing module (match its style: plain functions, `execute`/`commit` from `backupme.core.database`):

```python
def next_version_seq(source_root: str, relative_path: str) -> int:
    """Next version_seq for a path, over ALL rows (soft-deleted included)."""
    # MAX(version_seq)+1 over rows matching source_root+relative_path,
    # deliberately NO deleted_at filter (a purged-but-undeleted history must
    # never reuse a seq). Return 1 when no rows exist.
```

```python
def hard_delete_file(file_id: int):
    """Hard-delete a file row and its chunk mappings, unreferencing blobs."""
    # 1. fetch blob_ids from file_chunks for this file
    # 2. DELETE FROM file_chunks WHERE file_id = ?
    # 3. DELETE FROM files_fts WHERE rowid = ?
    # 4. dedup_mod.unref_blob(blob_id) for each blob (import dedup lazily inside the function to avoid a circular import — dedup imports database only, but keep the pattern of other modules)
    # 5. DELETE FROM files WHERE id = ?
    # 6. commit() once at the end
```

Note: `files` has `UNIQUE(source_root, relative_path, version_seq)`; `unref_blob` deletes a blob row + its blob_storage rows when refcount hits 0, and cascades to parent bundle blobs — reuse it, do not reimplement refcount logic.

## Task 2 — `backupme/config.py` IngestConfig + `config.yaml.example` section

Add a dataclass (place after `NotificationsConfig`, before `BackendDef`):

```python
@dataclass
class IngestConfig:
    enabled: bool = False
    drop_root: str = ""                  # directory holding cpmove-*.tar.gz
    staging_dir: str = "staging"         # plaintext extraction area
    staging_budget_gb: float = 10.0
    max_parallel: int = 2
    bandwidth_limit_mbps: float = 0.0    # 0 = unlimited (Phase 3 pacing hook)
    keep_raw: int = 0                    # generations of raw archive to keep (0 = none)
    raw_backend: str = ""                # backend name for raw archives ("" = default set)
    account_pattern: str = ""            # optional regex override for account detection
    exclude_members: list[str] = field(default_factory=list)  # fnmatch globs
    max_member_size_gb: float = 5.0      # skip members larger than this
```

Wire it in `load_config`: `if "cpanel_ingest" in raw: cfg.cpanel_ingest = IngestConfig(**_filter_fields(IngestConfig, raw["cpanel_ingest"]))`, and add field `cpanel_ingest: IngestConfig = field(default_factory=IngestConfig)` to `Config`. Add `exclude_members` list handling exactly like the codebase does elsewhere (check how `BackupDef.exclude` is handled via `_filter_fields` — list fields pass through it fine).

Add to `config.yaml.example` at the end (with a comment header matching the file's style):

```yaml
# --- cPanel Ingest ---
# Stream cpmove-*.tar.gz archives member-by-member into the dedup vault.
cpanel_ingest:
  enabled: false
  drop_root: /srv/drop          # where cPanel rsyncs cpmove archives
  staging_dir: staging          # plaintext extraction area (0700, wiped per member)
  staging_budget_gb: 10         # pause ingest when staging exceeds this
  max_parallel: 2               # archives processed concurrently
  bandwidth_limit_mbps: 0       # 0 = unlimited (pacing hook)
  keep_raw: 0                   # keep N generations of raw archives (0 = delete after ingest)
  raw_backend: ""               # backend for raw archives ("" = default backend set)
  account_pattern: ""           # optional regex; default derives account from folder/cpmove-NAME
  exclude_members: []           # glob patterns to skip, e.g. ["*/mail/*/cur/*"]
  max_member_size_gb: 5         # skip members larger than this
```

## Tests — new file `tests/test_versioning.py`

Use the existing `env` fixture (isolated config+db+backend) and `backends` fixture from `tests/conftest.py`. Test:
1. `next_version_seq` returns 1 for unknown path; 2 after one `index_file`; still counts soft-deleted rows (call `index_file` then `delete_file` then `next_version_seq` → 2).
2. `hard_delete_file`: upload a small file via `upload_bundle` (see `tests/helpers.py` `backup_bytes`), capture file_id; assert `file_chunks` rows exist; `hard_delete_file`; assert `files` row gone, `file_chunks` gone, blob refcount dropped (and blob row deleted when it hits 0).
3. Config: write a YAML with a `cpanel_ingest:` section (all fields) to a temp path, `load_config(path)`; assert each field parsed, and that a config without the section yields defaults.

Run the new tests, then the FULL suite (`tests/`) to prove no regression (baseline: 100 pass).

## Report
Write your full report to `.superpowers/sdd/task-1-2-report.md` (what you did, test evidence incl. full-suite line, deviations). Return: status (DONE/DONE_WITH_CONCERNS/NEEDS_CONTEXT/BLOCKED), commit hash, one-line test summary, concerns.

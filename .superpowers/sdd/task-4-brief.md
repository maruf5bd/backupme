# Task 4 Brief — core/cpanel_ingest.py (the ingest engine)

You are implementing Task 4 of the cPanel Ingest pipeline (Plan 01) for BackupMe, a Python deduplicating backup tool. This is the core engine: stream `cpmove-*.tar.gz` cPanel archive members through the existing `upload_bundle` dedup pipeline, per-archive checkpoints, raw disposition, restore, and dry-run estimation.

## Environment
- Windows, Git Bash. Broken aliases: use `command git`; tests via `/c/Python313/python.exe -m pytest tests/... -q > /tmp/pt.txt 2>&1` then read /tmp/pt.txt.
- TDD STRICT: create the fake-archive test fixtures + failing tests FIRST (they can import `backupme.core.cpanel_ingest` which won't exist yet), then implement.
- You may create tests/test_cpanel_ingest.py now with the tests listed below; a later task adds more. Run FULL suite at the end (currently 120 passed + 1 skip).
- One commit: "feat(ingest): cpanel_ingest core engine (discover/stream/checkpoint/restore)"

## Existing infrastructure you MUST reuse (read these first)
- `backupme/core/uploader.py`: `upload_bundle(files, backends, config, redundancy)` — files are dicts `{local_path, source_root, relative_path, version_group, version_seq, sha256(set later), size, mtime}`. Handles dedup (donor lookup by sha256), chunking, content_text=None default. Returns per-file dict with status ok/dedup/error.
- `backupme/core/index.py`: `next_version_seq(source_root, relative_path)` (Task 1, exists — MAX+1 over ALL rows), `index_file(...)` (don't call directly — upload_bundle calls it).
- `backupme/core/op_log.py`: `append(op_type, target, idempotency_key, status='pending') -> dict(row)`, `mark_committed(op_id)`, `mark_failed(op_id, error)`. Idempotency: append with an existing key returns the existing row.
- `backupme/core/downloader.py`: `download_file(file_id, dest_path, backend_name=None, version=None)` returns `{"status": "ok", "path":..., "sha256":...}`.
- `backupme/config.py`: `IngestConfig` dataclass (Task 2, exists): fields enabled, drop_root, staging_dir, staging_budget_gb, max_parallel, bandwidth_limit_mbps, keep_raw, raw_backend, account_pattern, exclude_members (list[str]), max_member_size_gb.
- `backupme/backends/__init__.py`: `get_all_backends(config)`.
- Tests: `tests/conftest.py` fixtures `env` (isolated config+db+cwd, config at `env/config.yaml`, has one local backend `test_local`), `backends`.

## New file: `backupme/core/cpanel_ingest.py`

Use `from backupme.log import get_logger; log = get_logger("cpanel_ingest")`. Lazy-import core siblings inside functions ONLY where needed to avoid cycles (uploader imports index/dedup/etc; cpanel_ingest importing uploader at module top is fine — uploader does top-level imports of database/hasher/etc, none cyclic with this module).

### 1. `discover(drop_root: str) -> list[dict]`
- Recursive `os.walk` of drop_root. Skip: any path whose relative parts include `.done` or start with `.` (dotfiles/dirs), and anything inside a `staging` dir.
- Classify: files ending `.tar.gz` or `.tgz` → `{"path": abs, "account": <acct>, "type": "archive"}`; all other files → `{"path": ..., "account": <acct>, "type": "file"}` (plain files are queued but the engine's `ingest_archive` is not called on them in this phase — ingest_tree only processes archives; list them in results as `skipped_file`).
- Account derivation, in order: (a) if `account_pattern` config regex provided (compile; first group = account, else whole match) applied to the relative path — when it matches; (b) first relative subfolder under drop_root, sanitized (`[A-Za-z0-9._-]` kept, others → `_`, stripped of leading/trailing `_-.`); (c) if the file NAME matches `cpmove-(\w+)\.tar\.(gz|bz2)` style regex → that capture; (d) else `"default"`. Sanitize the account the same way.
- Return list of archive dicts (skip non-archives entirely, but count them for a `files_ignored` tally in ingest_tree).

### 2. `sanitize_member(name: str) -> str | None`
- Reject: absolute paths (startswith `/`, `\`, or drive letter `X:`), any `..` component, backslash-containing paths (normalize first: convert `\` to `/`, then reject if ORIGINAL contained `..` etc.), empty string, names normalizing to `.` or empty.
- Otherwise return the name with `/` separators.

### 3. `estimate_archive(archive_path: str) -> dict`
- `tarfile.open(path, "r:gz")`; iterate members WITHOUT extracting: count regular files (member.isfile()), sum their sizes, count skipped (non-regular). Return `{"members": <regular count>, "bytes": <sum>, "skipped": <non-regular count>}`.

### 4. `ingest_archive(archive_path, account, backends, config, redundancy=1, progress_cb=None) -> dict`
Signature note: `config` here is the FULL config object (dataclass Config or dict — upload_bundle handles both; convert once with `dataclasses.asdict` if needed for your own reads of `cpanel_ingest` via `config.get("cpanel_ingest", {})` after conversion).
- **Idempotency**: compute `archive_sha256 = sha256 of file (hasher_mod.sha256_file)`. `ik = f"cpanel_ingest:{archive_sha256}"`. Check op_log for existing SUCCESS: `SELECT target_json FROM op_log WHERE idempotency_key = ? AND status = 'committed'` — if found, return `{"status": "skipped", "reason": "already ingested", "archive": ..., "account": ...}` WITHOUT re-ingesting. Else `op = op_log.append("cpanel_ingest", target={"archive": archive_path, "account": account, "sha256": archive_sha256}, idempotency_key=ik)`.
- **Stream**: `tarfile.open(mode="r:gz")`. Per member, in archive order:
  - Skip non-regular, symlinks, devices. Skip names failing `sanitize_member`. Skip if fnmatch.any against `ingest_cfg.exclude_members` globs (match against BOTH the sanitized name and its basename). Skip if member.size > `max_member_size_gb * 1024**3`.
  - Extract entry to `staging_dir/<archive_sha[:12]>/<sanitized name>` (parents mkdir; use `tar.extractfile(member)` + manual file write, NOT `tar.extract` — never extract with setuid/perms; write with 0600-equivalent default).
  - Build `file_info`: `source_root=f"cpanel/{account}"`, `relative_path=<sanitized member name>`, `version_group=f"{account}|<sanitized name>"`, `version_seq=index_mod.next_version_seq(source_root, relative_path)`, local_path=staging path.
  - `results = upload_bundle([file_info], backends, config, redundancy)`; take results[0]; count statuses.
  - Delete the staging file immediately after upload (and on exception — try/finally per member).
  - Fires `progress_cb(member_name_sanitized, done_count, total)` after each member (done starts at 1). `total` comes from `estimate_archive` computed once up front (cheap enough: two passes, acceptable — no partial extraction persisted).
  - **Checkpoint**: maintain `done_members: list[dict]` (name, sha256, version_seq, file_id if result has one). Every 25 members AND at end: write checkpoint via `state_mod.save_checkpoint("cpanel_ingest", {"archive": archive_path, "sha256": archive_sha256, "account": account, "members": done_members[-1:], "done": len(done_members), "total": total})` — NOTE: keep checkpoints SMALL (last member + counts), full member manifest goes in the generation manifest at the end.
- **Post**: compare indexed count (rows now in files with source_root=f"cpanel/{account}" created during this run — simply use len(done_members)) vs estimate's `members` regular-file count; warn (log.warning) on mismatch, do not fail.
- Write generation manifest: `manifest/<archive_sha256>.json` under the app working dir (relative Path `manifest/`), content: `{"account", "archive": <abs path>, "archive_sha256", "members": [done_members list], "indexed": len, "estimated": est['members'], "completed_at": iso}` — one row per archive, overwrite on re-success.
- On success: `op_log.mark_committed(op["id"])`; result dict `{"status": "ok", "account", "archive", "members": len, "bytes": est['bytes'], "dupe": <count of results with dedup or status ok with 'dedup' flag>, "errors": <error count>}`.
- On ANY exception mid-archive: `op_log.mark_failed(op["id"], str(e))`, re-raise if caller wants — NO: return `{"status": "failed", "error": str(e), "account", "archive"}` (ingest_tree continues with other archives). Log via log.error with exc_info=True.
- **Raw disposition** (only when status ok): `keep_raw = ingest_cfg.keep_raw`; if > 0: raw account dir `cpanel-raw/<account>` source_root, `relative_path=<archive basename>`, `version_seq=next_version_seq(...)`; if the file would dedup (`.gz` extension → compression already handled by uploader's ext-skip; no special override needed — just call `upload_file(source_root=f"cpanel-raw/{account}", relative_path=basename, local_path=archive_path, backends, config, redundancy)`. obey `raw_backend`: if set, filter `backends` list to those whose `.name == raw_backend` (fall back to all if none match). Note keep_raw semantics for old raw generations (retention handles versioning — no extra pruning here).
- **Move to .done**: after success (and after raw upload if any): mkdir `drop_root/.done/<account>/`, move (shutil.move) the archive there. If a same-name file exists, overwrite it (unlink first).

### 5. `ingest_tree(drop_root, backends, config, redundancy=1, progress_cb=None) -> dict`
- `discovered = discover(drop_root)`. If empty → `{"status": "idle", "archives": 0, ...}`.
- Thread pool: `ThreadPoolExecutor(max_workers=ingest_cfg.max_parallel)`; submit one `ingest_archive` per archive; per-archive try/except already inside ingest_archive returns failed dicts — collect them. (Staging budget gate + bandwidth pacing are Phase 3 — include **stub functions** `check_staging_budget(config) -> bool` returning True, and `pace()` no-op, and CALL check_staging_budget before each submit (skip archives when it returns False, mark result `{"status":"deferred","reason":"staging budget"}`), so Phase 3 fills them in.)
- Return `{"status": "ok"|"partial", "archives": n, "results": [per-archive dicts], "files_ignored": count}`. status partial if any failed/deferred.

### 6. `restore_cpanel(account: str, generation: int, member_filter: str | None = None, out: str = "restore_out", retar: str | None = None, backends=None, config=None) -> dict`
- Query files: `SELECT id, relative_path, version_seq FROM files WHERE source_root = ? AND version_seq = ? AND deleted_at IS NULL` with source_root=f"cpanel/{account}", version_seq=generation. (Generation == version_seq: the plan defines ingest so that gen N of an archive writes version_seq=N per member.)
- Optional member_filter: fnmatch against relative_path.
- Download each via `downloader.download_file(file_id, dest, version=generation)` to `out/<account>/<relative_path>` (parents mkdir). Collect per-member {path, status}.
- If `retar`: write a tar.gz at that path containing the downloaded members under their ORIGINAL member names (relative_path). Return dict includes `retar`: path.
- Return `{"status": "ok", "account", "generation", "members": n, "out": ..., ("retar": ...)}`.

## Tests — `tests/test_cpanel_ingest.py`
Build a helper `make_cpmove(root: Path) -> Path` creating a real tar.gz: `homedir/index.php` ("<?php echo 1;"), `homedir/config.php` ("<?php 1234567890"), `mysql/db.sql` ("INSERT INTO x VALUES (1);"), plus a symlink member `homedir/link` → target, plus a traversal member name `../evil.txt` (tarfile allows constructing via TarInfo with name "../evil.txt"). Drop it at `drop_root/acct/cpmove-acct.tar.gz` (env fixture cwd). Then:
1. `sanitize_member`: rejects `../evil`, `/abs/path`, `C:\Windows`, empty; passes `homedir/index.php`; normalizes backslashes `mysql\db.sql` → `mysql/db.sql`.
2. `discover`: finds the archive with account `acct`; ignores `.done/` contents; dotfile archive skipped.
3. `estimate_archive`: 3 members, byte sum, skipped=1 (symlink) without extraction (assert no staging dir created).
4. gen1 `ingest_tree`: status ok; 3 members indexed under `cpanel/acct`; symlink+traversal not indexed; op_log has committed row with idempotency key `cpanel_ingest:<sha>`; content null (query fts or files.content_text IS NULL); manifest json exists with 3 members; archive MOVED to `.done/acct/`; staging dir cleaned (no leftover files).
5. **gen2 dedup**: place identical archive again (new copy, same name back at drop_root) → skip via idempotency (status skipped). Then place a CHANGED archive (same member names, index.php content differs): ingest → config.php + db.sql must be DEDUP hits (no new blob rows for their content: count blobs before/after — delta equals only new index.php content), version_seq=2 rows exist for all 3, version_seq=1 rows still alive.
6. checkpoint: after ingest, `state_mod.get_last_checkpoint()` type cpanel_ingest with total=3.
7. progress_cb: called 3 times with done 1,2,3 (monotonic).
8. `restore_cpanel` roundtrip: restore gen1 with member_filter `homedir/*` → 2 files restored, contents match originals.
9. `restore_cpanel --retar` equivalent: retar set → tar.gz exists, `tarfile.open` lists exactly the filtered member names.
10. failure path: corrupt archive (write garbage bytes with .tar.gz name) → ingest_tree status partial, op_log has failed row, other archives still processed (place second tiny archive too).
11. keep_raw: set config file cpanel_ingest.keep_raw: 1 (regenerate env config yaml with section — write it in the test via the env root config), ingest → `cpanel-raw/<acct>` file row exists.

## Report
Full report to `.superpowers/sdd/task-4-report.md`. Return: status, commit, one-line test summary, concerns (max 2 lines).

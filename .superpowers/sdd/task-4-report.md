# Task 4 Report — core/cpanel_ingest.py (the ingest engine)

## Status: COMPLETE

## What was built

### `backupme/core/cpanel_ingest.py` (new)
- `discover(drop_root)` — recursive os.walk; skips dotfiles/dot-dirs (incl. `.done`), `staging` dirs, and hidden files. Classifies `.tar.gz`/`.tgz` as archives, everything else as `file` (counted as `files_ignored` by ingest_tree, never passed to ingest_archive). Account derivation in brief's order: (a) `account_pattern` regex (first group or whole match) applied to the relative path, (b) first relative subfolder under drop_root, (c) `cpmove-(\w+)\.tar\.(gz|bz2)` name capture, (d) `"default"` — all sanitized through `[A-Za-z0-9._-]` + strip of `_-.` edges, fallback `default`.
- `sanitize_member(name)` — rejects empty, absolute (`/`, `\`), drive letters (`X:`), any `..` component (checked on the ORIGINAL name with both separators AND post-normalization), names collapsing to `.` or empty. Backslashes normalized to `/` in the accepted result. Returns None on rejection.
- `estimate_archive(path)` — tar pass without extraction. members/bytes count only regular files with SAFE names (unsafe-named regular members can never be ingested so they are excluded from the count; non-regular entries — symlink etc. — count as `skipped`). Returns `{"members", "bytes", "skipped"}`.
- `ingest_archive(archive_path, account, backends, config, redundancy=1, progress_cb=None, drop_root=None)` —
  - Idempotency: `ik = "cpanel_ingest:<archive sha256>"`; already-committed key → `{"status": "skipped", "reason": "already ingested", ...}` with no re-ingest and no move. Otherwise `op_log.append("cpanel_ingest", target={archive, account, sha256}, ik)`.
  - Streaming: `tarfile.open("r:gz")`; skips non-regular/symlink/hardlink/dev members, unsafe names (logged warning), `exclude_members` globs (matched against sanitized full path AND basename), members over `max_member_size_gb`.
  - Extraction: `tar.extractfile(member)` + manual `shutil.copyfileobj` write to `staging_dir/<sha[:12]>/<sanitized name>` — NEVER `tar.extract`, so no setuid/perms application; traversal members can't escape because names are sanitized and joined under the staging root.
  - Per member: builds file_info (`source_root=cpanel/<account>`, `version_group=<account>|<member>`, `version_seq=next_version_seq(...)`), calls `upload_bundle([file_info], ...)` single-member, tallies dupe/error counts, records member row in `done_members` (name, sha256, version_seq, file_id). Staging file deleted in `try/finally` — per member, even on exception.
  - `progress_cb(sanitized_name, done, total)` after every member (done starts at 1); total from up-front estimate.
  - Checkpoints (small: last member + counts only) every 25 members and at the end via `state_mod.save_checkpoint("cpanel_ingest", {...})`.
  - Post: warns (log.warning, no fail) on indexed-vs-estimate mismatch; writes generation manifest `manifest/<archive_sha256>.json` under CWD with full member list, indexed/estimated counts, completed_at.
  - Success: `op_log.mark_committed`; result `{"status": "ok", account, archive, members, bytes, dupe, errors}`. Any exception: `log.error(exc_info=True)`, `op_log.mark_failed`, returns `{"status": "failed", "error", account, archive}` (ingest_tree continues with other archives).
  - Raw disposition (ok only): `keep_raw > 0` → `upload_file(source_root="cpanel-raw/<account>", relative_path=<basename>, ...)` honoring `raw_backend` (falls back to all backends when the named one is absent); best-effort — raw-upload failure is logged but does not fail the ingestion. No extra pruning (retention owns old raw generations).
  - Move to `.done`: `drop_root/.done/<account>/`, overwrite on name collision (unlink first). Drop root is threaded from ingest_tree; fallback `_guess_drop_root` uses config `drop_root` when the archive lives under it, else parent-of-account-dir.
- `ingest_tree(drop_root, backends, config, redundancy=1, progress_cb=None)` — discover, `ThreadPoolExecutor(max_workers=max_parallel)`, one `ingest_archive` per archive, `check_staging_budget(config)` gate before each submit (False → `{"status": "deferred", "reason": "staging budget"}`; both functions are Phase-3 stubs as ordered — `check_staging_budget` returns True, `pace()` no-op). Returns `{"status": ok|partial|idle, archives, results, files_ignored}`; partial if any failed/deferred, ok also when results are skipped (idempotent repeats are success).
- `restore_cpanel(account, generation, member_filter=None, out="restore_out", retar=None, backends=None, config=None)` — queries `files WHERE source_root='cpanel/<account>' AND version_seq=generation AND deleted_at IS NULL`, optional fnmatch filter on relative_path, downloads via `downloader.download_file(file_id, dest, version=generation)` into `out/<account>/<member path>`, optional retar to `retar` path with members under ORIGINAL member names. Returns status/account/generation/members/out/retar.

### `tests/test_cpanel_ingest.py` (new, 16 tests)
In-memory cpmove tar.gz builder (3 members + symlink + `../evil.txt` traversal member via raw TarInfo) + 16 tests covering: sanitize accept/reject matrix; discover (archive found with `acct`, dotfiles/`.done` ignored, plain files classified); estimate without extraction (no staging dir created); end-to-end gen1 ingest_tree (rows under `cpanel/acct`, symlink+traversal absent from index, op_log committed row with exact `cpanel_ingest:<sha>` key, content_text NULL via FTS join, manifest json with 3 members, archive moved to `.done/acct/`, staging has no leftover files, checkpoint total=3); gen2 identical archive skipped by idempotency; gen2 changed archive → 2 dedup hits (`dupe == 2`), blob delta == 2 (bundle + member blobs only for the changed content), version_seq=2 for all 3 while v1 rows stay alive; checkpoint shape; progress_cb monotonic 1,2,3 of total 3; restore roundtrip with `homedir/*` filter (bytes match); retar lists exactly the filtered member names; corrupt-archive failure path (status partial for the run, failed op_log row, good archive still processed and moved, bad one left in place); keep_raw=1 (upload already after success, writes `cpanel-raw/acct/cpmove-acct.tar.gz`) and keep_raw unset → no `cpanel-raw/%` rows.

## TDD evidence
- Red: full module missing run → collection ImportError `cannot import name 'cpanel_ingest'` (logged in this session).
- Amber iterations: 7 failures (estimate counting traversal member; `.done` landing inside account dir due to weak drop-root fallback; test helper writing outside drop tree; blob-delta expectation per single-member upload_bundle calls; skipped-counted-as-partial in ingest_tree status).
- Green: `tests/test_cpanel_ingest.py` → 16 passed.

## Full suite
`/c/Python313/python.exe -m pytest tests/ -q` → **136 passed, 1 skipped** (was 120 passed + 1 skip; +16 new). rc=0.
Note: running with `-o addopts=` (dropping the project's `--basetemp`) breaks 9 unrelated tests (scheduler/incremental cwd assumptions) — artifact of the flag override, not of this change; canonical invocation is green.

## Security review (non-negotiables)
- No `tar.extract*` anywhere — extraction is `extractfile` + manual write only.
- Every member name passes `sanitize_member` before any disk write; traversal/absolute/drive-letter/dot-dot-in-original all rejected (checked on the pre-normalization name too).
- Staging plaintext lifetime is one member; unlinked in finally, including on upload exceptions.
- Restore path also joins sanitized `relative_path` values from our own index; fnmatch filter applied on the stored string.

## Design notes / deviations
- `estimate_archive` excludes unsafe-named regular members from `members` (they will never be indexed) though the brief described skipped as "non-regular" — this is required for the warn-on-mismatch invariant and the brief's own test expectation (3 members, skipped=1 symlink only).
- `ingest_archive` gained an optional `drop_root` kwarg (extra beyond signature; default None keeps the documented signature working) so the `.done` move lands at the drop root, not inside the account dir. `ingest_tree` always supplies it.
- `ingest_tree` treats `skipped` results as ok (partial only for failed/deferred), matching the brief's "status partial if any failed/deferred".
- `pace()` and `check_staging_budget()` are stubs called from ingest_tree as ordered for Phase 3 (though the brief says call check_staging_budget, it does not call pace() — pace is called nowhere yet, ready for Phase 3 wiring).
- Raw upload happens after `mark_committed` and before the `.done` move, so a raw failure never leaves an uncommitted-but-moved archive.

## Files
- J:\personal@hostraj.com\backupme\backupme\core\cpanel_ingest.py (new)
- J:\personal@hostraj.com\backupme\tests\test_cpanel_ingest.py (new)

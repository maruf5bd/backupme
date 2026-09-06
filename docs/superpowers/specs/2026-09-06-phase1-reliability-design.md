# BackupMe — Phase 1: Restore Reliability (Design)

Date: 2026-09-06
Status: Proposed (awaiting user review)
Scope: Core reliability fixes — every file that can be backed up must be restorable and verifiable, and accounting must never corrupt future restores. Performance, config wiring, and logging overhauls are explicitly deferred to phases 2–3.

---

## 1. Context and evidence

An empirical round-trip test (scratch config + scratch DB, `test_data/` → backup → verify → download) and a full read-through of the core modules found:

**Confirmed by execution**
- Files >1 KB stored as chunks/bundles round-trip byte-perfectly (backup, dedup re-run, integrity check, restore all correct).
- Files ≤1 KB ("inline" path) are backed up but **cannot be restored**: `download` crashes with `KeyError: 'path'` (`cli/download_cmd.py:17`) because `downloader.download_file` returns the error dict `{"status": "error", "message": "No chunks found"}` (`downloader.py:108`) for files with no chunk rows. Integrity verification fails for them as well.
- Dedup works (second run: all `dedup=True`, zero new blobs).

**Root causes from code survey (file:line evidence)**
1. **Inline path is lossy and unrestorable** — `uploader.py:476-486` stores ≤1 KB files only as `content_text` via `decode("utf-8", errors="replace")` (lossy for non-UTF-8 bytes), with no blob and no `file_chunks` row; downloader and verifier only implement the chunk path.
2. **Dedup accounting corrupts restores** — `uploader.py:431-461` never bumps `ref_count` when a new file maps to existing chunks, and its inline fallback maps a chunk to `blob_id = file_sha256` which has no `blob_storage` row (permanent restore failure). Retention (`scheduler.py:44-60`) decrements refs without reconciliation, so pruning the original can delete blobs a deduped copy still needs. `uploader.py:380` records the bundle blob once **per backend**, inflating `ref_count`.
3. **Phantom refs after failed uploads** — `uploader.py:500,511,526` record blobs *before* upload success and `index_file` (`uploader.py:488`) runs before upload; a fully failed backend run leaves indexed files with no chunks and phantom blob refs, which poison future dedup of that file. Per-file results omit failures entirely.
4. **Encrypted DR is lossy** — `manifest.py:14-21` `ChunkStorageEntry` lacks `nonce`/`auth_tag`; the rebuild insert (`manifest.py:170-178`) writes `encrypted=1` rows with NULL nonce/tag → undecryptable after DB rebuild. `compress.py:135-146` recompresses encrypted blobs without the key while still marking them encrypted.
5. **`get_connection(path)` ignores `path`** — `database.py:14-23` returns the existing thread-local primary connection regardless of path, so `recover.py:116-129` compares the same generation for every replica and picks arbitrarily; `recover.py:53` overwrites the live DB under open handles.
6. **Snapshot retention never runs** — `snapshot.py:50-51` parses `s.stem.split("-")[2]` = `"1699999999.db"` → `int()` raises → `continue` → snapshots accumulate forever.
7. **Verify defects** — `verify.py:47` leaks a temp dir per checked file; inline files are always counted as failures; `run_verify_restore` (`verify.py:108`) hardcodes 100 samples, ignoring `verification.verify_restore_count`.
8. **Restore fallback can never match** — `downloader.py:64-77` expects bundle member names `bndl/<sha>` but the writer embeds `bndl/objects/ab/cd/<sha>` (`uploader.py:290`); the fallback falls through to returning the whole bundle → hash mismatch. Old-style bundle entries are unrecoverable either way. `downloader.py:48` silently falls back to `backupme.key` in CWD.
9. **Skip rules break on Windows** — `skip.py:78` fnmatches user excludes against backslash paths, so patterns written with `/` never match; any directory named `temp`/`tmp` anywhere is skipped (over-exclusion); the tool's own `backupme.db`/`-wal`/`-shm` are not excluded.
10. **Silent failure handling in touched paths** — manifest writes `except: pass` (`uploader.py:402-403`), per-file results omit errors, `verify` temp leaks.

## 2. Goals / Non-goals

**Goals (phase 1)**
- G1: Every file the system accepts can be restored byte-exactly — including tiny files and non-UTF-8 content.
- G2: Dedup/ref accounting is provably consistent: `ref_count` always equals the number of referencing `file_chunks` rows; failed uploads leave no trace that affects later runs.
- G3: Disaster recovery works for encrypted setups: manifests carry nonce/auth_tag; rebuild restores decryptable state.
- G4: Maintenance paths do what they claim: snapshot pruning prunes, verify samples per config and cleans up, replica selection compares real replica data.
- G5: A pytest suite locks the round-trip in, so phases 2–3 can refactor safely.

**Non-goals (deferred)**
- Incremental scheduling (mtime/size skip), commit batching / `synchronous` tuning, bundler scheduling, upload download-verify strategy → phase 2 (performance).
- Remote blob GC (orphan cleanup on backends), full logging framework, config-option wiring (chunk sizes, compression overrides, per-backup `backend`, retries), web UI changes → phase 3 (trust/config).
- No schema redesign beyond the additions listed in §4.

## 3. Approaches considered

- **A. Surgical fixes, architecture unchanged (chosen).** Keep the inline/bundle/chunk pipeline, thread-local DB layer, and manifest format shape; fix each defect in place; add targeted migrations; build the pytest suite first. Lowest risk, fastest path to G1–G5; leaves the phase-2 performance debt in place deliberately.
- **B. Unify the storage model first** (single content-addressed blob model for all sizes, no inline/bundle special cases, then re-fix on top). Cleanest end state but a large rewrite before any bug is fixed, with its own migration risk on live DBs/backends — poor sequencing for reliability.
- **C. A + refactor of the two worst subsystems** (uploader accounting flow, database connection layer). More durable than A, but doubles the blast radius of phase 1; the same code gets restructured again in phase 2 anyway.

Decision: **A**. B/C refactors fold naturally into phase 2, which touches the same files.

## 4. Detailed design

### P1.1 Replace the inline path with the blob path (uploader.py, index.py, downloader.py, verify.py)
- Delete the ≤1 KB `content_text` inline branch in `upload_bundle`. All files route to: bundle (≤ the 10 MB threshold currently hardcoded in `uploader.py`) or chunked blobs (> 10 MB). A tiny file becomes a bundle member like any other small file; a lone tiny file still produces a valid bundle + manifest + `file_chunks`.
- `index_file` stops writing `content_text` (column remains for schema compatibility; nothing reads it after migration).
- Downloader/verifier need no inline special case — uniform chunk handling.
- **Migration (runs via `db check`, and at daemon/web startup):** for each `files` row with `content_text NOT NULL` and no `file_chunks`: reconstruct bytes as `content_text.encode("utf-8")` (exactly the bytes the system retained), recompute SHA-256, create a single-chunk blob, upload it to the file's backends, insert `blobs`/`blob_storage`/`file_chunks`, set the row's `sha256` to the recomputed value, NULL `content_text`, and flag the row `migrated=1`. If the recomputed hash differs from the originally recorded one (lossy case), log a warning listing the file; the report page shows a "migrated (lossy)" section. Broken legacy rows where `file_chunks` references a nonexistent blob are detected and reported as unrecoverable (re-backup required) — they cannot be repaired from stored data.

### P1.2 Ref-count correctness (uploader.py, dedup.py, scheduler.py, db check)
- Record each blob exactly once per upload operation (not once per backend).
- When a file dedups against existing chunks, bump `ref_count` for every referenced blob within the same transaction as the file's insert.
- Retention `unref` never drives `ref_count` below 0.
- Invariant enforced and repairable: `ref_count == COUNT(file_chunks WHERE blob_id = ?)`. `db check` recomputes and `--repair` fixes drift.

### P1.3 Transactional upload accounting (uploader.py)
- Reorder `upload_bundle`: upload to backends **first**; `blob_storage` rows are written only for backends that actually succeeded. After at least one backend success, a single transaction writes `blobs`, the successful `blob_storage` rows, `file_chunks`, and the file index row. All backends failed → per-file result `{"status": "error", ...}`, nothing indexed, no blob rows. Fewer successes than the requested redundancy → result `{"status": "partial", ...}` with a warning naming the failed backends; the file remains restorable from what succeeded.
- Manifest + pointer write: once per backend (fix the nested-loop duplication), failures logged with backend name and path instead of `except: pass`.
- Per-file results always include a status; the backup CLI prints an `N failed` summary line; the scheduler logs failures.

### P1.4 Encrypted disaster recovery (manifest.py, uploader.py, compress.py)
- `ChunkStorageEntry` gains `nonce` and `auth_tag` (hex). Uploader persists them in manifests; manifests carry `"manifest_version": 2`.
- `rebuild_database_from_manifests` writes nonce/auth_tag for v2 manifests; for v1 manifests it marks those blobs `encrypted=1` with NULL crypto fields and lists them in the rebuild summary as "needs re-upload" rather than silently producing undecryptable rows.
- `recompress_blobs`: when a blob is encrypted and the key is unavailable → skip with a counted warning; when the key is available → decrypt → recompress → re-encrypt with a fresh nonce/tag and update the row accordingly. Remove the blanket `except Exception: continue` in favor of logged per-blob failures. Temp dirs created during recompression are cleaned up.

### P1.5 Database connection layer — minimal fix (database.py, recover.py)
- `get_connection(path)` keeps one connection per (thread, resolved path). Requesting a non-primary path that has no connection raises unless creation is explicitly requested. Primary behavior unchanged.
- `recover.py`: close the primary connection before copying a replica over the DB file, reopen after; WAL bundles replay into distinct temp locations in sequence (not last-wins over one file); replica generation comparison reads each candidate DB through a real per-path connection; temp dirs cleaned up.

### P1.6 Snapshot retention (snapshot.py)
- Parse snapshot names with a regex over `snapshot-<db_id>-<epoch>.db.zst`; prune by age and count per the existing `database.snapshots` config; tolerate unknown names by skipping them with a log line instead of a silent `continue`.

### P1.7 Verification correctness (verify.py)
- Sample count from `verification.verify_restore_count` (fallback to current 100 only if unset).
- Temp dirs via `TemporaryDirectory` (guaranteed cleanup).
- After P1.1, inline files no longer exist; the "failed file" accounting reflects real failures only.

### P1.8 Restore fallback and key handling (downloader.py)
- Remove the never-matching legacy bundle walk fallback; a bundle member that isn't found via the offset index raises a clear error naming the blob and expected member.
- Encryption: use the configured `key_file`; if encryption is required and the key is missing → explicit error (the silent CWD `backupme.key` fallback is removed; a log line explains where the key was expected).

### P1.9 Skip rules (skip.py)
- Normalize path separators (both pattern and candidate) before fnmatch so `/`-written excludes match on Windows.
- Stop skipping every directory named `temp`/`tmp`; skip only the known system-temp roots and what the user excludes.
- Add the tool's own `backupme.db`, `backupme.db-wal`, `backupme.db-shm` to the always-skip set.
- Keep `p.resolve()` (dropping it is a phase-2 performance item), but cache per-directory results where trivial.

### P1.10 Failure surfacing (minimal)
- No new logging framework (phase 3). Touched paths log failures via the existing mechanism and never `except: pass`; backup/restore CLI output ends with a success/failure count; `report integrity` gains the "migrated (lossy)" and "unrecoverable legacy rows" sections.

## 5. Data model changes

- `files`: new nullable `migrated` flag (0/1; migration-set). `content_text` retained but no longer written.
- `manifests`: `"manifest_version": 2`, chunk entries gain `nonce`/`auth_tag`.
- No changes to `blobs`/`blob_storage`/`file_chunks` schemas; ref_count semantics tightened (§4 P1.2).
- Migrations are additive and idempotent; a pre-flight `db check` reports what would change.

## 6. Testing strategy

Pytest (added to the repo, `tests/`), fixtures build a scratch config + scratch DB + local backend under `tmp_path` — the same pattern as the manual round-trip run used for the survey. Tests are written first (red) where practical, then the fix lands (green).

Integration/round-trip: tiny text file; tiny **non-UTF-8 binary** file; ~5 MB file (bundle); ~70 MB file (chunked); directory tree with subdir; restore → byte-compare; verify passes with 0 failures; second backup run dedups (no new blobs).
Accounting: ref_count invariant after dedup, after retention prune of the original (referencing file still restorable), after a simulated all-backend-failure run (nothing indexed, later retry succeeds, no phantom refs).
Encryption: encrypted round-trip; manifest rebuild → restore works; recompress without key skips and reports.
DR/DB: replica selection reads real per-path generations; recover replaces DB and restores cleanly.
Misc: snapshot prune removes aged snapshots and keeps recent; skip excludes match Windows-style user patterns and include a user dir named `tmp`; verify honors `verify_restore_count` and leaves no temp dirs.
Migration: synthetic legacy inline row + legacy broken chunk row → migration produces downloadable (flagged) / reported-unrecoverable results respectively.

## 7. Rollout

1. Test suite lands first; existing bugs demonstrated red.
2. Fixes land as independent commits per area (P1.1 … P1.10), each turning its tests green and keeping the suite green.
3. Migration runs automatically on first `db check` / daemon start; `report integrity` shows migration results.
4. No behavior changes to upload verification strength or scheduling (phase 2).

## 8. Risks

- Migrating live inline rows touches user data: migration is additive (adds blobs), never deletes, and flags rather than guesses when the original hash can't be reproduced.
- Manifest v2 must remain readable by older manifests — v1 manifests keep working; only rebuild of v1 loses crypto fields, now reported instead of silent.
- `get_connection` change touches every module that opens the DB; mitigated by the existing suite plus a dedicated connection-layer test.

# BackupMe — Phase 2: Performance (Design)

Date: 2026-09-06
Status: Proposed (awaiting user review)
Scope: Make backup runs proportional to *change*, not to *size*: incremental runs, no re-download verification, single-pass reads, batch-fed bundling. Reliability invariants from phase 1 (transactional accounting, ref-count integrity) must not regress. Remote GC, logging framework, and the remaining dead-config cleanup stay in phase 3.

---

## 1. Context

Phase 1 made restores correct and accounting transactional. Performance profile after phase 1:

- The scheduler (and CLI `backup`) walks every source and **re-hashes every file on every run** — one full read per unchanged file before the dedup lookup skips the upload. On a large archive with daily daemon cycles this read-everything pass dominates runtime.
- `_do_upload` verifies **every** upload by downloading it back (2× write amplification over the wire). All six backends (local, ftp, sftp, smb, ssh, rclone) implement `hash()` — rclone does it remotely via `hashsum SHA256` — so the re-download is unnecessary on healthy backends.
- `_do_upload` re-computes the hash of the temp file it was just handed, even though the caller computed that exact hash already.
- Files ≤10 MB are read twice in the uploader (hash pass + read for bundling).
- The scheduler calls `upload_file` per file, so every small file becomes its own bundle + manifest + pointer set and its own commit.
- `chunking.chunk_size` (64 MB) is hardcoded in the uploader; the config value does nothing.

**Success bar (agreed with user):** an unchanged re-run reads **zero** file contents; uploads perform exactly **one** read pass per file (≤10 MB) and **never re-download** for verification when `backend.hash()` works.

## 2. Approaches considered

- **A. Index-based incremental + surgical uploader changes (chosen).** Skip decisions come from the existing `files` table; uploader changes are local (hash plumbing, one-pass read, batch feed). No new tables, no schema changes.
- **B. Dedicated scan-state / content-cache tables** — resumable scans and a `(size, mtime) → hash` cache. Rejected: extra state to migrate and invalidate; content dedup already covers the useful edge cases (e.g., after a DR rebuild, a re-hash re-links content without uploads).
- **C. Parallel chunk uploads** (wire `parallel_uploads`) — rejected for this phase: backend thread-safety varies and correctness risk outweighs the win.

## 3. Detailed design

### P2.1 Incremental backup (scheduler + CLI)
- New helper in `scheduler.py`: `collect_changes(source_root: str, excludes: list[str], force: bool) -> tuple[list[Path], int]` — walks the source (same skip rules as today), and for each candidate file decides skip vs backup.
- Per source, preload one map from the index (single query):
  ```sql
  SELECT f.relative_path, f.size, f.mtime,
         EXISTS(SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id) AS has_chunks
  FROM files f
  WHERE f.source_root = ? AND f.deleted_at IS NULL
    AND f.id = (SELECT MAX(g.id) FROM files g
                WHERE g.source_root = f.source_root
                  AND g.relative_path = f.relative_path
                  AND g.deleted_at IS NULL)
  ```
  (latest row per path by id; `has_chunks` guards against phantom legacy rows — a row without chunks is never treated as backed-up.)
- Skip rule: `not force` AND map has the relative path AND `has_chunks` AND stored `size == os.stat().st_size` AND stored `mtime == os.stat().st_mtime` (exact float equality — both values were produced by `os.stat`/`getmtime` on the same platform and stored as REAL).
- Anything else (new file, changed size or mtime, no live row, no chunks) → backup candidate. Content dedup still protects unchanged-content-but-changed-mtime files from re-uploading bytes.
- Applied to both the daemon `_run_backups` loop and the CLI `backup` command; `backup` gains `--force` (re-evaluate everything, skipping only system/temp file rules). Result output gains an `N unchanged (skipped)` line.
- Deleted/moved source files are **not** pruned by the walk (out of scope; retention handles version cleanup).
- Windows quirk honored: relative paths in the index are stored with OS separators from `os.path.relpath`; the walk produces the same separators, so map keys match directly (normalize both sides with `os.path.normpath` for safety).

### P2.2 Verify uploads via backend hash (uploader)
- `_do_upload(backend, local_path, remote_path, expected_sha256: str)` — the hash is passed in by the caller (it already knows the stored-sha of the bytes it wrote); the temp file is never re-read just for hashing.
- Verification order per attempt:
  1. `backend.upload(...)` as today.
  2. Try `remote_hash = backend.hash(remote_path)`. If it returns a hash: compare with `expected_sha256`. Match → success.
  3. If `hash()` raises or returns `None`: fall back to the existing download-back verification **including the 0-byte DriveFS stub check**.
  4. Mismatch via hash: retry (the existing exponential backoff); after the final attempt, one download-back check before declaring failure (guards against backends with unreliable hashing reporting stale hashes).
- Circuit breaker semantics unchanged.

### P2.3 Single-pass reads (uploader)
- Bundle path (≤10 MB): read `data = path.read_bytes()` once; `file_sha256 = hashlib.sha256(data).hexdigest()`; size from the same `os.stat` call used for mtime (one stat, not two). No second read of the file.
- Chunked path (>10 MB): unchanged two-pass (hash pass decides dedup before upload; the chunk pass then reads again). Documented trade-off: dedup must be decided before uploading, and chunked files are the rare, large minority.
- The dedup short-circuit and `prior_chunks` copy path are unchanged (they already avoid uploads).

### P2.4 Batch feeding (scheduler)
- `_run_backups` collects all candidates for a source (post-incremental filter), then calls `upload_bundle(infos, backends, config, redundancy)` in batches of **200 files or 64 MB total size**, whichever comes first — so small files share bundles and the per-batch transaction covers many files.
- Per-file `move` handling and result logging move from per-`upload_file` call to per-result entry inside the batch loop (results already carry `local_path` and `status` per phase 1).
- The CLI `backup` command already passes all files in one `upload_bundle` call — unchanged.

### P2.5 Wiring and micro-fixes
- `chunking.chunk_size` is read from config at `upload_bundle` entry (`config["chunking"]["chunk_size"]`, parsed via the existing `_parse_size_val` semantics; fallback 64 MB). The 10 MB bundle/chunk threshold (`_vault_max`) stays fixed for this phase; `min_size` and `parallel_uploads` remain explicitly unwired — phase 3 will wire or remove them.
- `_do_upload` retry count comes from the `ExponentialBackoff` object (`retry.max_attempts`) instead of the hardcoded `attempt < 4`.
- `skip.py`: only call `p.resolve()` when the path is not already absolute (`os.walk` and the CLI walkers produce absolute paths), removing a syscall per file from the hot path.

## 4. Invariants (must hold)

- Phase 1's accounting: DB rows only after ≥1 successful backend; one transaction per batch/file; `ref_count` invariant intact (`reconcile_refcounts()` still returns 0 after runs).
- Every upload is still verified (hash-based or download fallback) — verification is never skipped, only made cheaper.
- Restore correctness: full round-trip suite (byte-exact, encrypted, chunked) stays green.

## 5. Testing strategy

- **Incremental:** backup → run again with a spy asserting `upload_bundle` receives zero files and no source file is opened; touch mtime only → re-uploaded (dedup path, no new blobs); change size → new version; legacy row without chunks + matching mtime/size → NOT skipped (re-uploaded). `--force` re-offers everything.
- **Hash verification:** stub backend whose `hash()` returns the correct value → assert `download()` is never called during upload-verify; wrong hash → retry then download fallback; `hash()` raising → download fallback; final attempt mismatch → upload reported failed.
- **Single-pass:** count `open()` calls on a source file during a bundle-path backup == 1.
- **Batch feeding:** 300 tiny files via the batch helper → exactly 1 bundle blob, 1 manifest+pointer per file, ref-count invariant holds.
- **Config wiring:** `chunk_size: 1MB` config → a 3 MB file produces 3 chunks.
- **Performance smoke (manual, not CI):** unchanged second run of `test_data` finishes with "N unchanged (skipped)" and near-zero elapsed time; upload of the 12 MB test file performs no download.

## 6. Risks

- mtime equality across platforms (FAT timestamps 2s granularity): skip rule only *skips* on exact match; a borderline file re-hashes and content dedup catches it — worst case is a wasted hash pass, never a missed change.
- Backends whose `hash()` lies (stale CDN/DriveFS views): the final-attempt download-back check catches stale-hash mismatch before declaring success.
- Batch behavior changes bundle layout (fewer, bigger bundles) — restore path is content-addressed and already covered by the round-trip tests.

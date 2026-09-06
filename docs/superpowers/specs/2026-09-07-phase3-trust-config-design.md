# BackupMe — Phase 3: Trust & Config (Design)

Date: 2026-09-07
Status: Approved for implementation (user: "do those")
Scope: Make the system honest about what it does: every config option either works or is gone, failures are logged instead of swallowed, and unreferenced data on backends can be reclaimed. Phases 1–2 invariants (restore correctness, transactional accounting, ref-count integrity) must not regress.

---

## 1. Context

- One unreachable backend (e.g., Google Drive not mounted) crashed `get_all_backends` entirely because `LocalBackend.connect()` propagates its `mkdir` failure. **Fixed as task 1** (backend skipped with a logged warning; regression-tested).
- Config options that silently do nothing: `chunking.min_size` (the 10 MB bundle/chunk threshold is hardcoded), `compression.default_algorithm`/`default_level`/`overrides` (hardcoded zstd levels), `backups[].backend` (scheduler reads a nonexistent `target` key), `backups[].move` (not a `BackupDef` field, so scheduler never sees it), `verification.retries` (uploader hardcodes 4 attempts).
- Config options with no meaningful implementation: `chunking.parallel_uploads` (no parallelism exists; wiring it would require per-backend thread-safety guarantees that do not hold), `verification.fallback_on_fail` (semantics never defined).
- Failure handling: `print(..., file=sys.stderr)` in library code and several `except Exception: pass` sites; nothing is machine-filterable and warnings vanish in daemon mode.
- Storage growth: `unref_blob` removes DB rows but never deletes remote objects, so unreferenced blobs accumulate on backends forever. Nothing today can reclaim them.

## 2. Detailed design

### P3.1 Config wiring (config.py, uploader.py, scheduler.py, compress.py)
- **`chunking.min_size`** becomes the bundle/chunk threshold: files with `size > min_size` are chunked (into `chunk_size` blobs); files `≤ min_size` are bundled. Loader default stays 10 MiB (matching the old hardcoded value). The user's existing config sets 1 MiB — that is now honored (files between 1–10 MB are chunked as single-chunk blobs).
- **`compression.default_algorithm` / `default_level`**: `choose_compression_level(size)` becomes `choose_compression(data_size, config)` — algorithm and level come from config (fallback: old size-based zstd levels when not configured). Compression is skipped when `compression.enabled` is false (currently ignored).
- **`compression.overrides`**: list of `{ext, algorithm, level, skip}` — matched by file extension (case-insensitive) before the default; `skip: true` disables compression for that extension.
- **`backups[].backend`**: scheduler reads `entry.get("backend")` (the real `BackupDef` field) instead of the nonexistent `"target"` key. `target` is accepted as a deprecated alias.
- **`backups[].move`**: added as a `BackupDef` field (`move: bool = False`) so the daemon honors it; the CLI flag is unchanged.
- **`verification.retries`**: `_do_upload` builds `ExponentialBackoff(max_attempts=max(1, retries))` from config (default 4 as today).
- **Removed with tolerance**: `chunking.parallel_uploads` and `verification.fallback_on_fail` are dropped from the dataclasses. The loader ignores unknown keys in all `**kwargs`-constructed dataclasses and logs a warning naming them, so existing YAML files keep working.

### P3.2 Logging (new `backupme/log.py`; touched core modules)
- `backupme/log.py`: `get_logger(name)` wrapper; `setup_logging(verbose: bool)` called from `cli/base.py` — INFO to stderr by default, DEBUG with `--verbose` (added to the CLI group). Library code must never call `basicConfig`.
- All `print(..., file=sys.stderr)` in core modules (`uploader`, `compress`, `snapshot`, `recover`, `scheduler`) become `log.warning/error/info`.
- Remaining silent `except Exception: pass` sites get logged: manifest/pointer write failures, backend cache handling, `verify` cleanup, `dedup.record_blob` conflicts.
- CLI commands keep their human-readable output; library logs go to stderr only.

### P3.3 Remote garbage collection (new `backupme/core/gc.py`; CLI `backupme gc`)
- Purpose: delete remote objects that no `blob_storage` row references anymore (left behind by retention/`unref_blob`, which only cleaned the DB).
- Referenced remote paths = `SELECT DISTINCT storage_path FROM blob_storage` with any `@offset:len` range suffix stripped, PLUS every prefix-parent of bundle-member paths (a member `bundle/<x>/<y>/<sha>/<m>` implies its parent bundle file `bundle/<x>/<y>/<sha>...` variants are referenced via their own rows — both forms already appear as rows, so plain exact matching after suffix-strip is sufficient; verified against the writer layout in tests).
- Walk each backend's `objects/` and `bundle/` trees (`backend.walk(prefix)`). Anything not in the referenced set is an orphan.
- `backupme gc` → dry run by default: prints per-backend orphan counts and total bytes. `backupme gc --apply` → deletes orphans (`backend.delete`) and reports what was removed. Manifests and tree/pointers are never touched.
- Orphan deletion is conservative: only exact path matches are deleted; when in doubt, keep.

## 3. Testing strategy

- Config: `min_size` drives bundle-vs-chunk (2 MiB config → 3 MB file chunked, 1 MB file bundled); compression override by extension (`skip: true` for `.bin` → stored uncompressed); scheduler `backend` key restricts targets; `move` honored via daemon config path; unknown config keys tolerated with warning; `retries` honored (failing stub backend called exactly `retries` times).
- Logging: `caplog` asserts a manifest-write failure and an unreachable backend produce warnings.
- GC: fake storage dir with referenced + orphan object files → dry run lists orphans, deletes nothing; `--apply` deletes exactly the orphans and leaves referenced files, manifests, and pointers intact; bundle-member parent bundle files are never flagged.
- Full suite green; phase-1 round-trip and phase-2 incremental tests unchanged.

## 4. Risks

- GC deleting wrong files: mitigated by exact-path matching, dry-run default, and never touching manifests/tree. The `--apply` run reports every deletion.
- `min_size` behavior change for the existing config (1 MiB): documented here; users can set it back to 10 MiB.
- Removing config fields: tolerated via loader filtering with warnings, so no existing YAML breaks.

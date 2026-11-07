# QA Report — Core Engine (QA agent 3 of 3)

Scope: `backupme/core/` + `backupme/backends/`, black-box through the public
APIs. Date: 2026-11-07. All work in English per AGENTS.md.

Deliverables:
- `tests/qa3_core_qa.py` — 37 new adversarial/integration tests (new file only; no existing test or source file modified).
- `qa3_fullsuite_out.txt` — full-suite baseline.
- `qa3_run_final.txt` — final recorded run of the qa3 file.
- This report.

Summary: **37 scenarios tested, 6 defects recorded (0 P0, 4 P1, 2 P2) + 2 pre-existing
baseline failures, full-suite baseline 416 collected / 413 passed / 2 failed / 1 skipped.**

Severity recount: the one P0-candidate found (recompress member breakage) is scoped
below as P1 after analysis of trigger conditions (manual `backupme recompress` /
dashboard recompress button); it is defect 2 and the highest-impact finding.

---

## 1. Full-suite baseline

Command: `python -m pytest tests/ -q --basetemp=test_tmp_backend/qa3full`
(output: `qa3_fullsuite_out.txt`)

- **416 collected — 413 passed, 2 failed, 1 skipped** (exit 1).
- FAILED `tests/test_verify.py::test_integrity_check_cleans_temp_dirs` — 3 extra
  `tmp*` dirs appeared in the system temp dir during `run_integrity_check`.
  **Solo re-run of `tests/test_verify.py` passes (2/2)**: the baseline failure is
  almost certainly concurrency noise (the other QA agents' pytest sessions create
  `tmp*` mkdtemp dirs in the same system temp dir), not a product defect.
  Recommend a solo full-suite re-run in a quiet window to confirm.
- FAILED `tests/test_web_api.py::test_sweep_inventory_covers_every_readonly_get_route`
  — the GET route `/api/upload/folders` (backupme/web/routes/files.py:456) is not
  in the sweep inventory. Deterministic inventory drift (web area = QA1 scope;
  recorded here because it surfaced in the baseline).

## 2. Defects found (each verified by at least two runs)

All four are pinned in `tests/qa3_core_qa.py` with
`@pytest.mark.xfail(reason="qa defect N", strict=False)` so the suite stays exit 0.

### qa defect 1 — P1: 0-byte files are accepted but can never be restored
- Area: `core/uploader.py` (`_flush_batch`) + `core/downloader.py` (`_extract_from_bundle`).
- Reproduction: `python -m pytest tests/qa3_core_qa.py::test_empty_file_roundtrip`
  (upload an empty file with the real pipeline; upload reports `status=ok`; restore raises).
- Evidence: the uploader stores the empty member as a byte range with length 0
  (`bundle/<tar>@52:0`); the downloader rejects any range with `length <= 0`
  (downloader.py:59) and raises `UnrecoverableChunkError: ... has no valid
  @offset:length range — re-backup this file`. Observed on run 1 (hard failure)
  and xfail on every subsequent run.
- Impact: any empty file (common in real trees: lock files, placeholders) is
  silently unrestorable while the UI/CLI reports a successful backup. No data was
  ever stored that could matter, but restore of such a file errors instead of
  producing a 0-byte file.

### qa defect 2 — P1 (highest impact): recompress_blobs makes every batched file unrestorable
- Area: `core/compress.py` `recompress_blobs` (B12 migration).
- Reproduction:
  `python -m pytest tests/qa3_core_qa.py::test_recompress_bundle_member_restore_breaks`
  (one small file → bundle; run `recompress_blobs(algo="zstd", level=1)`; restore fails)
  and `tests/qa3_core_qa.py::test_recompress_counters_and_bundle_member_survival`
  (counters on a mixed corpus: `recompressed=2, failed=1, skipped_encrypted=0`).
- Evidence: recompressing the parent bundle writes a NEW object at
  `<bundle>.zstd.recompress_<ts>`, inserts a new blob_storage row for the parent,
  then deletes the OLD object. Bundle-member storage rows still reference the old
  path (`<bundle>.zstd@<off>:<len>`) and are never rewritten — every member of
  that bundle now points at a deleted object. Restore then fails with
  `RuntimeError: Failed to download chunk bundle/...` (captured in qa3_run logs;
  member rows additionally always count as `failed` because a `@off:len` path can
  never be downloaded as a file).
- Impact: one `backupme recompress` (CLI) or dashboard recompress click
  permanently breaks restores of **all files whose chunks are bundle members**
  (the default path for every file under `chunking.min_size`), unless the parent
  happens to be recompressed at identical bytes. Mitigated in practice only by
  nobody having run recompression against the live vault — treat as a P0-hazard
  and fix before the feature is ever used.

### qa defect 3 — P1: recompress_blobs is a silent no-op in a fresh CLI process
- Area: `core/compress.py:174` (`get_backend(row["backend_name"], {})`) + `cli/encrypt_cmd.py` `recompress`.
- Reproduction: `python -m pytest tests/qa3_core_qa.py::test_recompress_cold_backend_cache_silent_noop`.
- Evidence: with the backend instance cache cleared (fresh process), the result is
  `{'recompressed': 0, 'total_scanned': 4, 'skipped_encrypted': 0, 'failed': 0}` —
  `get_backend(name, {})` can never resolve a backend (empty config map), every
  row is `continue`-skipped without a failure count, and the CLI prints
  "Recompressed 0 of N blobs". The web route warms the cache explicitly
  (`web/routes/misc.py` "Warm the backend instance cache"), the CLI does not —
  proof the dependency is known but not enforced at the source.

### qa defect 4 — P1: ingest commits an archive whose members ALL failed to upload
- Area: `core/cpanel_ingest.py` (`ingest_archive`, error_count at :392, `mark_committed` at :454).
- Reproduction: `python -m pytest tests/qa3_core_qa.py::test_ingest_all_members_failed_still_marks_committed`
  (cpmove archive ingested against a backend that rejects every upload).
- Evidence: result is `status=ok` (with `errors=N`), the op_log row is marked
  `committed` with idempotency key `cpanel_ingest:<sha256>`, and the archive is
  moved to `drop_root/.done/<acct>/`. A later retry of the same archive is
  skipped by idempotency — the members are never ingested. During any backend
  outage longer than one ingest pass, cPanel archives are consumed and lost with
  a success-looking status.
- Related (passing) test: `test_ingest_staging_cleanup_on_member_upload_failure`
  confirms the plaintext staging file is still removed in `finally` on member
  upload failure — that part is correct.

### qa defect 5 — P2: `backupme db check` writes to the database without `--repair`
- Area: `cli/db_cmd.py:23` → `core/migration.py:16` (`run_migrations` calls
  `dedup_mod.reconcile_refcounts()` unconditionally; reconcile UPDATEs + commits).
- Evidence: live read-only probe `backupme db check --json` returned
  `"refcounts_repaired": 3` — three rows in the LIVE database were rewritten by a
  command documented as a check (the `--repair` flag only gates the inline-file
  migration). Disclosure: this write was triggered by my probe; the write itself
  is the intended reconcile semantics (self-healing refcounts) and benign, but a
  check command must not mutate — and on the 84k-blob live DB every `check` also
  pays the full reconcile write pass.

### qa defect 6 — P2: `hard_purge_versions` `skipped` counter is dead code
- Area: `core/db_lifecycle.py:242-255`.
- Evidence: the candidate query already filters `AND id <= ?` (watermark), so the
  in-loop `if fid > watermark: skipped += 1` branch can never fire — the result
  always reports `skipped: 0`, and rows above the watermark are silently
  unreported rather than accounted. Surfaced by
  `tests/qa3_core_qa.py::test_hard_purge_grace_and_watermark` (a backdated,
  soft-deleted row above the watermark is correctly preserved but not counted).

### Live observation — dashboard unresponsive (P2, environment)
- `http://127.0.0.1:8081`: TCP connect succeeds (something is listening), but
  HTTP never answers — a raw `GET /health HTTP/1.0` on the socket times out, and
  all 18 probed GET endpoints (`/api/status`, `/api/health/data`, `/api/files`,
  `/api/backends`, `/api/daemon/status`, ...) time out at 15 s each. The server
  accepts connections but the request path never completes (likely a blocked
  event loop or DB lock). Not diagnosed further — touching the running dashboard
  is out of scope. Worth investigating: at the time of probing the live CLI was
  responsive (`db check` returned in seconds), so the DB was not locked.

## 3. Coverage map

### New coverage added by tests/qa3_core_qa.py (37 tests, 33 pass + 4 xfail)
- Uploader: bundle batching + member/parent refcount arithmetic; cross-file dedup
  donor refcounts (same content under different names); same-content duplicates
  inside ONE batch (`seen_members` semantics); D9 changed-re-upload consistency
  matrix parametrized over batch / chunked / donor paths (files.sha256 checked
  against DB after EVERY re-upload + restore equality + reconcile==0); empty file
  (xfail, defect 1); multi-chunk file (4 chunks, offsets, chunked flag, standalone
  storage rows); 4-thread concurrent `upload_bundle` (all ok, reconcile 0, every
  file restorable with sha match); redundancy=2 with a failing backend (partial
  semantics, `failed_backends`, no storage rows for the failed backend, restorable);
  all-backends-fail for both batch and chunked paths (error results, zero DB rows);
  vanished source file mid-batch (per-file error, no crash, no ghost rows).
- Downloader: encrypted small + chunked roundtrip (no plaintext bytes on disk,
  every storage row encrypted=1); corrupted stored object (tail-byte flip and
  truncation) → loud RuntimeError, no partial `.part-*` file left at the
  destination; missing blob_storage rows → clear `Failed to download chunk`;
  unknown file id → FileNotFoundError. (Also documented: a single flipped byte
  that decodes to identical member content is correctly NOT an error — the
  content-sha gate judges data, not container bytes.)
- Dedup: `reconcile_refcounts()==0` asserted after nearly every scenario;
  `unref_blob` cascade member→parent (both rows + storage rows gone);
  `find_unrecoverable` on seeded ghost chunk (FK-safe damage seeding);
  `list_orphans` detection and empty-after-clean invariant.
- Retention: keep-N versioning with version_seq 1..4 + backdated created_at;
  soft delete drops chunk rows and unrefs unique blobs to zero (rows deleted),
  survivors restorable, reconcile 0; hard-purge grace semantics (backdated row
  purged, fresh soft-deleted row kept, above-watermark row preserved).
- db_lifecycle: cold-export → watermark advance → hard purge → DuckDB history
  roundtrip (purged row still queryable from the zstd Parquet partition).
- cpanel_ingest: member rejection matrix (traversal, absolute, drive letter,
  smuggled `..`, symlink skipped; backslash name normalized and ingested; nothing
  escapes staging); staging cleanup when member uploads fail; all-members-failed
  committed archive (xfail, defect 4); two-account isolation (mysql + homedir
  members per account). Idempotent skip and changed-archive dedup are already
  pinned by existing `tests/test_cpanel_ingest.py` (test_gen2_*) and were not
  duplicated.
- Crypto: `capabilities()` layout report cross-checked against the actual
  `encrypt_file` byte layout (salt+nonce position, tag-at-end for AEAD);
  encrypt/decrypt roundtrips at 0 B / 1 KB / 5 MB (bytes and file APIs); wrong
  key raises for both APIs, and a wrong configured key makes the downloader fail
  loudly with no partial output.
- Compress: recompress counters on a mixed corpus (parent+member+chunk:
  recompressed=2, failed=1 for the member row); chunk restore survives migration;
  member restore breaks (xfail, defect 2); cold-cache silent no-op (xfail,
  defect 3); encrypted blobs skipped and counted.
- data_health: summary buckets at zero damage; after deleting a storage row,
  `missing_blob_files`/`missing_blobs`/`purge_candidates` grow in agreement with
  reality; drill-down `issues()` and `purge_candidates()` list the damaged file;
  `filter_purge_candidate_ids` exact set; unknown reason raises ValueError.
- Rebalance/replicate/drain: `replicate_to_n` to a second backend then restore
  from either backend explicitly (member fallback semantics included);
  `rebalance_to` copies top-level blobs only (member byte-range rows NOT copied
  as objects, `remaining_blobs==0`), restore intact; drain dry-run changes
  nothing; real drain deletes nothing remotely (`list_files` unchanged),
  `remaining_only_here_blobs==0`, runbook present.
- Scheduler: `_periodic_loop` survives an injected failing stage (no exception
  escapes; loop exits via stop event); single-flight locks are non-blocking
  (held lock → tick returns fast and does NOT run ingest/gc; free lock → runs
  exactly once); health tick emits `data health regression` warning + notify
  hook after seeded damage (first tick baseline silent).

### Existing coverage relied upon (not duplicated)
- `tests/test_cpanel_ingest.py`: e2e ingest, idempotent skip, changed-archive
  dedup, keep_raw, encrypted ingest, oversize/exclude counters, restore filters.
- `tests/test_reupload_consistency.py`: original D9 pins.
- `tests/test_db_lifecycle.py`, `tests/test_rebalance.py`, `tests/test_dedup.py`,
  `tests/test_downloader.py`, `tests/test_scheduler_health_tick.py`: baseline
  coverage of the same modules (my file adds the adversarial layer).

## 4. Live read-only probe results

CLI (all exit 0, via CliRunner against the live config/DB, read-only intent):
`--help`, `health --json`, `list --limit 3 --json`, `stats --json`,
`backends --json`, `backend list --json`, `db info --json`, `db check --json`,
`db oplog --json`. Live health snapshot corroborates the known defect register:
56,644 live files, no_chunks 4,996, missing_blob_files 1,513, missing_blobs 1,300,
integrity ok, unrecoverable 0.
Caveat disclosed above (defect 5): `db check` repaired 3 refcount rows as a side
effect of its unconditional reconcile — a product bug, triggered unintentionally
by this probe.

Dashboard: unresponsive (see live observation above). No server was started,
stopped, or written to.

## 5. Recommendations (priority order)

1. Fix recompress_blobs before anyone uses it: rewrite member storage rows when
   the parent object is renamed, or refuse to migrate bundle parents (defect 2),
   and make it resolve backends from the real config (defect 3).
2. Fix ingest to only mark committed when every member status is ok/dedup —
   otherwise fail the archive and leave it in the drop root (defect 4).
3. Special-case empty members in the downloader (`length == 0` → return b"")
   or in the uploader (store pt_size=0 members without a byte range) (defect 1).
4. Make `db check` pass `repair` down to the reconcile step (defect 5) and fix
   the dead `skipped` accounting in hard_purge (defect 6).
5. Investigate the dashboard event loop (accepts TCP, never answers HTTP).
6. Update `tests/test_web_api.py` sweep inventory for `/api/upload/folders`
   (baseline failure; QA1/web scope).

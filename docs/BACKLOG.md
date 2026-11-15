# Improvement Backlog

Generated 2026-11-05 from a full codebase scan (`diagnostics/codebase_scan.txt`).
Status (2026-11-07): **B01-B16 ALL DONE; D1-D9 all closed.** The only remaining items
are the standing user decisions (gc --apply, purge candidates, junk archives,
password rotation) — see brain/status.md.
Tracks: **[F]** functionality, **[C]** codebase quality, **[W]** wiring.
Priorities: **P0** = fix first (correctness), **P1** = quality, **P2** = features.
Rule from AGENTS.md applies: what is broken gets fixed first; ignoring is not allowed.

## P0 — correctness

- **B01 [C][W] Port the SFTP walk fix to the other backends.** **DONE (2026-11-07, `24146d6`)**
  smb/ssh/ninedrive/agent now expose `walk_errors`, retry transient list failures, and
  log skips; truncation surfaces instead of passing silently.

- **B02 [W] Dead config keys warn on every startup — wire or remove.** **DONE (2026-11-05)**
  `config.yaml` had `chunking.parallel_uploads` and `verification.fallback_on_fail`; the
  dataclasses (`config.py` ChunkingConfig/VerificationConfig) dropped them, so every process
  printed "ignoring unknown option(s)". Resolution: keys removed from config.yaml (zero
  behavior change, verified no warnings on load); committed `config.example.yaml` documents
  every section incl. the nested `verify_restore` shape and all backend types;
  `tests/test_config_example_sync.py` pins example <-> dataclass drift both directions.

- **B03 [W] Destructive endpoints need token + explicit confirm.** **DONE (2026-11-07, `01036ab`)**
  `POST /api/server/restart`, `POST /api/db/restore`, `POST /api/db/rebuild`,
  `DELETE /api/backends/remove/{name}`, snapshots delete/prune are all behind the token gate
  AND a `confirm: true` body flag; all 7 frontend callers updated; missing confirm -> 400
  (re-pinned by tests/test_web_foldins.py).

- **B04 [W] Consolidate the health surface.** **DONE (2026-11-07, `8baea8e`)**
  Canonical `/api/health/data` + `/health` page; old paths kept as thin aliases.

## P1 — codebase quality

- **B05 [C] Split `web/routes.py` (2,700+ lines) into domain routers.** **DONE (2026-11-07, `c909655`)**
  Completed agent 3's unfinished split: all 10 domain modules + common.py wired from
  routes/__init__.py; routes.py monolith and the redundant legacy.py (43 duplicated routes)
  deleted; `reset_background_state()` at registration restores per-app task-state isolation
  (the monolith scoped its dicts inside register_routes). Fold-ins landed: B14 crypto
  capabilities in /api/encrypt/status, B09 op_counters in /api/report, B12 op_log audit in
  POST /api/health/purge (confirm-gate rejections write no row). Tests:
  tests/test_web_foldins.py (4) + 113 web-area tests green. Follow-up queued: /api/recompress
  still uses an inline pre-core implementation (no counters, no op_log audit, silent
  except-continue) — rework onto core recompress_blobs after B07.

- **B06 [C] One background-task runner instead of 8 copies.** **DONE (2026-11-07, `e819600`)**
  All inline op_log + SSE + thread scaffolding in backup/dbops/files/misc/ingest/backends
  switched onto the common helpers (op_log_start/op_log_finish, make_sse_stream,
  start_daemon_thread); nothing needed to stay inline. /api/recompress reworked onto core
  `recompress_blobs` — response now `{status, recompressed, total_scanned, skipped_encrypted,
  failed, converted(=recompressed alias)}`, audited to op_log (feeds /api/report op_counters),
  hash-verified writes, encrypted blobs skipped + counted (the old inline loop silently
  corrupted them and uploaded without verification). "converted" kept for settings.html.
  Tests: tests/test_web_b06.py (3); targeted 100 passed; full suite 394 collected /
  393 passed + 1 pre-existing skip, exit 0.

- **B07 [C] No silent data loss in API responses.** **DONE (2026-11-07, `dc2c2d9`)**
  43 truly-silent except handlers across 9 route modules converted: 40 now log via the
  module logger (op_log bookkeeping failures, trend-query failures, per-blob recompress
  failures, config reads), 3 justified-silent with written reasons (non-numeric
  priority/port form input; staging-file vanishing between walk and stat is the normal
  concurrent-cleanup case). Behavior unchanged; tests/test_web_b07.py (3 caplog tests);
  97 web-area tests + full suite green.

- **B08 [F] Turn the QA harness into a regression suite.** **DONE (2026-11-07, `b9abff1`)**
  `tests/test_web_api.py` runs in the standard suite (route inventory sweep, auth gate,
  pagination, soft-delete semantics, SSE termination, restore round-trip, concurrency
  smoke). It caught the B05 state-leak regressions without any edits when the split
  landed.

- **B09 [C] Count and report skips instead of absorbing them.** **DONE (2026-11-07; core `43d7a8b`, wiring `c909655`)**
  `compress.recompress_blobs` returns `failed`; `manifest.rebuild_database_from_manifests`
  returns `walk_failed_backends` + `unreadable_manifests`; /api/report carries op_counters
  from the op_log audit. Covered by tests/test_skip_counters.py + test_web_foldins.py.

## P2 — functionality

- **B10 [F] CLI for health + verify.** **DONE (2026-11-05)**
  `backupme health [--json] [--purge-candidates] [--limit N]` wiring `core/data_health`;
  `backupme verify [--count N] [--backend NAME] [--file ID]` wiring `core/verify`
  (restore-and-hash, exit 1 on any failure). `cli/__init__.py` registers both;
  tests/test_cli_health.py covers JSON shape, human output, empty-DB check, unknown-id exit.
  Notably, MANUAL's command table and `get_report()` already referenced `backupme verify`
  before it existed. Documented in MANUAL 8.0 + command reference table.

- **B11 [W] Scheduler data-health tick.** **DONE (2026-11-05)**
  `_run_scheduled_verify` now logs the data-health summary every verify tick
  (`health tick: live=... no_chunks=...`) and emits `data health regression: key: old -> new`
  warnings when any tracked bucket (no_chunks_files, missing_blob_files, missing_blobs,
  purge_candidates) grows vs the previous tick. Covered by tests/test_scheduler_health_tick.py.
  NOTE: `notifications.on_failure` could not be wired — see B16.
- **B16 [W] NotificationsConfig is parsed but never consumed — no dispatcher exists.** **DONE (2026-11-05)**
  New `core/notify.py`: `notify_failure(title, detail, config)` always logs a warning and
  best-effort fires a desktop toast (plyer, optional, zero new hard deps) when
  on_failure+desktop are enabled; never raises. Wired at the two real failure points:
  scheduler data-health regressions (`_log_health`) and cPanel ingest archive failures
  (`ingest_archive` failure-recording block). Covered by tests/test_notify.py (7 tests) incl.
  the scheduler-regression path. Documented in MANUAL section 8.

- **B12 [F] Purge approval workflow end-to-end.** **DONE (2026-11-07)**
  Dashboard purge-candidates view (checkbox rows, button disabled until a selection) →
  `App.Modal.confirm` dialog → `POST /api/health/purge {confirm: true, file_ids}` —
  confirm-gate rejections write no op_log row (`c909655` fold-in), every purge is
  audited to op_log with requested/ok/skipped counts, results toast + health refresh.
  A purge is impossible without the dialog and every purge is auditable.

- **B13 [F] Backend rescan as a product feature.** **DONE (2026-11-07; CLI `3d88ab1`, web `d7ec4e1`)**
  `core/backend_scan.rescan_backend` diffs a walk against blob_storage (bundle-member
  rows excluded, parents compared as plain paths) reporting missing_on_box /
  extra_on_box / size_mismatch + missing_bytes; CLI `backupme backend rescan NAME
  [--json]`; Backends page has a per-backend Rescan button (`rescanBackend()` →
  `POST /api/backends/{name}/rescan`, results in a modal). Verified live 2026-11-07 on
  local_disk: 19,819 walked vs 17,146 DB paths, 0 missing, 0 mismatches, 2,673 expected
  extra-on-box (the known orphan-objects bucket).

- **B14 [F] Capability introspection for crypto.** **DONE (2026-11-07; core `43d7a8b`, wiring `c909655`)**
  `crypto.capabilities()` reports cipher, active KDF (argon2id vs pbkdf2 fallback), AEAD
  availability, and the FILE LAYOUT each build writes; served on `/api/encrypt/status`
  (pinned by tests/test_web_foldins.py).

- **B15 [C] Fix AGENTS.md project-overview reference** to `@package.json` npm commands — this is
  a Python project; point to pytest/CLI commands instead.

## QA-found defects (2026-11-05, black-box sweep — `diagnostics/qa_dashboard_report.txt`)

- **D1 [W] P0 — `web.token` from config.yaml is silently ignored (unauthenticated admin API).**
  `config.py load_config()` has no `web` branch, so `WebConfig.token` stays `""`; every `/api/*`
  route serves 200 to tokenless and wrong-token requests. The gate itself is correct when armed
  via `backupme web --token` (verified 401/200 across 21 GETs + writes + malformed headers).
  Fix: wire the `web` section in load_config (same pattern as other sections) + regression test
  asserting a token from config.yaml rejects bad bearers.
  Done when: token set in config.yaml → 401 without/with wrong bearer, 200 with correct one.

- **D2 [F] P0 — health scan false "unreachable" verdicts for compressed bundles.**
  `_health_scan_backend` compares member PLAINTEXT byte ranges (offset+len) against the parent
  bundle's COMPRESSED on-disk size, flagging members `members_unreachable` that `download_file`
  restores perfectly (2/2 proven). Same class of error as the Gdrive misdiagnosis: member
  reachability must be decided against the parent's size AND the member's recorded compressed
  length semantics — decompress-hash is the definitive check for borderline cases.
  Done when: zero false members_unreachable on fixture data with compressed parents; scan result
  cross-checked against restore success.

- **D3 [F] P1 — search 500s on user-typed punctuation.**
  `GET /api/files?q=delta.txt`, `/api/search?q=` and the `/files?q=` page: raw `q` goes into
  sqlite FTS5 `MATCH` → `fts5: syntax error near "."` escapes as 500.
  Fix: escape/quote the query (or fallback to LIKE on FTS error); never 500 on user input.
  Done when: punctuation queries return 200 (or clean 4xx), verified by test.

- **D4 [F] P1 — `/api/browse` cannot descend forward-slash paths.**
  Handler builds a backslash `LIKE 'sub\%'` prefix; all cPanel-ingested rows use forward slashes
  (`sanitize_member`), so `?root=R&path=sub` returns empty dirs/files.
  Done when: browse works for both separator styles; test covers a forward-slash tree.

- **D5 [F] P1 — unknown file id returns 500 instead of 4xx** on `GET /api/download/{id}` and
  `/api/files/{id}/preview`. Map missing rows to 404 with a JSON error body.

## Wiring checks that PASSED (do not re-litigate)

- Route order: `/api/backends/usage` (1256) correctly precedes `/api/backends/{name}` (1277);
  deeper 3-segment routes cannot shadow it.
- Scheduler ticks each have their own exception guard + single-flight lock; DB writers commit
  explicitly (single-writer discipline intact).
- Optional-dep ImportError fallbacks are module-level and intentional (compress, crypto,
  progress, db_lifecycle).
- No TODO/FIXME debt markers in package code.

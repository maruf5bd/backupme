# Improvement Backlog

Generated 2026-11-05 from a full codebase scan (`diagnostics/codebase_scan.txt`).
Status: **B02, B10, B11 DONE**; D1-D5 + B01 in flight; the rest queued.
Tracks: **[F]** functionality, **[C]** codebase quality, **[W]** wiring.
Priorities: **P0** = fix first (correctness), **P1** = quality, **P2** = features.
Rule from AGENTS.md applies: what is broken gets fixed first; ignoring is not allowed.

## P0 — correctness

- **B01 [C][W] Port the SFTP walk fix to the other backends.**
  The silent-walk-truncation bug (fixed in `backends/ftp_sftp.py` 6dcaa73) exists in the
  siblings: `smb.py:115-142` (walk + list swallows errors), `ssh.py:120-227` (6 `except: pass`
  around remote probes), `ninedrive.py:355`, `agent.py:28-44`.
  Done when: every backend `walk()` exposes a `walk_errors` counter, retries transient list
  failures, and logs skips; a test asserts truncation surfaces instead of passing silently.

- **B02 [W] Dead config keys warn on every startup — wire or remove.** **DONE (2026-11-05)**
  `config.yaml` had `chunking.parallel_uploads` and `verification.fallback_on_fail`; the
  dataclasses (`config.py` ChunkingConfig/VerificationConfig) dropped them, so every process
  printed "ignoring unknown option(s)". Resolution: keys removed from config.yaml (zero
  behavior change, verified no warnings on load); committed `config.example.yaml` documents
  every section incl. the nested `verify_restore` shape and all backend types;
  `tests/test_config_example_sync.py` pins example <-> dataclass drift both directions.

- **B03 [W] Destructive endpoints need token + explicit confirm.**
  `POST /api/server/restart` (routes.py:1470), `POST /api/db/restore` (1480),
  `POST /api/db/rebuild` (1488), `DELETE /api/backends/remove/{name}` (426) are state-changing;
  verify each is behind the `web.token` gate AND requires a `confirm` body flag like the new
  purge endpoint. Fold in the QA agent's auth findings.
  Done when: unauthenticated calls 401; missing confirm 400; tests cover all of them.

- **B04 [W] Consolidate the health surface.**
  A `GET /health` (routes.py:1219) and `GET /api/health/detailed` (1311) already exist while the
  new data-health module adds `/api/health/data|issues|purge`. Make one canonical `/health` page
  and one summary endpoint; keep old paths as thin aliases marked deprecated in comments.
  Done when: single nav entry, no overlapping/duplicated health JSON shapes.

## P1 — codebase quality

- **B05 [C] Split `web/routes.py` (2,727 lines) into domain routers.**
  Group by files / backends / db / ingest / backup / scheduler / health / config, keeping the
  current specific-before-generic route ORDER (regression: `/api/backends/usage` before
  `/api/backends/{name}`).
  Done when: routes.py is a thin registrar (<400 lines), full suite + QA suite green.

- **B06 [C] One background-task runner instead of 8 copies.**
  The `op_id = None` + try/except + module-state-dict + SSE scaffolding is copy-pasted across
  scan/ingest/rebalance/restore/backup routes. Extract a helper (start/status/stream) and reuse.
  Done when: helpers used everywhere; one new bg task costs ~20 lines.

- **B07 [C] No silent data loss in API responses.**
  ~30 `except Exception: pass` sites in routes.py return partial data with no signal
  (e.g. 207, 342, 978, 1265, 1335, 1363). Every partial response must include an `errors` field
  and log the exception.
  Done when: grep finds no bare `except.*pass` in routes.py; QA harness asserts error visibility.

- **B08 [F] Turn the QA harness into a regression suite.**
  `tests/test_web_api.py` from the QA agent's TestClient harness: auth gate, pagination,
  route-order, soft-delete semantics, SSE termination, concurrency smoke.
  Done when: suite runs in CI/local run_pytest and catches route regressions.

- **B09 [C] Count and report skips instead of absorbing them.**
  `compress.py:210-231` recompress loop and `manifest.py:98-135` corrupt-manifest skips are
  invisible. Add per-run counters, log them, and surface in `/api/report`.
  Done when: a recompress/manifest skip shows up in the report endpoint and logs.

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
- **B16 [W] NotificationsConfig is parsed but never consumed — no dispatcher exists.**
  `notifications.on_failure`/`desktop` have no reader anywhere in the package (grep confirms);
  scheduler "notifications" are currently log lines only. Decide the minimal honest version:
  a dispatcher module (log + optional desktop toast) consumed by scheduler ticks and ingest
  failure hooks, or drop the section from config/example and document logging-only.
  Done when: a failure actually reaches the user through the configured channel, or the
  section is gone with a documented alternative.

- **B12 [F] Purge approval workflow end-to-end.**
  Dashboard purge-candidates view → confirm dialog → `POST /api/health/purge`, plus an
  `op_log` entry per purge for audit.
  Done when: a purge is impossible without the dialog and every purge is auditable.

- **B13 [F] Backend rescan as a product feature.** **CLI DONE (2026-11-05); dashboard button queued**
  `core/backend_scan.rescan_backend(backend)` diffs a walk against blob_storage (correct
  `backend_name` column; bundle-member rows `@offset:len` excluded, parents compared as plain
  paths) reporting missing_on_box / extra_on_box / **size_mismatch** (cheap corruption signal)
  + missing_bytes. CLI: `backupme backend rescan NAME [--json]` (exit 1 on missing/mismatch).
  Covered by tests/test_backend_rescan.py. Remaining: dashboard button once routes.py frees up.

- **B14 [F] Capability introspection for crypto.**
  `core/crypto.py` has 5 optional-dependency fallbacks; `/api/encrypt/status` should report
  which algorithm is actually active and which fell back.
  Done when: status shows active vs fallback explicitly.

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

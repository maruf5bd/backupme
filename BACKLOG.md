# Improvement Backlog

Generated 2026-11-05 from a full codebase scan (`diagnostics/codebase_scan.txt`).
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

- **B02 [W] Dead config keys warn on every startup — wire or remove.**
  `config.yaml` has `chunking.parallel_uploads` and `verification.fallback_on_fail`; the
  dataclasses (`config.py` ChunkingConfig/VerificationConfig) dropped them, so every process
  prints "ignoring unknown option(s)". Decide per key: re-implement (parallel uploads is a real
  perf feature) or delete from config + document in config example.
  Done when: zero startup warnings; example config matches dataclasses exactly (test compares).

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

- **B10 [F] CLI for health + verify.**
  `backupme health [--json]` wiring `core/data_health.summary()`; `backupme verify [--apply]`
  wiring `core/verify.run_verify_restore()` — both exist in core but have no CLI entry
  (cli/ has no verify/health command module).
  Done when: both commands work and are documented in MANUAL.

- **B11 [W] Scheduler data-health tick.**
  Fold a data-health summary line into the existing verify tick (`scheduler.py` `_periodic_loop`)
  and fire the `notifications.on_failure` hook when regressions appear (new orphan chunks,
  walk_errors > 0).
  Done when: daemon log shows the summary daily; a regression notifies.

- **B12 [F] Purge approval workflow end-to-end.**
  Dashboard purge-candidates view → confirm dialog → `POST /api/health/purge`, plus an
  `op_log` entry per purge for audit.
  Done when: a purge is impossible without the dialog and every purge is auditable.

- **B13 [F] Backend rescan as a product feature.**
  Generalize `storagebox_rescan.py` (walk vs DB diff: missing-on-box / on-box-not-in-DB) into a
  dashboard button + CLI (`backend rescan NAME`), reusing the fixed walk.
  Done when: any backend can be diffed against the DB from the UI.

- **B14 [F] Capability introspection for crypto.**
  `core/crypto.py` has 5 optional-dependency fallbacks; `/api/encrypt/status` should report
  which algorithm is actually active and which fell back.
  Done when: status shows active vs fallback explicitly.

- **B15 [C] Fix AGENTS.md project-overview reference** to `@package.json` npm commands — this is
  a Python project; point to pytest/CLI commands instead.

## Wiring checks that PASSED (do not re-litigate)

- Route order: `/api/backends/usage` (1256) correctly precedes `/api/backends/{name}` (1277);
  deeper 3-segment routes cannot shadow it.
- Scheduler ticks each have their own exception guard + single-flight lock; DB writers commit
  explicitly (single-writer discipline intact).
- Optional-dep ImportError fallbacks are module-level and intentional (compress, crypto,
  progress, db_lifecycle).
- No TODO/FIXME debt markers in package code.

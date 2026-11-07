# QA Report — Web Dashboard and HTTP APIs (QA agent 1 of 3)

Scope: every page and endpoint registered by `backupme/web/routes/*` (10 modules,
106 routes — see `qa1_routes.txt`), tested black-box via FastAPI TestClient in
isolated fixture envs (`tests/qa1_web_sweep.py`) plus read-only GET probes of the
live dashboard at `http://127.0.0.1:8081` (`qa1_live_results.txt`).

Deliverables:
- `tests/qa1_web_sweep.py` — 132 tests, **0 failed, 0 errored, 9 xfailed**
  (each xfail documents a real defect below, `strict=False`, reason carries the
  defect id). Four consecutive full runs green.
- `qa1_routes.txt` — full method+path inventory (106 routes) built from a live
  `create_app()` instance.
- `qa1_live_results.txt` — live GET-only probe log (two passes).

Defect numbering continues the repo's existing qa-defect register (D2–D6 already
used in code comments).

## Summary

- **132 endpoint/page tests executed; 0 failures; 9 defects documented via xfail.**
- **13 findings: 0 P0 / 5 P1 / 8 P2.**
- The destructive-surface story is solid: all 7 confirm-gated endpoints reject
  without `confirm: true` (400, exact-boolean check), the auth gate rejects all
  `/api/*` without/with-wrong bearer (401) and lets pages stay public (documented
  loopback mitigation), and unauthenticated destructive calls never reach handlers.

## P0 — none found

No data corruption, no wrong-data destruction, no crash loops. The worst issues
below are correctness/availability degradations.

## P1 — degraded or wrong data

### D7 — `/api/restore/all` subfolder filter silently restores nothing for forward-slash rows
- File: `backupme/web/routes/files.py` `_select_restore_rows` — matches
  `rel.startswith(path_filter + os.sep)` (and a hardcoded backslash variant);
  on Windows `os.sep == "\\"` so both checks are identical, and cPanel-ingested
  rows (`docs/alpha.txt`) never match `path_filter="docs"`.
- Repro: seed rows `docs/alpha.txt`, `docs/beta.txt` (forward slashes) under
  `source_root="QA1"`; `POST /api/restore/all {"source_root": "QA1",
  "dest_path": ..., "path": "docs"}` → `{"restoring": 0}` (expected 2).
- Evidence: `tests/qa1_web_sweep.py::test_restore_all_subfolder_filter` (xfail
  `qa defect D7`). Verified twice: code read + observed response.

### D8 — restore worker escapes the destination for `/abs/x`-style poisoned rows on Windows
- File: `backupme/web/routes/files.py` `_restore_worker` — the guard tests
  `p.is_absolute()`, which is **False** on Windows for drive-less absolute paths
  (`/abs/evil3.txt`), and `Path(dest) / "/abs/evil3.txt"` resolves to
  `<drive>:\abs\evil3.txt` — outside the destination.
- Repro: insert row `relative_path="/abs/evil3.txt"`, restore its source root →
  the worker created `J:\abs\` (empty dir, download then failed) outside the
  destination. Test cleaned the stray directory afterwards.
- Evidence: `tests/qa1_web_sweep.py::test_restore_all_guard_covers_posix_absolute_paths`
  (xfail `qa defect D8`). Verified twice: code read + observed stray dir.
  Defense-in-depth fix: reject `p.parts[0] == p.root` / paths whose anchor is
  not a prefix of `dest.anchor`, or reuse `_safe_backend_join`-style filtering.

### D9 — restore worker counts un-restorable rows as restored (success)
- File: `backupme/web/routes/files.py` `_restore_worker` — calls
  `downloader_mod.download_file(...)` and ignores the returned dict;
  `download_file` returns `{"status": "error", "message": "No chunks found"}`
  **without raising** for chunk-less rows (`backupme/core/downloader.py`),
  so the worker does `done += 1` with `errors == 0`.
- Repro: restore a source root containing a chunk-less row → final status
  `{"done": 1, "errors": 0}`; the UI/SSE reports a successful restore of a file
  that was never written.
- Evidence: `tests/qa1_web_sweep.py::test_restore_worker_counts_broken_rows_as_errors`
  (xfail `qa defect D9`); observed `errors=2, done=3` for 3 poisoned rows in
  the guard test. Verified twice.

### D13 — op_log records failed uploads as `completed`/`success`
- File: `backupme/web/routes/files.py` `api_upload_file` — after
  `uploader_mod.upload_file` returns an error dict (no exception; e.g. all
  backends failed), the route still writes
  `op_log_finish(op_id, "completed", target_json={"result": "success", ...})`.
- Repro: upload with `backend=no_such_backend` → HTTP 200 with
  `{"status":"error","error":"all backends failed"}` while `/api/logs?op=upload`
  shows the row `status=completed`, `target_json` `result=success`.
- Evidence: `tests/qa1_web_sweep.py::test_upload_failure_is_logged_as_failed`
  (xfail `qa defect D13`) + probe (`qa1_upload_probe.py`). Verified twice.
  The /logs page and /api/report op_counters therefore overstate success.

### D18 (live) — one slow page stalls the whole dashboard: blocking SQLite/FS work inside async handlers
- Every route handler is `async def` but runs blocking SQLite and filesystem
  work inline on the event loop (e.g. `/database` runs `PRAGMA integrity_check`
  on the live DB; heavy pages run multi-query scans). One slow request
  serializes every other request behind it.
- Live evidence (pass 1, `qa1_live_results.txt`): 8 of 12 GETs timed out at
  30s (including `GET /` and `GET /api/status`) while the server was clearly
  alive — `GET /database` answered in **29.05s** (integrity check), other pages
  9–21s each, served strictly one-at-a-time.
- Expected: fast handlers, slow work offloaded (like `api_file_verify` already
  does with `asyncio.to_thread`), or at minimum an integrity-check timeout.
- Severity P1 because the dashboard (and its SSE feeds, which share the loop)
  become unusable while any heavy page runs.

## P2 — polish / unclear contract

### D10 — `/api/download/{id}` serves the wrong filename
- `filename` comes from `Path(result["path"]).name` where path is the server
  temp file `dl_{id}` → `Content-Disposition: attachment; filename="dl_8"`
  instead of the original basename (`delta.txt`). Every browser download gets a
  meaningless name. Evidence: xfail test `test_download_serves_original_filename`.

### D11 — `/api/download/{id}/to-path` answers input errors with HTTP 200
- Missing `path` → 200 `{"status":"error","message":"Destination path required"}`;
  unknown id → 200 `{"status":"error","message":"File ... not found"}` (the
  downloader raises, the handler swallows it). Every sibling endpoint uses
  400/404. Evidence: xfail `test_download_to_path_input_errors_use_error_status_codes`.

### D12 — unknown backend name on upload: HTTP 200 + server path leak
- `POST /api/upload/file` with `backend=no_such_backend` → 200
  `{"status":"error","error":"all backends failed","local_path":"C:\\...\\tmpXXXX.txt"}`.
  Should be 400; the response also echoes the server-side temp `local_path`
  (information disclosure, loopback-only so P2). Evidence: xfail
  `test_upload_unknown_backend_name_is_400` + probe.

### D14 — restore worker leaks its thread-local SQLite connection
- `_restore_worker` has no `db.close_connection()` in its `finally`, unlike the
  ingest worker (`routes/ingest.py` documents the exact WinError-32 teardown
  problem) and the rebalance worker. Consequence seen repeatedly in this sweep:
  after restore tests, the next fresh-DB init hits `sqlite3.OperationalError:
  database is locked` intermittently (run2/run5), and on the live server each
  restore leaves a connection handle until thread teardown/GC.
- Evidence: run logs (`qa1_run2.txt`, `qa1_run5.txt`) + code diff vs ingest
  worker. (The sweep works around it with `gc.collect()` finalizers — see
  `_release_stray_db_locks` in the test file.)

### D15 — `/api/files` has no hash search mode (page-only parity gap)
- `GET /api/files?q=<sha256>` → `[]` (search covers relative_path/content_text
  only), while the `/files` page supports `mode=hash` (exact sha256 + prefix).
  Search-by-hash from scripts is impossible. Evidence: xfail
  `test_api_files_finds_by_exact_sha256`.

### D16 — `/api/report/generate` persists always-zero counters in op_log
- `op_log_finish(..., target_json={"checked_files": result.get("checked_files", 0),
  "failed_files": result.get("failed_files", 0)})` — but
  `verify.run_integrity_check` returns keys `checked`/`failed`, so the audit row
  always shows 0/0. The response body itself is correct. Evidence: xfail
  `test_report_generate_op_log_counters_are_real`.

### D17 — content search can never match anything new
- `content_text` is written as NULL by every pipeline path (uploader →
  `index.add_file(content_text=None)`; only migration code ever set it, and it
  nulls it after migrating inline content to blobs). `GET /api/search?...&scope=content`
  and `/files?mode=content` are dead surfaces for fresh data (verified: search
  for the literal content of a freshly uploaded text file returns []). Either
  remove the mode or document it as legacy. Evidence: sweep +
  `qa1_ct3.txt` grep inventory.

### SSE observation (P2, design) — op streams never terminate before the first op
- `make_sse_stream` terminates when `completed and not running`; the four state
  dicts start with `completed=False`, so `GET /api/restore/stream`,
  `/api/ingest/stream`, `/api/backup/scan/stream`,
  `/api/backends/rebalance/stream` on a fresh app emit one snapshot and then
  loop forever every 0.5s. Harmless for live dashboards (open feed), but any
  scripted consumer that waits for EOF hangs. The sweep proves termination
  AFTER an op ran (4 stream tests green).

## What works (verified green)

- **Pages**: all 9 HTML pages render 200 with real data (`/` shows the latest
  uploads; `/files` search modes; `/logs` filters; `/database` lists snapshots).
- **Auth gate** (isolated app with `web.token`): every `/api/*` GET+POST+DELETE
  → 401 `{"detail":"unauthorized"}` without/wrong/lowercase-scheme bearer; 200
  with the correct bearer on 12 sampled endpoints; pages stay public by design;
  destructive calls with `confirm:true` still 401 unauthenticated (middleware
  runs first).
- **Confirm gates**: 7 destructive endpoints (`DELETE /api/backends/remove/{name}`,
  `POST /api/db/restore`, `/api/db/rebuild`, `DELETE /api/db/snapshots/{name}`,
  `/api/db/snapshots/prune`, `POST /api/server/restart`, `/api/health/purge`)
  reject without `confirm: true` — including `confirm: "true"` string — with
  400 `{"error":"confirm must be exactly true"}`. Safe positive-path executions
  (snapshot delete, prune, purge candidate) verified in isolated envs only;
  `/api/server/restart` was never confirmed anywhere.
- **Search hardening**: punctuation/hostile queries (`a"b`, `((`, `delta.txt`,
  `; DROP TABLE files; --`, `x*y?z`) never 500 on `/api/files`, `/api/search`,
  `/files` (D3 fix present: FTS phrase quoting + LIKE fallback).
- **Browse/tree**: forward-slash (cPanel-style) and backslash (Windows-style)
  rows both descend correctly at every level, trailing separators tolerated
  (D4 fix present); `/api/tree` aggregates per-folder counts/sizes.
- **Files API**: pagination windows disjoint + stable order; backend filter
  narrows to chunk-backed rows and composes with `q`; file detail/versions/
  replication/verify all 200 with correct shapes; unknown ids 404 with JSON
  (D5 fix present); soft-deleted rows disappear from listings and downloads.
- **Upload flow**: round-trip through the real pipeline; `dest_folder` rejects
  `../evil`, `a/../../b`, `C:\evil`, `C:/evil`, `.`, `..`, `a/./b` (400) and
  sanitizes `/abs`, `\\server\share` into safe relative folders; nested folder
  autocomplete (`/api/upload/folders` prefix + "under folder" modes) works;
  op_log row written (albeit with the D13 status bug on failures).
- **Restore**: `/api/restore/all` round-trips byte-exact; 409 while a restore
  is running (observed live race, retried until seen); `/api/restore/files`
  dedups ids, skips unknown ids with a `skipped` count, 400s on bad input;
  subfolder restore works for backslash rows; unsafe `..\` and `C:\` rows are
  refused ("unsafe path skipped"); SSE stream terminates completed.
- **Health**: `/api/health/data` + deprecated `/api/health/detailed` alias
  return identical payloads; issues/same-name/purge-candidates paginate and
  validate (unknown reason → 400); physical scan marks bundle parents and
  members reachable (D2 fix present: plaintext pt_size comparison);
  `POST /api/health/purge` re-verifies candidates and refuses non-candidates;
  purge is audited to op_log.
- **Backends**: list/detail/usage shapes complete, password never leaked in
  detail; `/api/backends/usage` is not shadowed by `/{name}` (route order kept);
  `test/{name}` 200 for known+unknown; add/remove round-trips config.yaml
  (remove confirm-gated); rebalance SSE terminates after a run.
- **dbops**: info (WAL, integrity ok), snapshots create/list/download/delete/
  prune with op_log audit, recovery events list, check + checkpoint OK,
  replicate OK on a local backend; restore/rebuild confirm-gated (never
  executed).
- **Ingest**: status shape complete (staging/sqlite/parquet/accounts/queue);
  generations 400 without account, 200 with; history templates + unknown → 400;
  trend 200; pause/resume gate refuses scans while paused (409 with hint);
  scan validates drop_root (400/404) and the SSE stream terminates after an
  empty drop-root scan.
- **Scheduler**: daemon status idle false; scheduler config get/save round-trip
  (daemon untouched by config changes).
- **Logs/report/misc**: `/api/logs` filters + csv/json export parse correctly;
  `/api/report` carries `op_counters`; `/api/reports` + detail + 404; encrypt
  status exposes `capabilities` and init writes the key (isolated env);
  `/api/hash/{sha}` exact + prefix fallback; replication totals reconcile;
  sources/config export/save round-trips; recompress runs and leaves downloads
  byte-identical; convert on unknown backend fails clean (no 500).
- **Live dashboard**: reachable and serving (see below), no auth errors on
  pages, HTML structure intact.

## Live dashboard probes (GET only — never mutated anything)

Scripted, auditable log in `qa1_live_results.txt`; probes in `qa1_live_probe.py`
(pass 1) and `qa1_live_probe2.py` (pass 2). **Every request was a GET** — no
POST/DELETE/PUT, no `confirm:true`, no restart/kill was ever sent live.

**Pass 1** — the dashboard answered but was badly degraded:
`GET /files` 200 in 20.6s (233 KB page), `/backends` 9.0s, `/ingest` 10.4s,
`/database` **29.05s** (runs `PRAGMA integrity_check` inline — see D18), while
`GET /`, `/logs`, `/report`, `/settings`, `/health`, `/files?q=hostpnqh` and
`GET /api/status` all timed out at 30s. The answered requests came back strictly
sequentially — consistent with the event-loop serialization of D18 (a single
async worker draining blocking handlers one at a time).

**Pass 2** (patient retry, 120s per request, sequential) — the server never
answered again: 8 consecutive requests timed out at 120s each
(`/api/status`, `/api/db/info`, `/api/daemon/status`, `/api/files?limit=3`,
`/api/files?q=hostpnqh`, `/api/search?q=hostpnqh`, `/api/backends`,
`/api/backends/usage`), then `GET /api/health/data` was killed by
`WinError 10054` (connection reset by the host) at 118.6s, and every subsequent
request got `WinError 10061` (connection refused): **the dashboard process
stopped listening during the probe window**.

Final state (`qa1_live_final_state.txt`): TCP connect to 127.0.0.1:8081 refused;
`/api/daemon/status` and `/` refused. The dashboard is DOWN at the time of
writing.

Attribution: GET-only traffic cannot normally stop a listener. The observed
sequence (progressive stall → reset → refused) is consistent with the D18
wedging under concurrent load (three QA agents plus the daemon probing the same
single-worker server), but a crash or an external stop cannot be excluded from
the outside. **Action for the owner:** check the dashboard process/console log
for the time of death and restart it; the D18 fix (blocking work off the event
loop) plus a bounded integrity-check on `/database` removes the biggest
contributor.

Page-level probes that DID complete in pass 1 returned valid HTML (dashboard
shell, dark theme) for `/files`, `/backends`, `/ingest`, `/database` — pages
render correctly once served.

## Test infrastructure notes

- `tests/qa1_web_sweep.py` uses only the shared `env`/`backends` fixtures, a
  `TestClient(raise_server_exceptions=False)` (5xx regressions surface as
  statuses), and `tests.helpers.backup_bytes` for real pipeline uploads.
- An autouse `gc.collect()` finalizer (`_release_stray_db_locks`) mitigates D14
  (leaked restore-worker connections locking the scratch DB on Windows).
- No source file under `backupme/` was modified; no live DB was written; the
  live dashboard process was never restarted or stopped.

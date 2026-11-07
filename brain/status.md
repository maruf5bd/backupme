# Operational status — 2026-11-05

## Implemented / landed

- **Data Health dashboard** (commit `510b60c`): `core/data_health.py` (summary, issues,
  same-name groups, purge candidates, scan plan), `/health` page + 7 `/api/health/*` routes,
  background reachability scan (SSE), confirm-gated purge (soft-delete only, re-verified at
  purge time). Tests: 215 passed / 1 skipped (19 new).
- **Repo tidy** (`698b5dd`): user-facing tools in `scripts/`, diagnostics gitignored.
- **SFTP resilience** (`3b0b8e2`, `6dcaa73`): handshake timeouts, auth fallback, channel
  retry, non-truncating walk with `walk_errors` counter.

## Repairs completed (verified)

- **Zero-storage-row blobs**: 791 files re-backed via real pipeline, 104 deduped, refcount
  drift 0, restore tests 3/3. 725 rows unfixable (716 SOURCE_GONE, 9 SOURCE_CHANGED).
- **Corrupt-blob re-backup**: 10 of 17 corrupt-affected files healed (DB-verified, 0 orphan
  chunks), incl. 1.07 GB Mikrotik vmdk (16 chunks through Storage Box SFTP).
- **Consolidation**: all blob copies unified under `H:\Backupme`; 31,711/31,735 indexed
  paths in one place; 24 corrupt stored files refused by decompress-hash check.
- **Same-name restore verification**: 7,023 groups / 21,459 files -> 7,056 OK, 0 hash
  mismatches, 0 collisions.

## Running right now

- **Storage Box full re-scan** (background): first corrected walk since the truncation fix;
  output -> `diagnostics/storagebox_rescan_report.txt`. Its DB cross-check needed the
  `backend_name` column fix (was `backend`) — cross-check re-runs from the JSON after the walk.
- **Agent: defect fixes D1-D5** (config.py, web/routes.py, data_health.py) — running.
- **Agent: B01 walk-fix port** to smb/ssh/ninedrive/agent backends — running.
- **Agent: W3-B notifications dispatcher (B16)** — new `core/notify.py` + scheduler/ingest
  hooks + tests; owns BACKLOG.md, brain/wiring.md, MANUAL.md while running.

## Backlog progress

- DONE: B02 (90112f5), B10 (b43fb88), B11 (10e4aaf), B13-CLI (3d88ab1), B15 (8beafe8).
- In flight: D1-D5, B01, B16.
- Queued on routes.py: B03, B04, B13-dashboard button, B14 endpoint, then W4 (B05 split,
  B06 helper, B07 except-cleanup), W5 (B08 QA regression suite, B09 skip counters).

## Pending user decisions (do NOT proceed without approval)

- Purge 7 SOURCE_GONE files that reference corrupt blobs (unrecoverable unless source reappears).
- Purge 725 unfixable zero-storage-row files.
- Purge 4 junk "archives" (a.zip, 2FA.7z, 2020-12-03 tar.gz, bitexchanger_v20.zip) — NOTE:
  2FA.7z vault copy is the LAST copy in existence (source deleted).
- Rotate the Storage Box password (it appeared in plaintext in some gitignored diagnostics
  scripts; config.yaml is the single allowed home).
- Approve `gc --apply` to delete 8,498 verified zero-reference remote orphans (28.22 GiB;
  see 2026-11-05 update 2 below).

## Where things stand overall

Dashboard + repair tooling are in good shape; the defect wave (D1-D5, B01) is the last
known correctness work before the wiring wave (B02-B04) and feature wave (B10-B14).

## 2026-11-05 (update 2)

- **Landed since the scoreboard above was written**: B16 notifications dispatcher
  (`dcb074b`), D1 web.token wiring (`80e9d23`), D2-D5 dashboard fixes (`cc5d88a`), D6
  detection — sampled remote-backend verification (`8a27d40`), brain updates (`15f54ae`,
  `42d79b9`, `a7ced81`). Scoreboard correction: DONE now also includes B16; D1-D5 closed;
  D6 open and under investigation in the working tree.
- **Test suite** (full run, 2026-11-05): 326 collected, 2 skipped, 0 failures.
- **NEW finding — GC dry-run on the real backends** (`python main.py --db-path backupme.db
  gc`, dry-run, nothing deleted): backend `h local drive` 8,497 orphan remote files /
  28.22 GiB (30,300,645,922 B); backend `local_disk` 1 orphan / 3,017 B
  (`objects/ab/14/ab1491471215c0de49f43d9143906b78fb74baa791811f5539af60481179f24f.zstd`);
  backend `hostraj Gdrive` NOT scanned (root unreachable at scan time). Deep
  cross-verification: every orphan path has ZERO rows in `blobs`, `file_chunks`, and
  `blob_storage` — leftovers of an earlier DB generation that no restore path can address;
  the 15,607 live blobs on the same drive matched DB rows exactly, so the GC matcher is
  healthy. `gc --apply` awaits explicit user approval (added to pending decisions above).

## 2026-11-07 (update 3)

- **D6 fully closed**: sampled remote-backend verification landed (`8a27d40`) — the health
  scan existence-checks 25 random paths on every REMOTE backend and the dashboard shows a
  "phantom rows likely" badge instead of silently skipping remotes; root cause recorded
  (undetermined second writer in the 11-04/05 repair window; all current write paths verify;
  the 16 vmdk rows carry zstd level 3 vs level 1 on healthy twins — `a7ced81`).
- **Phantom-row cleanup EXECUTED (user-approved)**: 323 storagebox rows deleted with
  pre-flight re-verification, restorable backup
  (`diagnostics/phantom_storagebox_rows_20261105_131934.json/.sql`), post-check clean
  (blobs-with-zero-storage unchanged at 31,045) — `0864d49`.
- **B03 landed** (`01036ab`): confirm:true gate on 6 destructive endpoints (server/restart,
  db/restore, db/rebuild, backends/remove, snapshots delete + prune) + all 7 frontend
  callers updated + 7 tests.
- **B09/B14 core halves landed** (`43d7a8b`): recompress `failed` counter, rebuild
  `walk_failed_backends`/`unreadable_manifests`, `crypto.capabilities()`.
- **Running**: agent B04+B13 (routes/templates), agent B08 (QA regression suite). Queued
  3rd agent: B05 split + B12 op_log remainder + B09/B14 endpoint wiring (spec in BACKLOG).
- Storage Box walk residual: 128 walk_errors on the box (rate-limit transients on the
  provider's own jetbackup tree; not backupme data — no follow-up needed).

## 2026-11-07 (update 4)

- **Dashboard integrity card: was stale-failed (2026-09-06 check id 3), now PASSED today.**
  Triage of that check's 5 files: 4/5 restore with sha match (repairs landed since
  September); the 5th (id 37423) exposed the real D8 shape.
- **D8 RESOLVED — legacy objects were recoverable all along (`d7cb9e6`)**: the 2026-05-era
  uploader wrote 10,853 blob_storage rows with blob_id `bundle/xx/yy/<sha>` but a
  standalone-object storage_path (`<sha>.zstd`, no `@offset:len`) — 9,671 on hostraj
  Gdrive + 1,182 on local_disk — blocking restore of 2,170 live latest-version files.
  Object format reverse-engineered: a 52-byte dedup envelope (`bndl/objects/<2>/<2>/<13-hex
  content-sha prefix>` header + real content), blob named after the WHOLE envelope.
  Fix: `downloader.py` downloads the object directly, gates sha256(object) == blob tail,
  strips the header via file_chunks.pt_size arithmetic cross-checked against the header's
  embedded sha prefix. 7 downloader tests (restore/missing/wrong-content/envelope/tampered).
- **Verification**: 37423 + 15-file random cohort sweep = 16/16 real downloads with sha
  match; fresh integrity check recorded (`report verify --count 10` -> check id 4,
  10/10 completed 2026-11-07). Dashboard restarted on 127.0.0.1:8081 with the fix; its
  per-file verify returns match:true for 37423 end-to-end.
- **Launcher**: `dashboard.bat` (one-click, reuses running instance) committed `50c2839`;
  index.bat menu #23 now opens the port actually passed.

## 2026-11-07 (update 5)

- **B05 COMPLETE (`c909655`)**: agent 3 had died mid-split (~2.5 h silent, nothing
  committed). It left the routes/ package complete but UNWIRED — only 5 of 10 domain
  modules registered, all 43 misc/health/backup/config/scheduler routes still served by
  legacy.py, and the 2,990-line routes.py monolith still on disk. Landed: wired all 10
  domain modules, deleted legacy.py + routes.py (104 route pairs, zero duplicates),
  restored per-app task-state isolation via `reset_background_state()` at registration
  (the monolith scoped its dicts inside register_routes; tests pin that), and landed the
  queued fold-ins: B14 crypto capabilities in /api/encrypt/status, B09 op_counters in
  /api/report, B12 op_log audit in /api/health/purge. 4 new pinning tests
  (tests/test_web_foldins.py); 113 web-area tests green.
- **B07 fanned out** (background agent, owns backupme/web/routes/*.py only): converting
  31 silent except-pass sites to logged failures. B06 marked PARTIAL (helpers exist,
  adoption pending — same file set, queued behind B07). /api/recompress rework onto core
  recompress_blobs queued behind B07 as well.
- Standing user decisions unchanged: gc --apply (8,498 orphans / 28.22 GiB), ~6,509 purge
  candidates, 7 SOURCE_GONE files, 4 junk archives (2FA.7z = last copy), Storage Box
  password rotation.

## 2026-11-07 (update 6)

- **B07 DONE (`dc2c2d9`, background agent)**: 43 silent except-pass sites converted
  (40 logged via module loggers, 3 justified-silent with written reasons — non-numeric
  form input kept as-is, staging-file races). Behavior unchanged; 3 caplog tests
  (tests/test_web_b07.py); 97 web-area tests green.
- **FULL SUITE GREEN (393 collected, 1 skipped, 0 failures) — and the 3
  ordering-dependent test_web_api.py failures are GONE without touching that file.**
  Root cause was the same one the state-reset fix addressed: background-task state
  leaked across test files under the split's process-global dicts. The B05 isolation
  fix (reset_background_state at registration) resolved them; no test edits needed.
- Dashboard restarted on 127.0.0.1:8081 serving the completed split — verified live:
  /api/encrypt/status carries capabilities (argon2id / AEAD tag-at-end), /api/report
  carries op_counters, integrity card = check id 4 completed 10/10 (2026-11-07).
- **B06 adoption + /api/recompress rework fanned out** (background agent, owns
  backupme/web/routes/*.py + tests/test_web_b06.py): switch inline op_log/SSE/thread
  copies in backup/dbops/files/misc (+ingest/backends) onto the common helpers;
  /api/recompress moves onto core recompress_blobs with counters + op_log audit
  (its inline copy uploads without hash verification and has no audit).

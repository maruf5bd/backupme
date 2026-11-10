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
  see 2026-11-05 update 2 below). **DONE 2026-11-10**: executed at the shrunken count of
  785 orphans / 21.96 GiB after the consolidation waves emptied the drive — see update 17 below.

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

## 2026-11-07 (update 7)

- **B06 DONE (`e819600`, background agent)**: every inline op_log/SSE/thread copy in
  backup/dbops/files/misc/ingest/backends adopted the common helpers (none needed to
  stay inline). /api/recompress now calls core recompress_blobs: counters in the
  response ("converted" kept as alias for settings.html), op_log audit feeding /api/report
  op_counters, hash-verified writes, encrypted blobs skipped + counted (the old inline
  loop silently corrupted encrypted blobs and uploaded without verification).
- **Activity page rebuilt** (`293f6aa`): the /logs page was server-rendered with a dead
  Live toggle (poll discarded its own fetch) and a status filter offering nonexistent
  statuses. Now a real console: working live polling, status cards + count chips,
  instant client-side filtering, per-row durations with honest "stale — never finished"
  for zombie rows (>24h running), friendly op summaries, click-to-expand detail with
  pretty JSON + copy, Load-older pagination, time-sorted display. Verified in-browser
  (screenshots). The 5 zombie scan rows from 2026-05-30 are visible as stale.
- **Full suite green after both waves** (394 collected, 393 passed + 1 pre-existing skip,
  exit 0 — agent-run). Dashboard restarted and verified: health/data, report card
  (check 4, 10/10), encrypt capabilities, new logs console.
- Backlog P1 code-quality track (B05-B07) is now fully closed. Remaining known work:
  QA-visibility item (errors surfaced in partial responses) satisfied via B07 logging;
  standing user decisions unchanged (gc apply, purges, 2FA.7z, password rotation).

## 2026-11-07 (update 8)

- **Restore-from-search landed (`ad69a1b`)**: the user asked whether search results can
  be restored — per-file restore existed (Download / Download-to-path via Details), but
  no bulk option. New `POST /api/restore/files` restores an explicit id set to a
  server-side folder through the existing background worker + SSE progress card; the
  Files table header gained "Restore all results to folder…" (shown when a query is
  active) and "Restore selected to folder…" (also in the bulk bar). Duplicate ids
  collapse, multiple versions of one path restore once (latest id wins), unknown ids
  are skipped and reported. Tests: tests/test_web_restore_files.py (5).
- **D9 — P0 data-correctness bug found and fixed (`cd5361d`)**: a changed re-upload
  UPDATED the file row in place (index_file matches path+version_seq=1) but the
  chunk-write sites' `INSERT OR IGNORE` silently kept the stale chunk rows — restores
  then served the OLD content under the NEW sha and the new blob got a phantom ref.
  Found by the new feature's "latest version wins" test (real integrity mismatch).
  Fix: `_replace_file_chunks()` at all 3 chunk-write sites (batch, chunked, donor) +
  migration.py — delete chunk rows first (FK), unref displaced blobs inside the caller
  txn (`unref_blob(commit_now=False)`), insert new mapping; no-op when unchanged.
  Dead shadowed `unref_blob` removed from dedup.py.
- **Live impact quantified (read-only)**: the 2,183 sha-vs-blob-tail mismatches among
  single-chunk live files are all 2026-05-era rows and are the legacy member-NAMING
  convention — restore probes (K:\ hotspot files) sha-match file_sha, so they are NOT
  damage. A 12-sample restore-probe over the 20,523 refreshed 2026-05 rows found 0
  additional damage (1 error = the known missing-blob class, id 3105). Exactly 1 proven
  damaged row: **id=2 (backupme.db's own backup, re-uploaded 2026-05-30)** — healed
  live by re-backing a consistent sqlite snapshot through the FIXED pipeline
  (177 MB, 3 chunks, post-fix restore sha-matched). Sample scripts:
  `stale_chunk_census.py`, `stale_refined_census.py`, `stale_damage_sample.py`,
  `repair_id2_and_probe3105.py`.
- **Full suite green at landing** (401 collected via wide runner, 1 pre-existing skip,
  exit 0) covering feature + fix together.
- **Upload destination folder (`186c8b3`)**: the web uploader already had backend +
  copy-count selection; the missing piece was WHERE IN THE INDEX files land — they were
  hardcoded to `web_upload/<filename>`. A new optional "Destination folder" input lands
  uploads at `web_upload/<folder>/<filename>`; `..`/`.`/drive-letter/absolute shapes are
  rejected with 400 and filenames are reduced to their basename. Verified live
  (upload → search hit at the chosen path → sha-matched download).
- **Backlog scoreboard reconciled (`c008a43`)**: B01-B16 ALL DONE (B12/B13 verified
  end-to-end incl. a live rescan on local_disk: 19,819 walked, 0 missing, 0 mismatch).
  The improvement queue is empty; remaining items are the standing user decisions.
- **Storage test (all 4 backends) + D10 opened**: locals healthy — local_disk / h local
  drive / hostraj Gdrive connect + 15/15 sampled restores sha-matched (checks 5-10).
  storagebox: password VALID; config was on port 22 (box refuses SFTP channels there) —
  FIXED to port 23 in config.yaml (gitignored) and verified (chunk downloads SHA-MATCH);
  app-level checks still flake on the box's ~10-connection limit when the dashboard's
  probes churn sessions (D10). Capacity truth: storagebox holds 3 blobs / 0.03 GiB
  (essentially empty off-site) and h local drive + hostraj Gdrive are the SAME directory
  (H:\Backupme) — only 2 distinct physical copies exist. Filling the box = user decision.

## 2026-11-07 (update 10)

- **QA campaign (3 subagents, 2-concurrent cap)**: web/API (132 tests, 106 routes),
  CLI (83 tests, 40 commands), core engine (37 scenarios + full-suite baseline).
  34 defects found (1 P0 / 11 P1 / 22 P2), each verified twice; all pinned as
  xfail(strict=False) in the landed suites. Reports: qa_report_web.md,
  qa_report_cli.md, qa_report_core.md.
- **P0 fixed (`2d69c99`)**: CLI search crashed on hyphenated queries — quoting +
  LIKE fallback moved into index.search_files (shared by web + CLI).
- **P1s fixed**: backend pool (D10 root cause: _get_backends invalidated the cache
  EVERY request → fresh Storage Box SSH sessions per poll → box limit → dashboard
  outage; now fingerprint-keyed instance reuse, `498029a`); recompress_blobs no longer
  renames parent bundles (made ALL batched files unrestorable) and resolves backends
  via real config (CLI no-op, `2594967`); 0-byte files restorable + verify reports
  real failure reasons (`526794c`); ingest archives whose members ALL failed stay in
  the drop root for retry instead of being stranded via .done (`208203b`); restore
  worker: forward-slash path filter, absolute-path escape guard, soft-failure
  truthfulness, thread-local SQLite close (locked-DB flake source); upload route:
  unknown backend 400, honest op_log, no temp-path leak; download serves the indexed
  filename; to-path answers 400/404/500; report/generate counters real; `db check`
  read-only without --repair. Full suite 438 tests exit 0 (qa_final run).
- **Dashboard restored**: the outage was D10; restarted serving all fixes.

## 2026-11-07 (update 11)

- **P2 polish batch landed (3 background agents + main-thread fixes), full suite 479 passed / 1 skipped / 0 failed, exit 0** (up from 438).
  - **Agent A — CLI P2s**: `hash-lookup` unknown hash exits 1 + resolves FILE sha256 (exact, then unique hex prefix; ambiguous prefix lists candidates); `backend drain <unknown>` errors with exit 1; `db history --json/--no-json` made real (human table renderer reachable again); `--json` added to backup/download/search/hash-lookup/convert/gc (human defaults byte-identical); hard_purge's dead `skipped` counter replaced by honest `above_watermark` accounting (scheduler log line updated too); MANUAL §10 kept drift-free (phantom scan 0). Tests: tests/test_cli_p2.py (28).
  - **Agent B — web/core**: qa1 D18 (re-verified OPEN after the P1 wave — brain D12) fixed: blocking integrity checks/downloads/uploads/convert/recompress moved to `asyncio.to_thread`; SSE op streams terminate after one idle snapshot pre-first-op; `/api/files` hash search mode (D15); `content_text` populated by the real pipeline (D17: unencrypted ≤64 KiB UTF-8, donor inheritance, re-upload replaces/clears); SFTP connect single-flight + bounded transient retry. Tests: tests/test_web_p2.py (32), tests/test_sftp_connect.py (8).
  - **Agent C — content backfill**: new `backupme db backfill-content` (resumable, candidates-only dry-run, per-file error isolation, guarded NULL-safe UPDATE, FTS rebuild after updates, skipped_encrypted gate) so historical rows become content-searchable. Tests: tests/test_content_backfill.py (10). NOT yet run against the live DB — user decision (it downloads every candidate ≤64 KiB).
  - **Main thread — D11 (found by agent C)**: `index.rebuild_fts()` NameError fixed by rewriting onto the native FTS5 'rebuild' command; `index_file` now deletes the FTS row BEFORE the files UPDATE (external-content DELETE semantics — stale terms from changed re-uploads eliminated); pins in test_web_p2.py.
- **Remaining known work**: D18-class follow-ups (dbops check/snapshot, backup-trigger endpoints, health summary still inline); CLI P2-11 note (encrypt init writes real HOME — documented behavior); standing user decisions unchanged (gc --apply 8,498 orphans / 28.22 GiB, ~6,509 purge candidates, 7 SOURCE_GONE files, 4 junk archives incl. 2FA.7z last-copy, Storage Box password rotation, filling the box, live content backfill).

## 2026-11-08 (update 12)

- **D18b landed (`ba72b95`, pushed both remotes)**: the remaining blocking handlers wrapped — `/api/db/check` (integrity PRAGMA), `/api/db/snapshot` (VACUUM INTO + zstd), `/api/backup` + `/api/backup/source` (os.walk + full upload pipeline, minutes-class, restructured into sync helpers via to_thread with response shapes/op_log/implicit-None preserved), `/health` page summary, plus `/api/db/info` + `/api/health/data` found in review. Off-event-loop pinned by `asyncio.get_running_loop()` assertion (tests/test_web_d18b.py, 7 tests). qa1 report-generate flake fixed (count 7→12 as its own comment specified; proven 2-in-12 flaky). Full suite **486 passed / 1 skipped / exit 0**.
- **New backend `k backupme-data` added (user request)**: local, `K:\backupme-data`, 40.5 GB free of 380 GB — connection-tested. K was empty; the box has 3.9 TB used of 5.0 TB (jetbackup etc.), 1.2 TB free — the ~196 GB fill plan still fits (~1/6 of free space).
- **Softaculous backups uploaded to K via the real pipeline (user request)**: 20 tar.gz (2,281,887,649 B) in 36.8s → stored 2,258,329,053 B → **saved 1.0%** — honest number: gzip-compressed daily snapshots don't dedup or recompress. Restore spot-check sha-matched. This motivated the user's **archive-extract-first feature** (approved): extract archives on backup, hash every member, archive = virtual DB manifest (fingerprint = manifest hash → archive-level dedup), members as files under `archive/<fp>/`, nested archives with caps + whole-file fallback, manifest sidecars WITH chunk recipes replicated to every backend (DB-loss insurance), daily DB snapshots shipped off-site, `db recover-from-manifests`. **Full detailed plan: `brain/plan_archives.md`** — Phase 1 (tar/tar.gz/zip + tables + restore + tests) and the off-site guard (parallel agent) in flight.

## 2026-11-08 (update 13)

- **Phase 1 archives LANDED (`6cd6691`)** + **off-site guard LANDED (`912bb2f`)**, both pushed to GitHub + GitLab. Full suite **519 passed / 1 skipped / exit 0** (was 486). Review notes: dead `_guard_cfg` helper removed before landing; MANUAL 7.8 (guard) merged from the guard agent's paste-ready lines; sanitize_member moved verbatim from cpanel_ingest to archives (import keeps `cpanel_ingest.sanitize_member` resolving); schema lives in schema.py so the tables went into the base schema AND migration 3 (apply_migrations now runs multi-statement migrations).
- Archive feature truth: `backup` extracts tar/tar.gz/tgz/tar.bz2/tar.xz/zip by magic bytes (plain .gz/.bz2/.xz stay normal files — only real tar streams extract); members upload through the normal pipeline (full cross-index dedup); `archive/<fp>/` rows make members searchable/restorable immediately; nested archives recurse (depth 5 / 200k members / 20 GiB, nested failure stays scoped to that member); corrupt/encrypted/rar/7z/over-cap → stored whole with explicit reason; manifest sidecars carry per-member chunk recipes to every backend (skipped when encryption enabled); `backupme archive list|show|restore` with fingerprint-prefix + ambiguity handling.
- Guard feature truth: daily daemon tick ships a fresh DB snapshot to every backend under `guard/snapshots/<UTC ts>-gen<gen>.db.zst`, size-verified per backend, partial-tolerant, never deletes (pruning deferred — box has 1.2 TB free, ~65 GB/year burn is acceptable); `db snapshot --offsite` for manual runs; config `guard.offsite_snapshots` (default true).
- Next: Phase 2 (7z/rar, dashboard Archives page, `db recover-from-manifests`, pruning design) and Phase 3 (re-ingest the 20 Softaculous archives through the extractor to reclaim ~2.25 GB → expected a few hundred MB; displaced whole-blob rows unref'd after member verification, actual deletion only via gc/purge approval gates).

## 2026-11-08 (update 14)

- **Phase 2 fanned out (2 agents)**: G = 7z (py7zr) + rar (external tool detection) formats with unchanged fallback rules; H = dashboard Archives page + confirm-gated restore API + `db recover-from-manifests` (rebuilds a vault map into a NEW sqlite file from the manifest sidecars, full verify = decompress-hash per blob). File territories disjoint; both scratch-DB only.
- **Phase 3 re-ingest RUNNING on the main thread** (single live-DB writer while agents stay on scratch DBs): 20 Softaculous archives → `ingest_archive_file` → members to K; gated sequence ingest → sha-verify → soft-delete old whole-blob rows via `_soft_delete_file()` + op_log audit (`core.op_log` API is append/mark_committed/mark_failed — the op_log_start/finish names are web-layer wrappers only; first run failed on that and was corrected before anything was deleted). Physical reclaim of the displaced ~2.25 GB still requires user-approved `gc --apply`.
- Brain plan file (`brain/plan_archives.md`) updated with the Phase 2 agent scopes and the Phase 3 gated procedure.

## 2026-11-10 (update 15)

- **Phase 2 LANDED (`620eb7e` 7z+rar, `5c5f6f5` dashboard Archives + recover-from-manifests)**, pushed both remotes. Full suite **553 passed / 1 skipped / exit 0**. 7z via py7zr 1.1.4 (caps fire from header metadata pre-extraction); rar works via the machine's WinRAR 7.13 (subprocess sandboxed, exit-0-only trust); dashboard /archives page + confirm-gated restore API; `db recover-from-manifests` rebuilds a vault map into a NEW sqlite file with decompress-hash verification and shape-filtered sidecar scanning.
- **Phase 3 COMPLETE**: all 20 Softaculous archives ingested extract-first (30 archive rows incl. 10 nested zips; ~198k member entries; 12,575 member downloads sha-verified; the 20 old whole-blob rows soft-deleted via `_soft_delete_file` + op_log op12). **The 2.25 GB physical reclaim already happened** — not by us: the owner's own restore-gated prune campaign (`D:\restore\merged-gdrive\restore_and_prune.py`, 4,062-object audit) deleted verified-safe objects incl. our 40 old blobs. Physical audit (user-mandated, both directions): **all 51,547 referenced blobs exist on the K disk, 0 missing**; "orphans" = the pre-existing `tree/` pointer subsystem (uploader `_write_manifest_and_pointers`), not damage. K now holds ~466 MiB of blob data (was ~2.25 GB of whole blobs + members).
- **D13 found + fixed**: the scheduler gc tick ran `run_gc(apply=True)` — a daemon would silently purge every 7 days. Now report-only unless `scheduler.gc_apply: true` (config + example + pinning test). No daemon was running; the campaign's prune-apply was the deletion source.
- Dashboard restarted serving the Archives page; still pending: owner decisions on remaining purge candidates (the prune campaign's soft-deleted 3,125 retired rows now among them), Storage Box fill, password rotation.

## 2026-11-10 (update 16)

- **`backupme archive stats` landed (formal channel for the Phase-3 savings numbers)**: core `archives.get_archive_stats(fingerprints=None)` computes, straight from the manifest tables — original archive bytes, total uncompressed member bytes, member entries (file vs nested), **unique logical bytes** (per distinct member sha, max size), dedup savings vs uncompressed, and the delta vs keeping the archives whole. CLI `archive stats [-f FP]... [--json]` with prefix scoping (unknown prefix → exit 1) and a human renderer. Live truth over the 30 ingested Softaculous archives: **2.47 GB original → 5.58 GB uncompressed members → 1.34 GB unique logical = 76.0% member dedup, 45.9% saved vs storing the archives whole**. Tests: tests/test_archive_stats.py (2, ingest-backed math + CLI surface). Note: a partially-built version of this command was found uncommitted in the tree after the Phase-3 push (dead duplicate formatter + no tests) — cleaned, tested, documented (MANUAL 7.7 + §10 rows), and landed.

## 2026-11-10 (update 17)

- **GC apply EXECUTED (user-approved; the standing decision from updates 5/11/15)**:
  `python main.py --db-path backupme.db gc --apply` deleted **785 orphan objects /
  23,578,828,744 B (21.96 GiB) from `h local drive`**, exit 0; post-apply dry-run reports
  **0 orphans on every reachable backend** (local_disk, h, Gdrive, k; storagebox
  unscanned — see caveat below).
- **Gate re-verification caught a shrunken target**: the dry-run count had fallen from
  8,497 (11-05) to 785 because the consolidation/prune waves physically emptied
  H:\Backupme — lesson reinforced: ALWAYS re-run the full gate on the CURRENT DB
  immediately before apply, never trust a stale dry-run number.
- **Gate results at apply time**: all 785 paths had ZERO rows in blobs / file_chunks /
  blob_storage; in-flight guard clean (newest orphan 24.2 h old, last DB write 2026-11-08).
- **Matcher anomaly investigated, not ignored**: the h walk saw only 808 files and matched
  only 23 of 15,000 h-name rows. Direct os.walk confirmed the drive really holds 821
  files; all 14,977 missing-reference rows have copies on OTHER backends
  (missing_NO_other_backend=0) — stale pointers to the drained h:/Gdrive shared directory
  (same physical dir per update 10), consistent with update 15's K-disk audit. Zero
  reachability loss; the stale h-name rows are the same phantom-row class as D6 and are
  now quantified (14,977) — cleanup candidates for a future migration, harmless today.
- **Caveat recorded**: the storagebox walk skips `objects` and `bundle` prefixes
  ([Errno 2] after retries — the prefixes don't exist on the box), so GC coverage of the
  box remains nil and its "0 orphans" is unverified. Box holds ~3 blobs per update 10, so
  exposure is minimal, but any future box onboarding should create the prefixes or teach
  the walk to tolerate their absence loudly (it does log the skip — B09 compliant).

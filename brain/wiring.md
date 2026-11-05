# Wiring status — 2026-11-05

How modules, config, and APIs connect; what is wired, what is broken, what is queued.

## In flight

- **D1 — `web.token` from config.yaml ignored**: `load_config()` had no `web` branch, so the
  auth middleware ran tokenless and the whole admin API served unauthenticated (localhost-only
  binding mitigates). Fix agent is wiring the section + regression test.
  **Dated note 2026-11-05 (update 2): LANDED as `80e9d23`** — `load_config()` now parses
  the `web` section into `WebConfig` (config.py, 2 lines); regression test
  `tests/test_web_token_config.py` committed alongside.

## Queued (BACKLOG refs)

- **B02 (P0)** — dead config keys `chunking.parallel_uploads`, `verification.fallback_on_fail`
  warn on every startup. Plan: remove keys from config.yaml (no behavior change), add a test
  pinning example config <-> dataclass fields so section drift cannot happen silently again.
- **B03 (P0)** — destructive endpoints (`/api/server/restart`, `/api/db/restore`,
  `/api/db/rebuild`, `DELETE /api/backends/remove/{name}`) need confirm-flag + token parity
  with the purge endpoint.
- **B04 (P0)** — health surface consolidation: old `/health` ping already repurposed as the
  Data Health page (restart pollers only check `r.ok`, unaffected). Remaining: overlap between
  `/api/health/detailed` (routes.py:1311) and new `/api/health/data` — pick one canonical shape.
- **B11 (P2)** — scheduler data-health tick: **DONE 2026-11-05** — verify tick logs the
  summary + warns on regressions (tests/test_scheduler_health_tick.py).
- **B16 (P1)** — NEW: `NotificationsConfig` (on_failure/desktop) is parsed by load_config but
  consumed by NOTHING — no dispatcher module exists. Scheduler regressions currently surface
  as log lines only. **LANDED 2026-11-05 as `dcb074b`** — `core/notify.py` dispatcher +
  scheduler/ingest `notify_failure` hooks; `notifications.on_failure` now has a consumer.
- **B13 (P2)** — backend rescan (walk vs DB diff) as dashboard button + `backend rescan NAME`.
  Known bug in the ad-hoc script `storagebox_rescan.py`: queried `blob_storage.backend` but the
  column is `backend_name`, so its DB cross-check skipped itself (caught by its own guard);
  fix + re-run the cross-check from `diagnostics/storagebox_rescan.json` when the walk lands.

## Verified good (do not re-litigate)

- Route order: `/api/backends/usage` (specific) registered before `/api/backends/{name}`
  (generic); 3-segment routes cannot shadow it.
- Scheduler `_periodic_loop`: every tick has its own exception guard + non-blocking
  single-flight lock; ingest/purge/export gated on `cpanel_ingest.enabled`.
- Single-writer SQLite: WAL, thread-local connections, explicit commits in every writer.
- Optional-dependency fallbacks (compress, crypto, progress, db_lifecycle) are intentional,
  module-level `except ImportError`.
- Auth middleware shape (`app.py::_auth_middleware`): 401 for all `/api/*` without matching
  bearer when a token IS configured; pages stay public by design.
- `_soft_delete_file()` is the single soft-delete path shared by DELETE `/api/files/{id}` and
  `/api/health/purge` (extracted by the data-health work).

## 2026-11-05 (update 2)

Append-only update; every entry above stays exactly as written.

### Landed (commits now in HEAD)

- **B02 — `90112f5`**: dead keys `chunking.parallel_uploads` and
  `verification.fallback_on_fail` removed from config.yaml (zero behavior change, no more
  startup warnings); `config.example.yaml` committed, documenting every section incl. the
  nested `verify_restore` shape and all backend types;
  `tests/test_config_example_sync.py` pins example <-> dataclass field drift in BOTH
  directions, so a new dataclass field without an example entry (or vice versa) fails tests.
  Closes the B02 entry in the Queued section above.
- **B10 — `b43fb88`**: CLI wired for both commands — `backupme health [--json]
  [--purge-candidates] [--limit N]` -> `core/data_health`; `backupme verify [--count N]
  [--backend NAME] [--file ID]` -> `core/verify` (restore-and-hash, exit 1 on any failure).
  Both registered in `cli/__init__.py`; `tests/test_cli_health.py` covers JSON shape, human
  output, empty-DB check, unknown-id exit. MANUAL's command table now matches reality.
- **B11 — `10e4aaf`**: scheduler verify tick logs the data-health summary
  (`health tick: live=... no_chunks=...`) and emits `data health regression: key: old -> new`
  warnings when tracked buckets grow (tests/test_scheduler_health_tick.py). Same landing the
  Queued section above already marked DONE; commit recorded here for the wiring trail.
- **B13 — `3d88ab1`**: `backupme backend rescan NAME [--json]` CLI wired to
  `core/backend_scan.rescan_backend` — walk vs blob_storage diff with the correct
  `backend_name` column (the ad-hoc `storagebox_rescan.py` bug from the Queued entry above is
  fixed in the product path), bundle-member `@offset:len` rows excluded and parents compared
  as plain paths; reports missing_on_box / extra_on_box / size_mismatch + missing_bytes;
  exit 1 on missing/mismatch; tests/test_backend_rescan.py. CLI half landed; the **dashboard
  button remains queued** (still gated on routes.py work).
- **B01 — `24146d6`**: non-truncating walks ported to the sibling backends — `smb.py`,
  `ssh.py`, `ninedrive.py`, `agent.py` now surface walk/list failures instead of silently
  truncating: `walk_errors` counter, retries on transient list failures, skips logged. The
  SFTP-only fix from `6dcaa73` is no longer unique to sftp/ftp.

### D-wave — LANDED (first written as "pending commit"; commits landed later the same day)

- **D1 — `80e9d23`** — web.token config wiring (see dated note on the In flight entry above):
  `backupme/config.py` (+2: `if "web" in raw: cfg.web = WebConfig(...)`) +
  `tests/test_web_token_config.py`.
- **D2 — `cc5d88a`** — health-scan false "unreachable" verdicts for compressed bundles fixed:
  `core/data_health.py::local_scan_plan` now LEFT JOINs `blobs.pt_size` and carries it next
  to `stored_size`; `web/routes.py` health scan judges member byte ranges against the parent
  bundle's PLAINTEXT `pt_size`, never the compressed on-disk size; `bundles_size_mismatch`
  bucket added to the scan result; bundle-parent detection widened to cover
  `storage_path` prefixes, not just `blob_id`.
- **D3** — search no longer 500s on user punctuation: `_fts_phrase()` quotes any query as a
  single FTS5 phrase (double quotes escaped); `_search_files_safe()` falls back to a plain
  LIKE query on `sqlite3.OperationalError`; applied to `/api/files`, `/api/search`, and the
  `/files?q=` page (`web/routes.py`).
- **D4** — `/api/browse` descends forward-slash paths: prefix matching now tries both
  separator styles (`base/%` and `base\%`), remainder slicing normalized, direct-file detail
  lookup matches either style (cPanel-ingested rows use forward slashes).
- **D5** — unknown/soft-deleted file id returns 404 with a JSON error body on
  `GET /api/download/{id}` and `GET /api/files/{id}/preview` (was 500). Bonus fix found in
  the same work: the download FileResponse `background` was a bare lambda (not awaitable —
  TypeError on every download); now a proper `starlette.background.BackgroundTask`.
- Regression tests: `tests/test_web_defects.py` + expanded `tests/test_web_health.py`
  (+107 lines) — all landed with `cc5d88a` (D2-D5) and `80e9d23` (D1).

### In flight (observed during this update)

- **D6 — phantom storagebox rows**: NEW defect opened 2026-11-05 (`42d79b9`); detection
  landed as `8a27d40` (sampled remote-backend verification in the health scan); root-cause
  findings recorded in the brain (`a7ced81`). Investigation continues in the working tree
  (`data_health.py`, `web/routes.py`, `templates/health.html` uncommitted at the time of
  this note).

### Still queued (unchanged; old Queued section above stands)

- **B03 (P0)** — destructive endpoints (`/api/server/restart`, `/api/db/restore`,
  `/api/db/rebuild`, `DELETE /api/backends/remove/{name}`) still need confirm-flag + token
  parity with the purge endpoint.
- **B04 (P0)** — health surface consolidation still open: overlap between
  `/api/health/detailed` (routes.py:1311) and `/api/health/data` — pick one canonical shape.
- **B13 dashboard button** — CLI landed (above), button waits on routes.py.
- Remaining BACKLOG.md open items, unchanged: B05 (split routes.py into domain routers),
  B06 (one background-task runner), B07 (no silent except-pass in API responses), B08 (QA
  harness -> regression suite), B09 (count/report skips), B12 (purge approval workflow e2e),
  B14 (crypto capability introspection).

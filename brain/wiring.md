# Wiring status — 2026-11-05

How modules, config, and APIs connect; what is wired, what is broken, what is queued.

## In flight

- **D1 — `web.token` from config.yaml ignored**: `load_config()` had no `web` branch, so the
  auth middleware ran tokenless and the whole admin API served unauthenticated (localhost-only
  binding mitigates). Fix agent is wiring the section + regression test.

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
  as log lines only.
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

# QA Report — Security Review (ingest / lifecycle / rebalance / web)

Reviewer: controller (QA-Security mandate; agent dispatch hit concurrency limit)
Scope: `core/cpanel_ingest.py`, `core/db_lifecycle.py`, `core/rebalance.py`, `web/routes.py` (ingest + backends sections), `cli/ingest_cmd.py`, `cli/backend_cmd.py`
Method: code review + adversarial probing of sanitizer functions (executed), no product files modified.

## Verdict: No Critical findings. 2 Medium, 3 Low. Ship-blocking: no.

### Probes executed (all passed)

- `sanitize_member`: `..`, `../..`, `a/../..`, `....//`, `..%2f`, mixed `\` separators, `C:`, blank, `a/b/../../../c` → all rejected or normalized safely. Absolute/drive-letter/empty rejected. Output always forward-slash joined relative path.
- `_sanitize_account`: `../../x` → `x`, `a/b` → `a_b`, `..%2f..` → stripped. Only `[A-Za-z0-9._-]` survive; path traversal via account impossible.
- SQL: every query in scope is parameterized (`?` placeholders). `/api/ingest/history` uses an allow-list of fixed templates; the only interpolation is `limit`, int-capped to 1..500 before formatting. `query_history_duckdb` passes user params via `con.execute(sql, params)` (DuckDB prepared parameters). The view-creation statement embeds only the server-derived glob path, quote-escaped. **No SQL injection paths found.**
- Tar extraction: `extractfile` + manual write only (never `tar.extract`); `tarfile` enforces the declared member size on the stream, and `max_member_size_gb` skips oversized members before extraction — gzip-bomb exposure is bounded by declared size. Symlinks/devices never written. Staging file deleted in `finally` even on upload failure.
- `check_staging_budget` gates every archive submit; oversize members skipped, not buffered.

### Findings

**M1 — Unauthenticated web app with server-filesystem write endpoints (app-wide pattern, inherited by new endpoints)**
`POST /api/ingest/restore-generation` accepts arbitrary `out` and `retar` paths (writes downloaded members / a tar.gz anywhere the process can write); `POST /api/download/{id}/to-path` already had the same shape. The dashboard has no authentication; `backupme web` defaults to `127.0.0.1` but accepts `--host 0.0.0.0`.
Impact: any local process or LAN peer (if bound wider) can exfiltrate vault data to a chosen path or fill a disk.
Suggested fix (small): bind-scoped warning when `--host` is not loopback + optional `--token` shared-secret header check in one middleware. Consistency note: this is a pre-existing app posture, not introduced by the ingest work.

**M2 — `.done` move and raw upload run after `op_log.mark_committed`; a failure there desyncs state**
`ingest_archive` marks the op committed, then performs keep_raw upload and the `.done` move outside the guarded try. If the move raises (locked file, permission, target on another volume), the worker reports `failed` while `op_log` says `committed`; the archive stays in the drop root and every future scan hits the idempotency skip — so it is never re-ingested *and* never moved (permanent queue resident until manually removed).
Suggested fix: wrap post-commit disposition in its own try/except, report `"status": "ok", "disposition_error": ...` instead of failed, and log a `recovery_events` row.

**L1 — Windows reserved device names survive account sanitization**
`_sanitize_account('CON')` → `'CON'`; `mkdir('.done/CON')` raises on Windows, feeding the M2 path. Fix: extend the reject list with `CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]` (case-insensitive) → fall back to `default`.

**L2 — `account` param used unescaped in DuckDB LIKE patterns**
`cpanel/{account}%` — `%`/`_` in the user string act as wildcards. No injection (parameterized), only broader/narrower matches on an informational endpoint. Fix if desired: escape `%`/`_` before embedding.

**L3 — Invalid `account_pattern` regex fails open**
Logged and ignored, archives fall back to folder-name derivation. Documented behavior; keep.

### Positive notes
- Idempotency keys are content-hash based; op_log prevents duplicate work even under concurrent scans.
- `restore_cpanel` paths are built from DB-stored relative paths (sanitized at ingest time), not from request input.
- No secrets/tokens are logged anywhere in the new subsystems (checked log calls).

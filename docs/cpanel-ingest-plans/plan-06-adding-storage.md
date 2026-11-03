# Plan 06 — Adding Storage (new backend onboarding + rebalance)

## Goal
Operator can add a new storage backend (e.g. second 9drive pool, new S3 via rclone, extra local disk) and progressively fill it with vault data — new ingest writes land there immediately, existing blobs backfill via rebalance, old backends drain via GC. No re-upload from sources, no downtime.

## How it works (reuse, no new copy engine)
- New writes: `upload_bundle` already targets `backends[:redundancy]` filtered by health/circuit-breaker — a healthy new backend with high `priority` immediately receives new blobs.
- Backfill: `mirror.replicate_to_n(blob_id, target_backends, config)` already downloads one copy and re-uploads to missing backends + records `blob_storage` rows. Rebalance = iterate blobs missing on the new backend, call `replicate_to_n`, batched + resumable.
- Drain: lowering an old backend's `priority` to 0 stops new writes; `gc.run_gc(apply=True)` reclaims remote objects only when unreferenced — safe drain = rebalance first, then remove backend from config, then GC.
- Safety: `check_consistency()` verifies replica hashes match before any backend removal.

## New files
- `backupme/core/rebalance.py`
  - `plan_rebalance(target_backend, source_filter=None) -> dict`: count blobs missing on target (query `blob_storage` anti-join), estimate bytes — powers `--dry-run`
  - `rebalance_to(target_backend, backends, config, limit=None, progress_cb=None) -> dict`: iterate missing blobs oldest-first, `replicate_to_n` per blob, checkpoint every 100 blobs to `rebalance_checkpoint.json` (converter pattern), per-blob try/except (continue on failure), resumable via checkpoint
  - `drain_backend(backend_name, backends, config, progress_cb=None) -> dict`: rebalance all blobs OFF the named backend onto remaining healthy backends (inverse direction), then report unreferenced remote bytes eligible for GC

## Modified files
- `backupme/cli/backend_cmd.py`:
  - `backend add` gains `--type/--name/...` non-interactive flags (for scripts) + `--json` output; existing interactive flow unchanged
  - new `backend onboard NAME [--rebalance] [--dry-run] [--json]`: test connection → show free/total → optionally run `rebalance_to` immediately with progress bar
  - new `backend rebalance NAME [--limit N] [--dry-run] [--json]`: backfill one backend (or all under-replicated blobs with `--all`)
  - new `backend drain NAME [--dry-run] [--json]`: copy everything off a backend, then print GC/removal runbook steps
  - `backend list` / `backends` command: add per-backend stored bytes, blob count, free space, dedup share (`--json`)
- `backupme/web/routes.py`:
  - `POST /api/backends/onboard`: test + optional immediate rebalance (background thread + SSE via existing scan-state pattern)
  - `GET /api/backends/rebalance/stream`: SSE live rebalance progress
  - `POST /api/backends/{name}/rebalance`, `POST /api/backends/{name}/drain`: trigger operations
  - `GET /api/backends/usage`: per-backend stored bytes, blob counts, free/total/quota, ingest bytes in last 24h (for the storage views)
- `backupme/web/templates/backends.html` (upgrade): per-backend usage bars (stored vs quota), "Onboard" wizard button (test → rebalance), per-card Rebalance/Drain buttons with progress modal, ingest-by-backend mini chart
- `backupme/web/templates/ingest.html`: per-backend ingest share (bytes written per backend in last 24h) — shows new backend receiving writes
- `config.yaml.example`: comment block showing multi-backend ingest example (two 9drive pools + local, priorities explained)

## Operator runbook (docs)
1. `backupme backend add` (interactive or flags) → `backend onboard NEW --rebalance` (or GUI Onboard wizard)
2. new ingest writes land on NEW immediately (priority); backfill runs in background, resumable
3. verify: `/api/backends/usage` shows NEW filling; `check_consistency` clean
4. to retire OLD: lower priority → `backend drain OLD` → remove from config → `gc --apply` reclaims remote objects

## Tests (`tests/test_rebalance.py`)
- two local backends; ingest to backend A only; `rebalance_to(B)` copies all blobs; `blob_storage` rows exist for both; checkpoint resume after simulated crash skips completed blobs
- `drain_backend(A)` with B healthy → all blobs present on B; dry-run reports bytes without copying
- `plan_rebalance` counts match anti-join query

## Verification
- onboard → rebalance → usage views show growth on new backend; drain → consistency clean → GC reclaims
- full suite green

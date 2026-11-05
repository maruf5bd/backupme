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
  output -> `diagnostics/storagebox_rescan_report.txt`.
- **Agent: defect fixes D1-D5** (config.py, web/routes.py, data_health.py).
- **Agent: B01 walk-fix port** to smb/ssh/ninedrive/agent backends.

## Pending user decisions (do NOT proceed without approval)

- Purge 7 SOURCE_GONE files that reference corrupt blobs (unrecoverable unless source reappears).
- Purge 725 unfixable zero-storage-row files.
- Purge 4 junk "archives" (a.zip, 2FA.7z, 2020-12-03 tar.gz, bitexchanger_v20.zip) — NOTE:
  2FA.7z vault copy is the LAST copy in existence (source deleted).
- Rotate the Storage Box password (it appeared in plaintext in some gitignored diagnostics
  scripts; config.yaml is the single allowed home).

## Where things stand overall

Dashboard + repair tooling are in good shape; the defect wave (D1-D5, B01) is the last
known correctness work before the wiring wave (B02-B04) and feature wave (B10-B14).

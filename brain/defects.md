# Defect register — 2026-11-05

From the black-box QA sweep (126 + 66 checks, report: `diagnostics/qa_dashboard_report.txt`).
Fix status updates go in the Status column; do not delete entries.

## Open / in flight

| ID | Sev | Defect | Evidence | Status |
|---|---|---|---|---|
| D6 | HIGH | **Phantom storage rows: uploader records blob_storage rows for backends where the bytes never landed.** All 323 storagebox rows (19.79 GB, uploaded_at 2026-11-04..05, the repair window) reference files that do NOT exist on the box — full corrected walk (86,154 files, 5.4 h) found ZERO objects//bundle/ paths, live `exists()` probes False. All 323 blobs exist on 3-4 OTHER backends, so no data is lost — but redundancy accounting lied, and any future drain/GC trusting those rows would be wrong. | `diagnostics/storagebox_rescan_report.txt`, `storagebox_crosscheck.txt`, `storagebox_redundancy.py` output | open — root-cause in uploader's record path (`_upload_single_blob`/`_record_chunk_upload` ordering, silent upload failures), then cleanup of the 323 phantom rows (needs user approval — production DB write) |

## Closed

| ID | Sev | Defect | Fix | Commit |
|---|---|---|---|---|
| D1 | HIGH | `web.token` in config.yaml silently ignored — admin API unauthenticated | `load_config()` parses the `web` section; 6 regression tests (token rejects bad bearer, pages stay public, CLI token wins) | `80e9d23` |
| D2 | HIGH | Health scan false `members_unreachable` for compressed bundles | scan judges member ranges against parent's PLAINTEXT `blobs.pt_size` (LEFT JOIN), never compressed size; `bundles_size_mismatch` bucket added; parent detection widened to `storage_path` prefix | `cc5d88a` |
| D3 | MED | Search 500s on punctuation (raw FTS5 MATCH) | `_fts_phrase()` quotes queries as single phrases; `_search_files_safe()` falls back to LIKE on sqlite errors; applied to all 3 search paths | `cc5d88a` |
| D4 | MED | `/api/browse` empty for forward-slash paths | prefix matching tries both separator styles; remainder + direct-file lookup normalized | `cc5d88a` |
| D5 | LOW | Unknown file id -> 500 | 404 + JSON error on download/preview; BONUS: download's `background` was a bare lambda (TypeError on every download) -> proper `BackgroundTask` | `cc5d88a` |

Tests: `tests/test_web_token_config.py` (6), `tests/test_web_defects.py` (14), expanded
`tests/test_web_health.py`. Full suite green at landing (exit 0).

## Class-bug watchlist

- Silent `except Exception: pass` in any `walk`/`list` — fixed for ftp/sftp (`6dcaa73`),
  port to smb/ssh/ninedrive/agent in flight (B01). After B01, grep the whole package for
  new occurrences in review.
- Config section drift (dataclass fields vs YAML keys) — B02 adds the pinning test.

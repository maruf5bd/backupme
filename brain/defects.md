# Defect register — 2026-11-05

From the black-box QA sweep (126 + 66 checks, report: `diagnostics/qa_dashboard_report.txt`).
Fix status updates go in the Status column; do not delete entries.

## Open / in flight

| ID | Sev | Defect | Evidence | Status |
|---|---|---|---|---|
| D6 | CLOSED | Phantom storagebox rows (details in update trail below; cleanup `0864d49`) | — | closed 2026-11-05 |
| D7 | RESOLVED 2026-11-07 | **"Healed" corrupt-blob files were verified by DB rows only — real downloads showed 5/10 broken.** Truth after full investigation: the corrupt9 repair DID work, but as NEW VERSION ROWS (files table versions by (source_root, relative_path, version_seq); the repair created v2/v3/v4 rows — e.g. jpg id 498 v1 is the old broken row, id 56642 v4 is the healed one). Download-verify of the LATEST version per path: **3/3 paths (5 files) RESTORE with sha match** (`verify_latest_versions.py`). The re-backup initially looked ineffective because (a) verification targeted the stale v1 ids and (b) `file_chunks` is keyed (file_id, seq) with `INSERT OR IGNORE` everywhere and `index_file` never deletes a previous version's chunk rows — a re-upload's new chunk is silently dropped while the old broken mapping survives (fixed in the repair tool with `replace_file_chunks()` = delete + unref old blobs before re-inserting; the CORE `index_file` path still has this flaw — tracked below as D8). Superseded v1 rows (498, 37915, 39457, 38043, 39585) are now ordinary purge candidates. | `verify_healed10_real.py`, `verify_latest_versions.py`, `check_d7_rows.py` | closed |
| D8 | RE-SCOPED 2026-11-07 (was misframed) | **Superseded v1-era rows carry malformed bundle chunk blob_ids** (e.g. `bundle/0d/98/0d98...5d.zstd` — a remote_path stored as a blob_id, written by the 2026-05-30-era uploader; the true member id `bundle/.../<membersha>` rows are healthy). Version semantics are CORRECT — v1 and v4 are independent rows keyed by id, so there is no (file_id, seq) collision and `index_file` needs no chunk-replacement change (rep0's `replace_file_chunks` is harmless hygiene for same-row re-runs). The malformed rows fail with `UnrecoverableChunkError: ... has no valid @` and cannot restore — they are superseded versions, already covered by the pending purge decision. Optional core hardening: a one-time migration normalizing `blob_id` values that look like remote paths (contain `.zstd`/`.aes` suffixes) to their true member ids, or simply purging superseded broken versions. | `check_d7_rows.py` output, verify error strings | re-scoped — no core change required; disposition via the pending purge approval |

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

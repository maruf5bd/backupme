# Defect register — 2026-11-05

From the black-box QA sweep (126 + 66 checks, report: `diagnostics/qa_dashboard_report.txt`).
Fix status updates go in the Status column; do not delete entries.

## Open / in flight

| ID | Sev | Defect | Evidence | Status |
|---|---|---|---|---|
| D6 | CLOSED | Phantom storagebox rows (details in update trail below; cleanup `0864d49`) | — | closed 2026-11-05 |
| D7 | RESOLVED 2026-11-07 | **"Healed" corrupt-blob files were verified by DB rows only — real downloads showed 5/10 broken.** Truth after full investigation: the corrupt9 repair DID work, but as NEW VERSION ROWS (files table versions by (source_root, relative_path, version_seq); the repair created v2/v3/v4 rows — e.g. jpg id 498 v1 is the old broken row, id 56642 v4 is the healed one). Download-verify of the LATEST version per path: **3/3 paths (5 files) RESTORE with sha match** (`verify_latest_versions.py`). The re-backup initially looked ineffective because (a) verification targeted the stale v1 ids and (b) `file_chunks` is keyed (file_id, seq) with `INSERT OR IGNORE` everywhere and `index_file` never deletes a previous version's chunk rows — a re-upload's new chunk is silently dropped while the old broken mapping survives (fixed in the repair tool with `replace_file_chunks()` = delete + unref old blobs before re-inserting; the CORE `index_file` path still has this flaw — tracked below as D8). Superseded v1 rows (498, 37915, 39457, 38043, 39585) are now ordinary purge candidates. | `verify_healed10_real.py`, `verify_latest_versions.py`, `check_d7_rows.py` | closed |
| D8 | MEDIUM | **`index_file` (new version of an existing file) does not delete the previous version's `file_chunks` rows; every chunk insert is `INSERT OR IGNORE` on PK (file_id, seq)** — so any re-upload of a changed/broken file via the version path leaves the file pointing at its OLD chunk mapping. The repair tool now works around it (`replace_file_chunks`), but the core path needs the same replace-and-unref semantics (delete old rows + unref their blobs) inside index_file. | rep0_rebackup.py `replace_file_chunks` (working workaround); check_d7_rows.py output | open — fix in core/index.py + uploader's same-version path, with a regression test (re-upload same path twice, chunk must follow the new blob) |

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

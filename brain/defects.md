# Defect register — 2026-11-05

From the black-box QA sweep (126 + 66 checks, report: `diagnostics/qa_dashboard_report.txt`).
Fix status updates go in the Status column; do not delete entries.

## Open / in flight

| ID | Sev | Defect | Repro | Status |
|---|---|---|---|---|
| D1 | HIGH | `web.token` in config.yaml silently ignored — admin API unauthenticated | tokenless GET `/api/files` -> 200 while token armed in config | fix agent in flight |
| D2 | HIGH | Health scan false `members_unreachable` for compressed bundles: member plaintext range (offset+len) compared against parent's COMPRESSED size | QA: 2/2 flagged members restored perfectly via `download_file` | fix agent in flight (compare against parent PLAINTEXT size) |
| D3 | MED | Search 500s on punctuation: raw `q` hits FTS5 `MATCH` (`fts5: syntax error near "."`) | `GET /api/files?q=delta.txt` -> 500 (also `/api/search`, `/files?q=`) | fix agent in flight (sanitize/quote + LIKE fallback) |
| D4 | MED | `/api/browse` empty for forward-slash paths (all cPanel data): handler builds backslash `LIKE 'sub\%'` | `GET /api/browse?root=R&path=sub` -> empty dirs/files | fix agent in flight |
| D5 | LOW | Unknown file id -> 500 instead of 404 | `GET /api/download/999999`, `/api/files/999999/preview` | fix agent in flight |

## Closed

(none yet — move entries here with commit hash once fixes land)

## Class-bug watchlist

- Silent `except Exception: pass` in any `walk`/`list` — fixed for ftp/sftp (`6dcaa73`),
  port to smb/ssh/ninedrive/agent in flight (B01). After B01, grep the whole package for
  new occurrences in review.
- Config section drift (dataclass fields vs YAML keys) — B02 adds the pinning test.

# Implementation Plans — cPanel Ingest Pipeline

Index of detailed phase plans. Master plan: `C:\Users\Maruf\.commandcode\plans\cpanel-ingest-dedup-pipeline.md`

| # | Plan file | Phase | Status |
|---|-----------|-------|--------|
| 1 | `plan-01-core-ingest.md` | Core ingest: `cpanel_ingest.py`, `index.next_version_seq`, config, CLI, tests | pending |
| 2 | `plan-02-db-lifecycle.md` | DB lifecycle: purge, cold export (Zstd Parquet), DuckDB history, `db_lifecycle.py` | pending |
| 3 | `plan-03-automation.md` | Daemon automation: scheduler ingest/purge/export ticks, single-flight guards | pending |
| 4 | `plan-04-web-gui.md` | Web GUI: `/ingest` page, APIs, nav, status wiring | pending |
| 5 | `plan-05-ops-docs.md` | Ops hardening, `ingest-status`, docs, verification | pending |
| 6 | `plan-06-adding-storage.md` | Adding storage: onboard, rebalance, drain, per-backend usage (CLI + GUI) | pending |

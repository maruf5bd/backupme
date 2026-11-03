# Plan 05 — Ops Hardening, Status, Docs

## Goal
Productionize: pacing refinement, `status` ingest summary, notifications, README/MANUAL chapters, full verification.

## Items
- Pacing token bucket refinement; staging budget enforcement tests at scale (simulated)
- `status` command (extend `list_cmd.py status` or `ingest-status`): cpanel ingest summary — generations, dedup ratio for `cpanel/` sources, SQLite size + Parquet size
- Docs:
  - README "cPanel Ingest" section: architecture + config + cPanel-side cron/rsync snippet + restore examples + DB topology/row-economics note (Zstd Parquet + DuckDB)
  - MANUAL chapter incl. the Ingest page
  - `config.yaml.example` comments for all new knobs
- Notifications: ingest failure → existing notifications config path (`op_log` + notification hook if configured)

## cPanel-side setup (documented, not code)
```bash
*/15 * * * *  rsync -a --remove-source-files /backup/cpmove-*.tar.gz backupme-server:/srv/drop/\$USER/
```

## Risks (documented, honest)
- MySQL dumps dedup poorly (daily full dumps differ): stored compressed + versioned; retention limits sprawl
- Staging holds plaintext: encrypted volume/tmpfs, 0700, immediate cleanup
- gzip CPU ~1 core/150MBs; Zstd export level 3 similarly cheap
- 9drive public-writer caveat: pipeline encryption (AES-256-GCM) covers blobs + raw archives
- >5GB members handled by 64MB chunking; `max_member_size` skip for pathological
- tar traversal: sanitized names, per-member rejection

## Verification (full)
1. `pytest tests/test_cpanel_ingest.py` + full suite green
2. manual e2e: real cPanel archive → dedup ratio → second gen near-zero bytes → `--retar` valid → optional test restore
3. crash test: kill mid-ingest → resume, no duplicate blobs
4. daemon test: auto-ingest → retention → export → purge → gc
5. GUI test: `/ingest` renders, scan/pause/resume work, history queries Parquet

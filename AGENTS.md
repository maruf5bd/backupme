# Memory

## Language (must follow)
- Always write in English. Use simple, clear English: short sentences and common words.
  This applies to everything an agent makes: chat replies, commit messages, code
  comments, docstrings, docs (README/MANUAL/config examples), plans, reports, progress
  notes, and prompts sent to subagents. Never write in any other language, in any file
  or message.

## Brain (living knowledge base)
The `brain/` folder is our memory. See @brain/README.md. It holds:
- `brain/status.md` — what is happening now (operational status)
- `brain/wiring.md` — what is wired up and how
- `brain/defects.md` — the list of known defects
- `brain/decisions.md` — decisions we made, and why
- `brain/lessons.md` — lessons we learned

How to use it: after every important finding, fix, or decision, add a note with the
date. Always add new notes at the end. Never rewrite old notes.

## Project Overview
See @README.md for the project overview. Commands are Python/CLI, not npm. Run tests
with `/c/Python313/python.exe -m pytest tests/ -q`. The CLI entry is `backupme` (see
docs/MANUAL.md §10 Command Reference).

BackupMe is a backup tool for Windows, written in Python 3.13. It removes duplicate
data. It has: a click CLI, a FastAPI web dashboard, storage backends you can plug in
(local, ftp, sftp, smb, ssh, rclone, 9drive), a hot store in SQLite, and a cold history
in monthly Zstd Parquet files (you query them with DuckDB).

## Code Style Guidelines
- Give variables clear names.
- Follow the patterns already used in the code.
- If a condition is complex, first put it into a boolean variable with a clear name.
- Import core modules inside the command body, not at the top of the file (this keeps
  CLI startup fast). At module level, set `log = get_logger("<module>")`.
- Tests use the `env`/`backends` fixtures from `tests/conftest.py` (each test gets its
  own config, DB, and local backend).

## Architecture Notes
- **One writer for SQLite** (`backupme/core/database.py`): WAL mode; each thread has
  its own connection. Every writer module (uploader, ingest, retention, lifecycle)
  commits its own work.
- **Dedup pipeline** (`core/uploader.py`): `upload_bundle` packs small files into tar
  bundles. It finds duplicate content by sha256 (donor lookup). The `blobs` table
  counts references (`dedup.add_ref` / `unref_blob`). When a bundle member is
  unreferenced, `unref_blob` also updates the parent bundle.
- **cPanel ingest** (`core/cpanel_ingest.py`): reads cpmove tar.gz members one at a
  time and sends them through `upload_bundle`. Use `tar.extractfile` only — NEVER
  `tar.extract`. `sanitize_member` rejects path traversal and absolute names, and
  turns backslashes into `/` (it checks traversal again after that). The staging file
  is deleted in `finally`. Each archive has an op_log idempotency key
  `cpanel_ingest:<sha256>`. On success the archive moves to
  `drop_root/.done/<account>/`. If ANY member fails, the op is marked failed and the
  archive stays in the drop root; the next ingest run resumes it and retries only the
  failed members (this was defect D21 — fixed and landed 2026-11-21, commit `e02b0ac`).
- **Bundle member blobs** are byte ranges inside the parent bundle
  (`storage_path` = `bundle@offset:len`). Any code that copies blobs (rebalance/drain)
  must skip 5-part `bundle/a/b/tar/mem` ids — copying the parent bundle already
  carries its members.
- **DB lifecycle** (`core/db_lifecycle.py`): `cold_export_to_parquet` moves `files`
  rows past an id watermark into `parquet/files_YYYY-MM.parquet` (zstd-3, pyarrow).
  `hard_purge_versions` deletes rows that are soft-deleted + past the grace period +
  below the watermark. `query_history_duckdb(sql, params)` answers SQL over the
  Parquet files.
- **Daemon** (`scheduler.py`): `_periodic_loop` runs backups → retention → ingest
  (every `scheduler.ingest_interval`) → purge (daily) → cold export (every
  `db_lifecycle.cold_export_after_days`) → gc → verify. Each tick has a non-blocking
  lock (only one can run at a time) and its own error guard. Ingest/purge/export run
  only if `cpanel_ingest.enabled` is true.
- **Rebalance** (`core/rebalance.py`): uses `mirror.replicate_to_n`. Progress is saved
  to `rebalance_checkpoint.json`. `drain_backend` never deletes. To retire a backend:
  lower its priority → remove it → run `gc --apply`.
- **Web** (`web/routes/` package): `register_routes(app, config)` in
  `routes/__init__.py` calls each domain module's `register(app, ctx)` — pages, files,
  backends, dbops, ingest, backup, config_api, scheduler, health, archives, misc.
  Shared helpers (RouteContext, search helpers, one background-task runner, op_log
  helpers, `reset_background_state()`) live in `routes/common.py`. The task-state
  dicts live in common.py but are reset at registration time, so each new app starts
  empty. Route order matters: register specific paths like `/api/backends/usage`
  BEFORE `/api/backends/{name}` (see backends.py).
- **Config** (`config.py`): one dataclass per YAML section, wired through
  `_filter_fields` (unknown keys warn but never crash). Sections: chunking,
  compression, encryption, verification, retention, database, notifications,
  ingest(cpanel_ingest), DBLifecycleConfig(db_lifecycle), SchedulerConfig(scheduler).

## Subagents (must follow)
- Give work to background subagents. Run at most 2 at the same time. The harness
  rejects a 3rd agent ("user concurrency limit exceeded"). Start 2; when one finishes,
  start the next instead of doing the work in the main thread.
- **Only one agent may WRITE the database at a time.** Every other agent running at
  the same time must only read (queries, reports, file lists). A job that only copies
  data (for example blob consolidation) does not count as a writer.
- Always start subagents with `run_in_background: true`, so the main thread keeps
  working and collects results when they arrive.
- Every subagent prompt must include: the shell limits (the startup script is broken:
  no parentheses in bash commands; use `command git`; write scripts with the Write
  tool, then run `cd /j/personal@hostraj.com/backupme && python script.py`), the
  English-only rule, and a clear "do not touch" list for running jobs and other
  agents' output files.

## Known broken (fix these first — ignoring is NOT allowed)
- **Workspace split — D23 (HIGH, open since 2026-11-18)**: TWO workspaces exist.
  `K:\backupme` (old layout, no git) is the production workspace, and its backupme.db
  (4.3 GB, ~547k files / ~370k blobs) is the LIVE production index. The J repo's
  backupme.db (2.6 GB) has had no op_log write since 2026-11-10. **All J-era numbers
  below (D14 counts, purge lists) are out of date until we re-check them against K's
  DB.** Key detail: K's driver script runs `python -m backupme.main` with cwd = the J
  repo — so production executes J's CURRENT working tree (including uncommitted
  changes), with K's config and K's database. Details: `brain/defects.md` D23. The
  owner still has to decide which workspace is the authoritative one.
  First re-based numbers (2026-11-19, row-coverage only — not physical existence):
  64,838 referenced blobs have no storage row (45,847 bundle parents + 18,991
  chunks, ~41 GiB plain), but only **1,527 of 292,198 live files** depend on them;
  99.5% of live files are fully row-covered. See brain/status.md update 34.
- **Gdrive backend / lost data — D14 (CRITICAL, open; root moved 2026-11-12; numbers
  are J-era, see D23)**: the hostraj Gdrive root in config is
  `H:\New folder\backupme` (the old `H:\Backupme` is almost empty — the move is
  recorded in status update 18). After the owner's overnight store cleanup,
  **35,967 referenced blobs (124.8 GiB of plain data) had no physical copy**.
  Partly fixed: 2,741 blobs (16.9 GB) were rebuilt from the owner's sha-verified
  restore tree (`b280fce`); 86.5 GiB was copied onto the Storage Box. The rest
  (~28-30k blobs / up to ~64k live files) cannot be recovered from BackupMe data —
  the plan was shown to the owner on 2026-11-10; the box-registration step waits for
  approval. Full details: `brain/defects.md` D14. Lesson from the past: an earlier
  "51,644 lost rows" estimate was WRONG — those are bundle-MEMBER rows
  (`storage_path = bundle/...@offset:len`) whose content lives inside the parent
  tars; reachability checks must resolve members through the parent bundle tar (the
  "1 bundle lost, id=25554" finding replaced the estimate). Parent + member
  restore-metadata rows are NEVER purge candidates. Full analysis:
  `diagnostics/root-sweep-2026-11-15/gdrive_lost_impact.txt`.
- **24 corrupt stored files** at old roots `E:\hostraj\backupme` and
  `H:\New folder\backupme` (zstd frame damage) — the hash-checked consolidation
  refused them; they touched 17 live files. 10 of them are FIXED: re-backed through
  the real pipeline (including the 1.07 GB Mikrotik vmdk, 16 chunks through Storage
  Box SFTP), verified in the DB 10/10 healed, 0 orphan chunks
  (`diagnostics/root-sweep-2026-11-15/corrupt9_plan.json`). The other 7 files: their
  sources are deleted (SOURCE_GONE) and they still point at corrupt blobs — they
  cannot be recovered unless the source comes back; they sit in the dashboard purge
  list. Never copy the corrupt files from the old roots without a decompress-hash
  check.
- **Zero-storage-row blobs**: 1,303 referenced blobs had no physical copy. FIXED:
  791 files re-backed through the real pipeline (new versions), 3 chunked objects
  healed on all 4 backends, 104 were duplicates; refcount drift 0; restore tests 3/3.
  The remaining 725 rows cannot be fixed (716 SOURCE_GONE including recycle-bin,
  9 SOURCE_CHANGED dev files) — waiting for the user's purge decision
  (`diagnostics/root-sweep-2026-11-15/rep0_report.txt`). On K's production DB this
  class is much bigger (64,838 referenced blobs with no storage row, before resolving
  members through parent tars — see brain/status.md update 32).
- **4 junk "archives"** (a.zip 484 B, 2FA.7z 192 B, the 2020-12-03 tar.gz 1,861 B,
  bitexchanger_v20.zip 414 B): the vault copies exist but the content cannot be
  extracted; every blob has ref_count=1 and no other file references them. The purge
  plan is ready in `diagnostics/root-sweep-2026-11-15/junk_archives_impact.txt` —
  NOTE: 2FA.7z is the LAST copy in existence (its source is deleted). Waiting for the
  user's decision; never purge silently.
- Rule: never skip, silence, or work around a failing part. Fix the root cause first.
  If it truly cannot be fixed in this session, write it here with the root cause and
  what would unblock it, then get the user's clear approval before going ahead
  without it.

## Common Workflows
- Run all tests: `/c/Python313/python.exe -m pytest tests/ -q` (pytest.ini adds
  `-q --basetemp=test_tmp_backend/pytest`)
- Run one file: `/c/Python313/python.exe -m pytest tests/test_cpanel_ingest.py -q`
- Git: the shell has broken `_lc` aliases for `git`/`pytest`/`grep` — always use
  `command git`. Prefer `/usr/bin/grep`, and write command output to a file, then
  read the file.
- Full CLI smoke test: `backupme ingest-cpanel <drop_root> --dry-run --json` must
  exit 0 and parse as JSON.
- Ingest end-to-end (the tests show the pattern): build a tar.gz with `homedir/*` +
  `mysql/*` members, put it under `drop/<acct>/`, run `cpanel_ingest.ingest_tree`,
  then check rows exist under `source_root='cpanel/<acct>'`; a second identical
  archive is skipped; a changed archive dedups.
- Cold storage end-to-end: `backupme db cold-export` → `backupme db purge` →
  `backupme db history "SELECT ... FROM files ..."`.
- Add and fill a backend: `backupme backend add` →
  `backupme backend onboard NAME --rebalance`. To retire one:
  `backupme backend drain NAME`, remove it from the config, then `backupme gc --apply`.

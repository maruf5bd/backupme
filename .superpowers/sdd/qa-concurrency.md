# QA Report — Concurrency & Correctness Review (Reviewer B)

Project: BackupMe (J:\personal@hostraj.com\backupme)
Scope: `backupme/scheduler.py`, `backupme/web/routes.py`, `backupme/core/cpanel_ingest.py`,
`backupme/core/rebalance.py`, `backupme/core/db_lifecycle.py` (+ supporting: `database.py`,
`schema.py`, `op_log.py`, `state.py`, `index.py`, `uploader.py`, `dedup.py`, `gc.py`,
`mirror.py`, `cli/db_cmd.py`, `cli/backend_cmd.py`, `web/templates/ingest.html`, `docs/`).
Mode: read-only review + test execution. Date: 2026-11-04.

## Test execution evidence

Command: `/c/Python313/python.exe -m pytest tests/test_scheduler_automation.py tests/test_web_ingest.py -q`

- Run 1: **19 passed, 1 failed** — `tests/test_web_ingest.py::test_api_ingest_scan_flow`
  (`assert n == 3` → `0 == 3`). Captured log shows `ingest_archive failed ...
  FileNotFoundError: ...drop\acct\cpmove-acct.tar.gz` — the archive vanished from the drop
  root before the web worker could open it, i.e. another in-process consumer moved it. This
  is a live observation of the missing cross-component single-flight (H1/H6).
- Run 2 (same command): cascading teardown failures —
  `PermissionError: [WinError 32] The process cannot access the file because it is being
  used by another process: ...test_gen2_changed_archive_dedu0\bkenv\drop\acct\cpmove-acct.tar.gz`
  repeated for 8 subsequent tests. A leaked background thread holds an open handle on the
  archive while pytest's tmp cleanup runs. Live evidence of the Windows file-locking
  findings (H4) and of ingest threads outliving their owner (H5).
- Run 3 (single test in isolation): passes. Confirms order/leak-dependent flakiness.

`test_scheduler_automation.py::test_periodic_loop_runs_all_ticks_once` (lines 140–174)
explicitly asserts that with all `_last_*` at 0.0 one loop pass fires every tick — so the
"first start fires everything" behavior is **intended and test-pinned** (see M1 for why it
is still risky).

---

## Findings

Severity scale: Critical (silent permanent data loss / corruption reachable in normal ops),
High (corruption/availability loss under plausible concurrency or on the target OS),
Medium (incorrect behavior, bounded loss, or requires specific timing), Low (robustness/hygiene).

---

### CRITICAL

#### C1. Cold export: cross-process overwrite of the monthly partition + watermark lost update → permanent history loss after purge

File: `backupme/core/db_lifecycle.py:119-196` (esp. 121, 123-129, 161-178, 181-196)

Two processes can run `cold_export_to_parquet` concurrently: the scheduler tick
(`scheduler.py:220-222,344-359`) and the manual CLI (`backupme/cli/db_cmd.py:116-138`,
`db cold-export`) — there is no cross-process lock (the scheduler's `_export_lock` is
per-instance only; the CLI has none). Both then:

1. Read the same watermark W (`db_lifecycle.py:121`).
2. `SELECT ... WHERE id > W` — overlapping row sets (123-129).
3. Write the **same partition path** `files_YYYY-MM.parquet` with `pq.write_table`
   (161-178) — a full-file write, not append and not tmp+rename. Last writer wins: rows the
   loser exported are **removed from the partition file**.
4. Read-modify-write `_manifest.json` (181-196): lost update — the lower watermark can win,
   or the higher one, depending on interleaving.

Consequences:
- If the process with the *smaller* row set writes the partition last, previously exported
  rows are silently dropped from Parquet.
- If the manifest ends with the *lower* watermark, the next export re-exports rows already
  in the partition and concatenates them (164-166) → **duplicate rows** in DuckDB history
  queries (`/api/ingest/history`, `query_history_duckdb`).
- The in-code comment at 159-160 ("no duplicates possible") is only true for a single
  process.
- Worst case: rows dropped from the partition are still in SQLite until
  `hard_purge_versions` (212-255) hard-deletes them once the watermark passes — after which
  the only copy of that history is gone. **Permanent, silent data loss in a backup tool.**
- Additional hazard: `pq.write_table` writes the final path directly, so a concurrent
  DuckDB reader (`query_history_duckdb`, 262-303) can read a half-written file, and a crash
  mid-write leaves a corrupt partition that the next export cannot `read_table` (165).

Suggested fix: serialize exports across processes (e.g. an exclusive `msvcrt.locking`/
`filelock` on `parquet/.export.lock`), write the partition to a temp file + atomic rename,
and re-read the watermark *and* max id inside the lock before appending. Alternatively
write one immutable partition per run (`files_YYYY-MM_<uuid>.parquet`) and keep the
manifest as a list of parts.

---

### HIGH

#### H1. Single-flight is per-component only: web ingest worker and scheduler tick can ingest the same archive concurrently, and the op_log "idempotency" does not stop in-flight duplicates

Files: `backupme/web/routes.py:2297-2449` (`_ingest_state`, `_ingest_worker`,
`api_ingest_scan`), `backupme/scheduler.py:156-166,305-325` (`_ingest_lock`),
`backupme/core/cpanel_ingest.py:313-330,332-333,486-488`, `backupme/core/op_log.py:8-29`,
`backupme/core/schema.py:84`.

- The scheduler guards ingest with its instance-level `self._ingest_lock`
  (scheduler.py:162,309). The web worker guards with the closure-local
  `_ingest_state["running"]` flag (routes.py:2443-2445). **Nothing is shared between the
  two components.** A manual `/api/ingest/scan` overlapping a scheduler tick (default
  ingest cadence: first tick fires immediately at daemon start, then every
  `max(interval, ingest_interval)`) runs two ingests of the same drop root in one process.
- The pre-check in `ingest_archive` only skips when the op is already `'committed'`
  (cpanel_ingest.py:317-323). While the first runner is mid-flight its op row is
  `'pending'`; the second runner's `op_log.append` finds that pending row and *returns it*
  (op_log.py:16-20) instead of failing — so both runners proceed concurrently on the same
  archive.
- Both runners derive the **same staging dir** `staging/<sha12>` (cpanel_ingest.py:332-333)
  and write the same member paths (`_extract_member_to_staging`, 265-274). One runner's
  `finally` unlink (392-399) can delete the file the other is hashing/uploading
  (cpanel_ingest.py:386-388): uploads of truncated/planted-partial files, or
  `FileNotFoundError` mid-hash. On Windows the unlink of an open file raises
  `PermissionError` (caught, but plaintext then leaks in staging).
- Both runners call `index_mod.next_version_seq` then insert with
  `UNIQUE(source_root, relative_path, version_seq)` (schema.py:37) → `IntegrityError` for
  the loser (see H2 for how badly that failure is handled).
- Both runners execute `shutil.move(archive_path, .done)` (486-488); on Windows moving a
  file that the other runner holds open (tarfile) raises `WinError 32` → the loser marks
  the archive failed — and since **both share the same op row**, the loser's
  `mark_failed(op["id"])` (444-446) can overwrite the winner's `mark_committed` (439),
  flipping a successful ingest to `failed` → the next tick re-ingests everything
  (duplicate versions, storage bloat).
- Duplicate-content archives in one drop root (same sha256, different paths) hit the same
  path *within a single `ingest_tree` run* via its ThreadPoolExecutor
  (cpanel_ingest.py:546-558) — no sha-dedup of the discovery list.

The test-suite flake in Run 1 (FileNotFoundError on the drop archive) is consistent with
exactly this class of overlap.

Suggested fix: use one process-wide, module-level single-flight for ingest (e.g. a
`threading.Lock` + an inter-process lock file in the staging root) acquired by BOTH the
web `api_ingest_scan` path and the scheduler `_run_ingest`; make the op_log gate
status-aware (treat an existing *pending* row younger than a timeout as "in progress,
skip"), and namespace the staging dir per op_log id (not just sha).

#### H2. No rollback anywhere in the upload path + thread-local connections → stale open write transactions block all writers, and trailing commit publishes partial batches

Files: `backupme/core/uploader.py:317-461` (`_flush_batch`, no rollback), `384-445`
(INSERTs with `auto_commit=False`), `597-605` (per-file `except` logs only; final
`bump_generation()`/`commit()` at 604-605 commit anything left open),
`backupme/core/database.py:10-37` (thread-local connections),
`backupme/core/schema.py:2-7` (`busy_timeout = 5000`).

- All DB writes across the codebase go through `execute()` on a **thread-local**
  connection (database.py:70-79). SQLite (default `isolation_level=""`) opens an implicit
  write transaction on the first DML and holds it until `commit()`. `grep` confirms there
  is **no `rollback()` call** in `uploader.py`, `index.py`, or `cpanel_ingest.py`.
- If `_flush_batch` raises after its INSERTs (e.g. the `IntegrityError` from H1, or any
  backend/IO error between INSERT and commit), the transaction stays open on that worker
  thread's connection. Inside the per-file loop the exception is swallowed (597-601) and
  the final `bump_generation()`+`commit()` (604-605) then **commits the half-done batch**:
  bundle/blob_storage rows without file rows or chunk mappings, skewed `ref_count`s
  (`dedup.add_ref` ran for some members only).
- If the exception escapes from the final `_flush_batch()` (line 603, outside the per-file
  try), `ingest_archive` marks the archive failed and the **thread returns to the
  ThreadPoolExecutor pool with an open write transaction**. Every other writer (other
  ingest workers, web UI writes, scheduler ticks) then hits
  `sqlite3.OperationalError: database is locked` after the effective 5 s busy timeout
  (see M3) for as long as that thread idles with the txn open.
- WAL does not save you here: WAL allows concurrent *readers*, but there is still exactly
  one writer, and an abandoned writer transaction blocks everyone.

Suggested fix: wrap every multi-statement unit in `try/except: rollback(); raise`
(a contextmanager `write_txn()` in `database.py` used by `_flush_batch`,
`index_file` callers, `apply_retention`, `hard_purge_versions`), and close/rollback
thread-local connections when pool threads are recycled.

#### H3. `rebalance_checkpoint.json` is not namespaced by target backend and is clobbered by concurrent rebalances (web + CLI)

Files: `backupme/core/rebalance.py:20-21,49-64,82-110`,
`backupme/cli/backend_cmd.py:320-345` (CLI rebalance, no guard),
`backupme/web/routes.py:2649-2670` (web rebalance, per-web running flag only).

- `CHECKPOINT_FILE = Path("rebalance_checkpoint.json")` — one global file in CWD, no
  target key. `rebalance_to` loads `completed` from it (82-83) and **skips any blob_id in
  that list before the anti-join is consulted** (93-94). Scenario: rebalance to backend A
  completes (checkpoint holds A's completed ids) → later rebalance to backend B: B's
  missing blobs that were already copied to A are in `completed` → **silently skipped** →
  `rebalance_to` returns `status: ok` while B is missing all of them. If A is then
  drained/removed and `gc --apply` runs, those blobs are gone — permanent loss.
- Two concurrent rebalances (web + CLI is the realistic pair; the web 409-guard at
  routes.py:2653-2655 only sees web state, and the CLI has none): both load the same
  checkpoint, both keep independent in-memory `completed` lists, both `_save_checkpoint`
  (61-64, 107-110) — last writer wins, erasing the other's progress; both also write the
  same `rebalance_checkpoint.tmp` (62), which on Windows raises `PermissionError` when the
  second process opens it while the first holds it.
- `_save_checkpoint({"completed": completed[-1000:], ...})` (108, 110) keeps only the last
  1000 ids, while in-memory `completed` grows unbounded — a crash after >1000 blobs loses
  earlier entries (mostly mitigated by the anti-join, but see the skip bug above).

Suggested fix: key the checkpoint by target (`rebalance_checkpoint.<target>.json`) or store
`{"target": ..., "completed": [...]}` and validate `target` on load; take an exclusive
file lock around load/save; drop the skip-if-in-completed shortcut in favor of the
blob_storage anti-join (it is already authoritative), or verify per-skip with a
`blob_storage` lookup for *this* target.

#### H4. Windows file locking: shutil.move / unlink of files with open handles (drop archive, staging members) — demonstrated by the test run

Files: `backupme/core/cpanel_ingest.py:392-399` (staging unlink in `finally`),
`465-488` (keep_raw upload + `shutil.move`), `backupme/scheduler.py:275-281`
(`os.remove` on move-mode files), `backupme/core/uploader.py:466` (`_long_path` exists
here but is not used in the ingest path).

- CPython opens files on Windows without `FILE_SHARE_DELETE`, so `os.rename`/`os.replace`/
  `shutil.move`/`Path.unlink` of a file another handle has open fail with
  `PermissionError: [WinError 32]`. The pytest Run 2 output shows exactly this on the
  drop archive (`...cpmove-acct.tar.gz ... being used by another process`) and the cascade
  it causes.
- In `ingest_archive` the failure path is nasty: `shutil.move` (488) is *outside* the main
  try (it runs after `mark_committed`), so a `WinError 32` there propagates out of
  `ingest_archive` uncaught (in the web worker it becomes a generic "failed" result at
  routes.py:2366-2368) — the archive stays in the drop root and is re-ingested next tick
  (duplicate generation), while the `.done` move never happens.
- Same-locking applies to the staging member unlink (392-399): under H1 two runners share
  the staging path, so one unlink hits an open handle; the `except OSError` only logs, and
  plaintext then persists in staging indefinitely (there is no staging sweeper anywhere —
  `check_staging_budget` only *measures* it, cpanel_ingest.py:194-215).

Suggested fix: retry-with-backoff on `PermissionError` for the `.done` move and staging
unlinks; move the `.done` move inside the guarded region with a distinct outcome
(`"ingested_but_move_failed"`); add a startup/tick sweeper that removes staging dirs older
than N hours; ensure only one component ever owns a given archive (H1).

#### H5. An unhandled exception in the periodic loop kills the daemon thread silently; `stop()` + immediate `start()` can leave two live schedulers

Files: `backupme/scheduler.py:208-229` (`_periodic_loop` — no try/except around
`_run_backups`/cadence calls), `275-297` (`_run_backups`: `fp.stat()` at 288 and
`collect_changes` unprotected), `202-206` (`stop()` joins with `timeout=10`),
`backupme/web/routes.py:1420-1454` (`api_daemon_start` TOCTOU, `api_daemon_stop`).

- `_periodic_loop` calls `self._run_backups()` bare. Any exception there — the classic
  being `fp.stat()` (scheduler.py:288) on a file deleted between `collect_changes` and the
  batch loop, or a long-path `OSError` on Windows — propagates and **terminates the daemon
  thread**. There is no restart logic; `/api/daemon/status` (1451-1454) will honestly
  report `running: false`, but nothing alerts or restarts.
- `stop()` sets the event and joins with `timeout=10` (202-206). If a tick is mid-flight
  (an ingest of a big drop root can run for hours), the join times out, `/api/daemon/stop`
  still returns ok and sets `_scheduler = None` (routes.py:1445-1447), and a subsequent
  `/api/daemon/start` creates a **second scheduler while the first loop is still executing
  its current pass**. The per-instance locks do not protect across instances → exactly the
  H1 double-ingest, plus double GC/verify/purge.
- `api_daemon_start` itself has a check-then-act race (1431-1434): two rapid POSTs can both
  pass the `is_alive()` check and start two schedulers.

Suggested fix: wrap the entire loop body in `try/except Exception: log.exception(...)`;
make `stop()` signal "abort current pass" (check `self._stop` between archives in the
ingest path) and refuse `start()` while the old thread is still alive (keep the reference
until join succeeds, not just 10 s); serialize `api_daemon_start` with a lock.

#### H6. Pause semantics: paused does NOT stop the current archive, does NOT stop the scheduler, and the SSE stream never reports paused

Files: `backupme/web/routes.py:2313,2345-2351,2451-2459` (`_ingest_paused`, worker pause
check, pause/resume endpoints), `2385-2433,2461-2482` (status vs stream), 
`backupme/scheduler.py:305-325` (`_run_ingest` has no pause check),
`backupme/web/templates/ingest.html:173-174`, `docs/CPANEL-INGEST-MASTER.md:135,167,175`.

- The worker checks `_ingest_paused` **only between archives** (routes.py:2345-2351). The
  current `ingest_archive` — potentially thousands of members / hours — always runs to
  completion after pause. That part is defensible, but it is nowhere documented.
- More importantly, the docs say pause/resume "toggle **daemon** ingest task"
  (docs/CPANEL-INGEST-MASTER.md:135,167,175). The implementation cannot do that: the
  scheduler's `_run_ingest` never reads `_ingest_paused` (scheduler.py:305-325), so a
  paused UI keeps seeing archives ingested by the daemon tick.
- The SSE endpoint `/api/ingest/stream` serializes `json.dumps(_ingest_state)`
  (routes.py:2466-2468) — the `paused` flag lives only in the `_ingest_paused` Event and is
  never merged into the state dict, so SSE consumers can never see `paused: true`. The
  status endpoint reads it separately (2425) and the ingest page uses polling
  (ingest.html:145,173-174), so the page happens to work — but the documented SSE mirror of
  `/api/backup/scan/stream` is inconsistent (the scan/restore states carry everything in
  the dict; ingest does not).
- Pause also defers archives by appending `{"status": "deferred"}` without incrementing
  `archives_done` (2347-2351) — after a pause+drain the queue shows e.g. `0 / 5` done
  forever with `completed: true`; the deferred archives are silently dropped (not retried
  until the next scan).

Suggested fix: check `_ingest_paused` inside the scheduler `_run_ingest` too; include
`"paused": _ingest_paused.is_set()` in the state dict (set under `_ingest_lock`); document
"pauses before the next archive; current archive completes"; either count deferred
archives or surface them in `errors`.

---

### MEDIUM

#### M1. Cadence math: fire-all-on-first-tick is intended (test-pinned) but the loop conflates period with interval, and a long pass defers every other task

File: `backupme/scheduler.py:157-161,208-229`; test
`tests/test_scheduler_automation.py:140-174`.

- `_last_* = 0.0` → first pass fires ingest+purge+export+gc+verify back-to-back after the
  first `_run_backups`/`_run_retention`. Intended (asserted by
  `test_periodic_loop_runs_all_ticks_once`), but on a large install daemon start triggers a
  full cold export of the entire `files` table + GC + purge + verify + full drop-root ingest
  in one thread — an uncontrolled startup storm.
- All five ticks run **sequentially** in the loop thread. If ingest takes 40 min, purge/
  export/gc/verify start 40 min late; effective cadence of every task is
  `interval + total_pass_time`, and `ingest_interval` (default 300 s) is unreachable
  whenever `interval` (default 3600) is larger — the configured 5-minute ingest cadence can
  never happen with defaults. Conversely if a pass takes longer than `interval`, the loop
  still waits the full `interval` afterwards — cadence silently drifts by pass duration.
- `_last_ingest = now` is stamped *before* the task runs (214-228): tasks that over-run
  their own interval drift progressively earlier relative to wall-clock intent.

Suggested fix: stamp `_last_*` after completion (`time.time()` at task end); compute next
due per task and `wait(min(next_due))` instead of a fixed `interval`; stagger the
first-run ticks (e.g. initialize `_last_export`/`_last_gc` to "now" on start, or spread
them over the first intervals).

#### M2. Check-then-act races on "is it already running?" in web endpoints

File: `backupme/web/routes.py:2443-2448` (ingest scan), `2650-2658` (rebalance),
`2661-2669` (drain), `684-697` (restore all), `2250-2266` (scan drive), `1422-1437`
(daemon start).

Pattern: `with lock: if running: 409` … then *release*, mutate state, and `Thread(...).start()`.
Two concurrent POSTs can both observe `running == False` (the first worker has not yet set
`running = True`) and both spawn workers → double ingest/rebalance/restore (compounding
H1/H3). The window is small but these are exactly the endpoints a UI double-click hits.
Fix: set `running = True` inside the same critical section as the check (as
`_reset_rebalance` nearly does — it is just called after the lock is released).

#### M3. Effective SQLite busy timeout is 5 s, not the 60 s `connect(timeout=...)` suggests; writer contention under parallel ingest is therefore sharp

File: `backupme/core/schema.py:4` (`PRAGMA busy_timeout = 5000`),
`backupme/core/database.py:30-36` (pragmas applied after connect, overriding
`sqlite3.connect(path, timeout=60)`).

Under H1/H2 (two ingest runners + web writes + retention), any write txn longer than 5 s
makes every other writer fail fast with `database is locked` instead of queueing for 60 s.
Either raise the pragma to match `timeout=60` or wrap writes in a retry helper; also note
`synchronous = FULL` in WAL makes each commit fsync — with per-member commits
(`upload_bundle` commits per bundle/`unref_blob` commits per call, dedup.py:24/68) ingest
is commit-heavy.

#### M4. `next_version_seq` check-then-insert race across parallel archives of the same account

Files: `backupme/core/index.py:132-144`, `backupme/core/cpanel_ingest.py:374-377`,
`backupme/core/schema.py:37` (`UNIQUE(source_root, relative_path, version_seq)`).

`ingest_tree` runs up to `max_parallel` (default 2) archives concurrently
(cpanel_ingest.py:544-558). Two archives that map to the same account (e.g. via
`account_pattern`) and contain the same member name both compute `MAX(version_seq)+1` and
both insert → `IntegrityError` on the loser, which (per H2) fails the whole archive *after*
the bundle was uploaded and leaves a partial batch committed by the trailing commit.
Fix: compute `version_seq` inside the insert (`INSERT ... SELECT COALESCE(MAX(version_seq),0)+1 ...`)
or retry the (seq) assignment on IntegrityError.

#### M5. GC orphan sweep can delete objects that an in-flight upload has not yet committed rows for

Files: `backupme/core/gc.py:41-58` (`find_orphans` → `bk.delete`), 
`backupme/core/uploader.py:349-368` (backend upload happens *before* the DB commit at 445),
`backupme/scheduler.py:361-374` (gc tick), `backupme/web/routes.py:899-969,2242-2266`
(web uploads run concurrently with the scheduler thread).

The uploader uploads the bundle to backends first and commits `blob_storage` rows later.
A concurrent `gc --apply` (web-driven verify/report actions, CLI, or the scheduler tick
while the *web* upload worker runs) sees the just-uploaded remote object as an orphan and
deletes it; the uploader then commits rows pointing at a deleted object. The scheduler
itself runs gc sequentially after backups in the same thread, but web-initiated uploads
(`api/backup`, `api/backup/scan`, `api/upload/file`) run on their own threads and are not
serialized against the gc tick. Fix: grace period (ignore objects younger than N hours —
`first_seen`/upload timestamp already exists) or a shared "backup in progress" flag/file
that gc respects.

#### M6. `backupme.state.json` checkpoint read-modify-write is not thread-safe and a save failure inside the ingest try marks the whole archive failed

Files: `backupme/core/state.py:10-21,38-50` (`save_checkpoint`: `_load_raw` → mutate →
`_write_atomic` to a **shared** `backupme.state.tmp`), 
`backupme/core/cpanel_ingest.py:404-412,430-437` (called from ThreadPoolExecutor workers
every 25 members and at completion).

Two ingest workers (or web + scheduler runners) race: both read the file, both write the
same tmp path (interleaved writes → corrupt JSON → `tmp.replace` installs a corrupt state
file; next `_load_raw` silently resets all checkpoints), and the loser's updates are lost.
Worse, `save_checkpoint` is called inside `ingest_archive`'s main try — any `OSError`
(e.g. Windows tmp-file contention, disk full) is caught by the outer handler
(cpanel_ingest.py:440-450) and **marks the whole archive failed** after all members were
already uploaded. Fix: add a lock inside `save_checkpoint`, use a unique tmp name
(`f".tmp-{os.getpid()}-{threading.getpid()}"`), move checkpointing outside the guarded
try, and tolerate checkpoint failures as warnings.

#### M7. Windows long paths: the ingest pipeline never applies the `\\?\` prefix that the rest of the codebase does

Files: `backupme/core/cpanel_ingest.py:265-274` (staging extraction — plain paths),
`332-333,483-488` (staging dir, `.done` move), `84-123` (`discover` walk);
contrast `backupme/web/routes.py:2060-2075` (`_lp`/`_safe_stat`) and
`backupme/core/uploader.py:466` (`_long_path`).

cpmove member trees (`homedir/mail/<domain>/<user>/...`) routinely exceed 260 chars. On
Windows the staging write raises `OSError` for a too-long member; the exception bubbles
through the member try (which has only a `finally`) into the archive-level handler → the
**entire archive is marked failed** because one deep member could not be staged. Same for
a drop root placed on a deep/network path (discover/move). Fix: route staging paths,
`archive_path.resolve()`, and the `.done` target through the existing `_long_path` helper;
skip-and-warn (like the oversize-member path at 360-364) instead of failing the archive.

#### M8. Cold-export partition read/append is not crash-atomic

File: `backupme/core/db_lifecycle.py:163-178`.

Even single-process, `pq.write_table` truncates the live partition: a crash (or power
loss) mid-write leaves a corrupt `files_YYYY-MM.parquet`, while the manifest still holds
the old watermark → the next export's `pq.read_table(partition_path, schema=schema)`
(165) raises on the corrupt file and cold export never succeeds again (manual repair
required). Fix: write to `partition.tmp` + atomic `os.replace`, and validate/fall back to
re-export on unreadable partitions.

---

### LOW

#### L1. `/api/backends/{name}/rebalance` silently drops the `limit` parameter

File: `backupme/web/routes.py:2656-2658` — `limit = data.get("limit")` is read but never
passed to `_rebalance_worker`/`rb.rebalance_to` (CLI `backend rebalance --limit` does pass
it, backend_cmd.py:339). Either wire it through or reject it.

#### L2. `rebalance` progress_cb counts skipped rows as progress

File: `backupme/core/rebalance.py:92-106` — `progress_cb(blob_id, i+1, len(rows))` fires
for checkpoint-skipped rows too; the web progress bar races to 100% without copying
anything. Cosmetic but misleading during incident triage.

#### L3. `_write_generation_manifest` is a non-atomic write

File: `backupme/core/cpanel_ingest.py:286-291` — `path.write_text(...)` directly to the
final manifest name; a reader (or a concurrent same-sha ingest) can observe a partial JSON.
Use tmp + `replace` like `state.py` does.

#### L4. `prune_oplog` concurrent duplicate archive lines

File: `backupme/core/db_lifecycle.py:317-341` — two processes pruning the same month both
SELECT the same rows and both append to `op_log_YYYY-MM.jsonl` (duplicated lines; deletes
are idempotent). Harmless but corrupts the JSONL archive; take a lock or `DELETE ... RETURNING`
semantics per row.

#### L5. `_emit_scan` / `_emit_restore` / `_scan_event` / `_restore_event` are dead code with a set-then-clear race

File: `backupme/web/routes.py:614-619,2056-2058` — `e.set(); e.clear()` is a no-op and
nothing waits on these events; all "streaming" is polling-based. Remove them to avoid the
impression of a signaling mechanism.

#### L6. Per-client SSE loops serialize on the state locks and hold them during `json.dumps`

File: `backupme/web/routes.py:699-718,2268-2288,2461-2482,2672-2693` — every connected
client every 0.5 s takes `_ingest_lock`/`_scan_lock`/`_rebalance_lock` and dumps the whole
state (including up to 5000 `results` entries for scan). With several open dashboard tabs
this stalls the ingest progress callbacks under lock contention. Snapshot under lock,
dump outside.

#### L7. Staging budget check is TOCTOU and computed per archive submission

File: `backupme/core/cpanel_ingest.py:548-558` — `check_staging_budget` walks the staging
tree for every archive while other pool workers are actively writing staging files; the
measured "used" is always stale, so N workers can each pass the gate and overshoot the
budget together. Bounded by `max_parallel`, acceptable, but worth a comment or a
reservation counter.

#### L8. Thread-local connections are never closed for FastAPI's thread pool

File: `backupme/core/database.py:13-27` with `routes.py` sync endpoints — each anyio
worker thread creates a connection cached for the thread's lifetime; `close_connection()`
is only called in tests. Bounded by the pool size (default 40) but each connection holds
WAL read marks that delay checkpointing; consider `close_connection()` in a pool
lifespan hook.

---

## Answers to the specific hunt questions

1. **Two background threads (web worker + daemon tick) ingesting simultaneously**: yes, it
   happens — single-flight is enforced per component only (`_ingest_lock` in the scheduler
   instance, `_ingest_state["running"]` in the web closure; nothing shared). With WAL +
   thread-local connections the DB itself serializes writers, but the application-level
   guards fail: shared pending op row lets both run the same archive (H1), shared staging
   dir corrupts member staging, `IntegrityError`s from `files`/`op_log` UNIQUE constraints
   hit a code path with no rollback and a trailing commit (H2), and the busy timeout is
   effectively 5 s (M3).
2. **Pause/resume**: the paused worker does *not* stop the current archive (checks only
   between archives, routes.py:2345-2351); the scheduler tick ignores the pause flag
   entirely (scheduler.py:305-325); the SSE stream never reports `paused`; and the docs
   promise "toggle daemon ingest task" (docs/CPANEL-INGEST-MASTER.md:135,175) — documented
   behavior is NOT consistent with the implementation or between SSE and `/api/ingest/status` (H6).
3. **Cadence math**: `_last_* = 0.0` → first pass fires all five ticks; this is intended
   and pinned by `test_periodic_loop_runs_all_ticks_once`, but it is a heavy startup storm
   and, because all ticks run sequentially in one thread, `ingest_interval` < `interval` is
   unreachable and a long pass defers every other task (M1). If the pass exceeds
   `interval`, the loop still waits a full `interval` afterwards — cadence silently
   stretches to `interval + pass_time`.
4. **Checkpoint file**: yes — `rebalance_checkpoint.json` is a single global file; web +
   CLI rebalances clobber each other (last-writer-wins, shared `.tmp` path that
   `PermissionError`s on Windows), and worse, the checkpoint is not namespaced by target
   backend so a previous target's `completed` list silently skips blobs for the new target
   while reporting `ok` (H3).
5. **Cold export same-month, two processes**: yes — lost update on `_manifest.json`
   (read-modify-write without a lock) and last-writer-wins overwrite of the shared
   `files_YYYY-MM.parquet` (no append, no tmp+rename) → lost rows (permanent after purge)
   or duplicate rows on the next export (C1).
6. **`progress_cb` mutation paths**: verified — every mutation of `_ingest_state`
   (`current_member`, `members_done`, `members_total`, `current_archive`, `archives_done`,
   `results`, `errors`, `running`) happens under `_ingest_lock`
   (routes.py:2325-2329, 2335-2383), and the status/SSE readers snapshot under the lock
   (2410-2411, 2466-2467). The lock-free reads (`_ingest_paused.is_set()`, list slices of a
   snapshot) are safe under the GIL. Note `ingest_tree` *does* call `progress_cb` from
   multiple pool threads, so any future callback must stay thread-safe — the current web
   callback is; the scheduler passes `None`.
7. **Windows-specific**: `WinError 32` on move/unlink of files with open handles is
   demonstrably reachable (test-run evidence) for the drop archive, staging members and
   `.done` moves (H4); long-path handling exists in the scan/uploader paths but NOT in the
   ingest staging/extraction/move path (M7); there is no staging sweeper, so plaintext can
   persist in `staging/` after any of these failures (H4/M6).

## Severity counts

| Severity | Count |
|---|---|
| Critical | 1 |
| High | 6 |
| Medium | 8 |
| Low | 8 |

## Priority fix order

1. C1 (cold-export serialization + atomic partition write) — permanent history loss.
2. H1 (single global ingest gate + status-aware op_log gate) — removes the whole class of
   double-ingest corruption and most Windows lock errors.
3. H3 (target-namespaced, locked rebalance checkpoint) — silent non-replication today.
4. H2 (`write_txn()` contextmanager with rollback on the upload path) — unlocks WAL under
   contention and stops partial-batch commits.
5. H5 (loop-level exception guard + correct stop/start handoff) — keeps the daemon alive.

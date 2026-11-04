# Production Verification Runbook — cPanel Ingest Pipeline

Use this on the real host with a **real** cpmove archive. Run every step
in order; a failing step halts the runbook (fix before continuing). The
synthetic `scripts/smoke_e2e.py` exercises the same CLI surface and is the
template for the commands below — adapt the archive path and account name.

## Pre-flight

1. **Back up the existing database.**
   ```sh
   cp backupme.db backupme.db.pre-verify-$(date +%F)
   cp -r parquet parquet.pre-verify-$(date +%F) 2>/dev/null || true
   ```
2. **Confirm config.**
   ```yaml
   cpanel_ingest:
     enabled: true
     drop_root: /path/to/drop      # where real cpmove-*.tar.gz lands
     staging_dir: /path/to/staging
     staging_budget_gb: 10
     keep_raw: 0                   # set >0 only if you want raw copies
     encryption:
       enabled: true               # verify encrypted path on real data
       key_file: /path/to/test.key
   ```
3. **Confirm a free backend exists** for the drain half:
   ```sh
   backupme backend list --json
   ```
4. **Note the current file/blob counts** (baseline for the delta checks):
   ```sh
   sqlite3 backupme.db "SELECT COUNT(*) FROM files; SELECT COUNT(*) FROM blobs;"
   ```
5. **Ensure the host runs Python 3.13** and the installed `backupme` is the
   commit you intend to verify (`git rev-parse HEAD`).

## Step 1 — Ingest a real archive (dry run first)

```sh
# 1a. Dry run: parses the archive, prints the member plan, writes nothing.
backupme ingest-cpanel /path/to/drop --dry-run --json
```
Expected: exit 0, valid JSON, `archives == 1`, `members` > 0, no
`../evil`-style traversal members, `excluded`/`skipped_oversize` reported.

```sh
# 1b. Real run.
backupme ingest-cpanel /path/to/drop --json
```
Expected:
- exit 0, `status == "ok"`, `archives == 1`
- the archive moves to `drop/.done/<account>/cpmove-<account>.tar.gz`
- rows under `source_root = 'cpanel/<account>'`
- `ingest-status --json` shows the account in `accounts`

## Step 2 — Idempotency

Re-run the **same** archive (copy it back from `.done/` first):

```sh
cp drop/.done/<account>/cpmove-<account>.tar.gz drop/<account>/
backupme ingest-cpanel /path/to/drop --json
```
Expected: `status == "skipped"`, `reason == "already ingested"`, **no**
new `files` rows, byte counts unchanged.

## Step 3 — Dedup across generations

Modify one file inside a fresh archive (e.g. bump `homedir/index.php`)
and ingest it:

```sh
backupme ingest-cpanel /path/to/drop --json
```
Expected: `dupe` > 0 (unchanged members dedup), `members` == 3,
new bytes ≈ size of the changed file only (not the whole archive).

## Step 4 — Restore + retar

```sh
backupme restore-cpanel <account> 1 --out /tmp/restore_out --retar
```
Expected:
- `status == "ok"`, `members restored == 3`
- restored tree matches the original (diff against a known-good extraction)
- `retar` path exists and is a valid tar.gz:
  ```sh
  tar -tzf /tmp/restore_out/<account>/retar.tar.gz | sort
  ```
- if encryption was enabled, the restored bytes decrypt correctly
  (spot-check a known string).

## Step 5 — Cold export + purge + history

```sh
backupme db cold-export
backupme db purge
backupme db history "SELECT source_root, COUNT(*) FROM files GROUP BY source_root" --json
```
Expected:
- cold-export reports `status == "ok"`, `rows >= 1`, parquet file appears under
  `parquet/files_YYYY-MM.parquet`
- purge reports deleted rows (only soft-deleted + past grace + below watermark)
- history query returns rows including `cpanel/<account>`; the purged rows
  are gone from SQLite but still queryable via history

## Step 6 — Backend onboard / rebalance / drain (optional but recommended)

```sh
backupme backend add second /path/to/storage2   # or via config edit
backupme backend onboard second --rebalance --json
backupme backend rebalance second --json
backupme backend usage --json
backupme backend drain test_local --json
```
Expected:
- onboard: `connected == true`, rebalance `copied >= 1`
- second pass of rebalance: `copied == 0`
- usage: `second.stored_bytes > 0`
- drain: `remaining_only_here_blobs == 0`, `runbook` present
- restore still works after drain (data survived on `second`)

## Step 7 — Web GUI smoke (optional)

```sh
backupme web --host 127.0.0.1 --port 8080 --token "$TOKEN" &
curl -s http://127.0.0.1:8080/ | grep -q "BackupMe" && echo "page ok"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/status   # 401
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
     http://127.0.0.1:8080/api/status   # 200
```

## Step 8 — Daemon tick (optional)

Enable the scheduler and let it run one full tick, then inspect logs for
exceptions (there should be none):

```sh
# scheduler.ingest_interval controls cadence; watch for one pass
tail -f backupme.log | grep -i "scheduler\|ingest\|ERROR\|Traceback"
```

## Rollback

If anything fails:
```sh
pkill -f "backupme web" 2>/dev/null
cp backupme.db.pre-verify-$(date +%F) backupme.db
cp -r parquet.pre-verify-$(date +%F) parquet 2>/dev/null || true
```
Move any half-ingested archives back from `drop/.done/<account>/` to
`drop/<account>/` only if you want to re-run Step 1.

## What "pass" means

- All 8 steps exit 0 with the expected JSON fields.
- No `Traceback` anywhere in stdout/stderr.
- DB integrity check clean: `backupme db check` (or
  `sqlite3 backupme.db "PRAGMA integrity_check;"`) returns `ok`.
- Restored tree byte-identical to the source archive's member content.
- No plaintext member content in `storage/` when encryption is enabled.
- `.done/<account>/` contains the moved archive.

## Known-good baseline

The synthetic run (`scripts/smoke_e2e.py`) passes all 21 checks against
the commit you are verifying. If the synthetic run fails, do **not** run
this production runbook — fix the regression first.

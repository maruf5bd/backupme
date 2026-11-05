# Lessons (class-bugs and how to never repeat them)

## Reachability / dedup semantics

- **Exact-path matching cannot see bundle members.** Member blobs live INSIDE parent tars
  (`storage_path = bundle@offset:len`). Any blob inventory, copy, or reachability logic that
  matches storage paths 1:1 will report thousands of false losses (the Gdrive misdiagnosis:
  "51,644 lost" -> actually 1 bundle). Always resolve members through the parent.
- **Compressed vs plaintext sizes are different axes.** `file_chunks.pt_offset/pt_size` are
  plaintext; on-disk bundle/object sizes are post-compression. Mixing them produces false
  "unreachable"/"corrupt" verdicts (D2).
- **Decompress-hash is the definitive corruption check.** File existence and size checks
  cannot see zstd frame damage (the 24 corrupt files passed "exists" but failed decompress).

## Silent failure modes

- **Silent walk truncation** — `except Exception: pass` inside a recursive walk turns a
  partial listing into a "successful" one (SFTP showed 74 of 1,002 archives). Pattern fix:
  bounded retries, `walk_errors` counter, log every skip. Now being ported to all backends.
- **Config sections not wired = silently ignored settings.** A missing branch in
  `load_config()` means the user's setting (e.g. `web.token`) does nothing and nothing tells
  them (D1). Rule: every Config section must be parsed + pinned by a test.
- **Raw user input into FTS5 MATCH = 500.** Quote/sanitize and fall back to LIKE (D3).

## Environment (this machine)

- **Git Bash is broken here**: `_lc` alias errors, parentheses/pipes fail. Use
  `command git`, `/usr/bin/grep`, and Write-tool scripts run as `python script.py`.
- **Concurrent pytest runs collide on the shared basetemp** — give each concurrent agent its
  own `--basetemp`.
- **Hetzner Storage Box SFTP**: password auth (encrypted key file fails), new sessions are
  rate-limited ("Unable to open channel" -> retry with backoff), `Transport.connect()` hangs
  forever without instance attributes `banner_timeout`/`auth_timeout` (constructor kwargs are
  not honored).
- **SQLite concurrency**: single writer via WAL + thread-local connections + `write_txn()`;
  readonly inspection from scripts uses `file:...?mode=ro`. **FK enforcement is ON**
  (`blob_storage.blob_id -> blobs.blob_id` fails inserts without a parent row; `blobs` needs
  `pt_size`). `blob_storage`'s backend column is `backend_name`, NOT `backend` — two ad-hoc
  scripts already tripped on this. CLI invocations reset `dbmod.DB_PATH` to `backupme.db`
  unless `--db` is passed — in-process CLI tests must pass `--db` to see the fixture DB.

## Process

- **Verify completion from the DB, not from task handles** — background job IDs can be lost
  to context rollovers; the data (orphan-chunk counts) is the source of truth.
- **Fan out but fence** — parallel agents only with disjoint file ownership; a routes.py
  edit by two agents is a lost edit.

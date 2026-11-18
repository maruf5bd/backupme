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
- **Verify heals by DOWNLOADING THE LATEST VERSION, never by row presence or a remembered
  id** — repairs create new version rows (files table versions by root+path+version_seq);
  testing a stale id "proves" breakage that no longer exists (D7), and row presence proves
  nothing about physical bytes (the original 10/10 heal claim).
- **Fan out but fence** — parallel agents only with disjoint file ownership; a routes.py
  edit by two agents is a lost edit. Never run the full test suite against the tree while
  a refactor agent is mid-move — you will "find" failures that are just half-moved modules.

## GC / destructive operations (2026-11-05)

- **A 28 GiB deletion is never trusted from one dry-run number.** A GC dry-run count is
  only as good as the reference set it is diffed against. Before any apply: (1) verify
  matcher health — matched-file count must equal the live DB row count for that backend
  (here 15,607 = 15,607); (2) sweep every orphan path across ALL referencing tables
  (`blobs`, `file_chunks`, `blob_storage`) — on this DB all 8,498 orphans had zero rows in
  every table, i.e. leftovers of an earlier DB generation that no restore path can address.

## Legacy formats (2026-11-07, D8)

- **"Malformed legacy metadata" can be an undocumented format.** The no-`@` bundle rows
  looked like garbage (three successive mis-diagnoses: member-id mismatch -> malformed
  blob_ids -> finally the truth). Reverse-engineer the bytes before declaring data dead:
  the objects turned out to be 52-byte dedup envelopes (`bndl/objects/...` header + real
  content, blob named after the whole envelope) and 2,170 live files became restorable
  with ~30 lines of downloader code.
- **Envelope naming subtlety**: the blob's sha gate passes on the FULL envelope, while the
  header points at the content's sha. Slice only AFTER the envelope-level gate, size the
  content from `file_chunks.pt_size` arithmetic (`len(data) - pt_size`), and cross-check
  the slice against the header's embedded sha prefix. Never slice first and then try to
  pass the content through a gate that expects the envelope's sha.
- **Header-scan ordering trap**: hexdump first, find the content offset by locating the
  known-good source bytes (`pt.find(src[:256])`), then decode the header around it —
  reading structure off the first bytes alone sent the investigation down a "pointer
  object" path that was half wrong.

## Release / CI (2026-11-16)

- **A green local suite does not mean a green clean install.** Six consecutive CI
  failures each exposed something the dev box hid by accident: an undeclared runtime dep
  (`python-multipart`, `py7zr`), a hard-dep with no py3.13 wheel (`smbclient`), an
  untested-paramiko upper bound, dev/test deps not in an extra, and a test suite that
  relied on a basetemp parent that only existed locally. Rule: any new
  dependency/tool/clean-checkout assumption must be proven on a fresh runner, not just
  on this machine.
- **Treat `smbclient`-style no-dist deps as optional extras, never hard deps.** The SMB
  backend lazy-imports it and degrades gracefully; the extra carries the install intent
  without breaking `pip install .`.
- **`--help` is a side-effect-free contract.** A group callback that initializes the DB
  before the command body means `backupme db --help` scatters a `backupme.db` in CWD.
  Help-only runs must skip all disk/config side effects.
- **FastAPI form endpoints need `python-multipart` declared.** It is not pulled in by
  FastAPI itself; a clean install hits `RuntimeError: Form data requires python-multipart`.
- **GitHub Actions green run order matters**: install extra deps (`dev`, feature extras)
  and any external binaries (`rclone`) in the same workflow the tests need, or the suite
  errors at collection rather than running.
- **Pinned-release value**: a tagged `v2.0.0` whose wheel builds cleanly on CI gives a
  reproducible artifact; the version string is single-sourced from
  `backupme.__version__` and surfaced via `backupme --version`.

## Truth registers (2026-11-18)

- **Two truth registers WILL drift — AGENTS.md "Known broken" did, for 8 days.** It claimed
  Gdrive resolved at `H:\Backupme` while defects.md D14 (open, CRITICAL) said the root moved
  to `H:\New folder\backupme` amid a 124.8 GiB depletion. An agent obeying AGENTS.md could
  have drained/repointed/gc-applied against the wrong store. Rule: defects.md is
  authoritative; update the AGENTS.md paragraph in the same pass as any defect/root change,
  and re-verify AGENTS.md claims against the register before acting on them.
- **Bulk-edit generators must be quoting-safe, and jobs close only on a re-scan.** The
  2026-11-16 docstring sweep died mid-job on an unescaped apostrophe in "backend's"
  (add_doc_out3.txt, rc=1): 10 of 26 modules got docstrings while the success log made it
  look complete. Insert text via repr()/triple-quoted raw templates, and close every
  bulk-edit job by re-running the original missing-X scan instead of trusting the run log.
- **A slow gate needs a liveness probe, not patience.** The 688-test local suite runs
  ~20+ min while CI does it in ~3, so "no notification yet" is ambiguous between working
  and hung. Check process liveness + output-file mtime + fixture-dir churn before assuming
  either.

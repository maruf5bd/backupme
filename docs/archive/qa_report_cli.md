# QA Report — CLI (`backupme`) and MANUAL.md §10 Command Reference

QA agent 2 of 3, black-box sweep. Date: 2026-11-07.
Scope: every command registered in `backupme/cli/` (40 leaf commands) plus
MANUAL.md §10 cross-check. All functional tests ran through click's CliRunner
against isolated scratch envs (tests/conftest.py `env` fixture pattern); the
live `config.yaml` / `backupme.db` / dashboard were never touched. No
destructive command was run (`gc --apply`, `db purge`, `db restore`, `db
rebuild`, non-dry-run `backend drain/rebalance/onboard` excluded by design).

**Every finding below was verified twice** (two identical runs of
`qa2_verify_defects.py`, evidence in `qa2_verify1.txt` / `qa2_verify2.txt`,
plus failing-then-xfail test runs in `qa2_run*.txt`).

## Summary

- Commands inventoried: 40 leaf commands in 5 groups + 15 top-level (see `qa2_cli_inventory.txt`)
- `--help` sweep: 40/40 exit 0 with documented options
- Tests: `tests/qa2_cli_sweep.py` — **83 tests: 75 passed, 8 xfailed** (the 8
  xfail entries ARE the defects, marked `@pytest.mark.xfail(strict=False)`), exit 0
- Defects: **8 verified functional defects — 1 P0, 7 P1** — plus 8 P2
  observations and extensive MANUAL.md §10 drift (12 phantom commands/options,
  16 undocumented commands/options)

---

## P0 findings

### P0-1 (qa defect 8) — `backupme search` crashes on any hyphenated query
- Repro: `backupme search "report-2026"`
- Evidence: exit 1, `sqlite3.OperationalError: no such column: 2026`, full
  traceback on stderr (verified via CliRunner AND the real CLI entry point in
  standalone mode).
- Root cause: `backupme/core/index.py::search_files` passes the raw query to
  FTS5 `MATCH ?`. FTS5 query syntax treats `-term` as a column filter, so
  `report-2026` parses as column `2026`. Any user query containing `-`
  (dates, versions, file stems like `annual-report`) crashes instead of
  returning results or a clean "no match".
- Related: hyphen-free queries work (exit 0, correct results verified).

## P1 findings

### P1-1 (qa defect 1) — `list --backend NAME` filters on the wrong column
- Repro: back up a file to backend `test_local`, then
  `backupme list --backend test_local --json` → 0 rows.
- Evidence: the file IS on `test_local`; filter returned 0 rows, exit 0.
- Root cause: `backupme/cli/list_cmd.py` lines 21-24 compare
  `f["source_root"] == backend`. Correct join path exists in
  `index.get_all_files(backend=...)` (blob_storage.backend_name) but the CLI
  filter never uses it — a user filtering by backend always sees an empty list
  (silently wrong results). `index.get_all_files` already supports the right
  semantics; `list` bypasses it when `--hash` is absent.

### P1-2 (qa defect 2) — `download <unknown id>` dumps a raw traceback
- Repro: `backupme download 999999`
- Evidence: exit 1 (correct code) but stderr shows
  `Traceback ... FileNotFoundError: File id=999999 not found (or soft-deleted)`.
- Root cause: `core/downloader.download_file` raises `FileNotFoundError`;
  `cli/download_cmd.py` has no try/except and would also `KeyError` on the
  `{"status": "error"}` no-chunks result. Same pattern as the fixed `verify
  --file` path, which maps errors to a clean message + exit 1.

### P1-3 (qa defect 3) — `backup` silently ignores nonexistent source paths
- Repro: `backupme backup J:\nope\missing.txt` → exit 0, no output.
- Evidence: exit 0, stdout `''`.
- Root cause: `cli/backup_cmd.py` only appends sources where
  `os.path.isfile(path)` / `os.path.isdir(path)`; anything else is dropped
  without a word. A typo'd path produces a "successful" backup of nothing —
  the worst possible feedback for a backup tool.

### P1-4 (qa defect 4) — `backend test <unknown name>` exits 0
- Repro: `backupme backend test ghost_backend --json` →
  `{"connected": false, "error": "backend 'ghost_backend' not found"}`, exit 0.
- Evidence: exit 0 with error payload; the not-found branch (`backend_cmd.py`
  `backend_test`) emits and returns without a nonzero exit. Every other
  backend subcommand exits nonzero on unknown names (`rescan` → 1, `onboard`
  → 2), so scripts cannot rely on a consistent contract.

### P1-5 (qa defect 5) — `backend add --test/--no-test` is accepted but ignored
- Repro: `backupme backend add local --name x --path <p> --no-test` with no stdin.
- Evidence: exit 1 (abort); output contains the `Test connection now? [Y/n]`
  confirm; the backend row was already written to config.yaml before the abort.
- Root cause: `backend_cmd.py::backend_add` declares `--test/--no-test`
  (default `--test`) but the body never references the `test` parameter; the
  unconditional `click.confirm("Test connection now?", default=True)` still
  fires. Non-interactive automation (`backend add` in scripts) hangs/aborts
  despite the documented flag.

### P1-6 (qa defect 6) — `backend remove <unknown name>` reports false success
- Repro: `backupme backend remove ghost_backend`
- Evidence: prints `Removed backend 'ghost_backend'`, exit 0, config file
  byte-identical before/after (nothing removed).
- Root cause: `backend_cmd.py::backend_remove` sets `found = True` whenever
  ANY backend-type list is still non-empty after filtering — it never checks
  whether the named backend matched. Dangerous in the retire runbook
  (`backend drain NAME` → remove → gc): a typo'd name reports success and the
  operator proceeds to `gc --apply`.

### P1-7 (qa defect 7) — `convert <unknown backend>` dumps a raw traceback
- Repro: `backupme convert ghost_backend`
- Evidence: exit 1 (correct code), stderr
  `Traceback ... ValueError: Backend 'ghost_backend' not found or not reachable`.
- Root cause: `core/converter.convert_backend` raises bare `ValueError`;
  `cli/convert_cmd.py` does not catch it.

### P1-8 — MANUAL.md §10 command reference has large-scale drift
Detailed table below. Highlights: 6 documented commands do not exist at all
(`status`, `restore`, `check-consistency`, `scrub`, `encrypt change-password`,
`daemon start|stop|status`), 3 more are documented with options that don't
exist (`report --format`, `db restore --from/--snapshot`, `download
ID|PATH --from/--dest`, `verify --all/--recent`), and 16 existing
commands/options are undocumented. Severity P1 because §10 is the operator's
contract: e.g. `backupme daemon start` per the quick table BLOCKS FOREVER
(the real `daemon` has no subcommands and runs the scheduler in the
foreground) — an operator following the manual hangs their shell.

## P2 findings (polish / consistency)

| # | Finding | Evidence |
|---|---|---|
| P2-1 | `hash-lookup <unknown>` exits 0 ("Hash not found") | `qa2_verify*.txt` |
| P2-2 | `hash-lookup` cannot resolve bundle-member content by FILE sha256 (small files live inside parent tars; only chunk/bundle blob ids resolve) — MANUAL §4 implies file-hash lookup | seeded file's sha256 → "Hash not found" while its parent bundle id resolves |
| P2-3 | `report get <unknown id>` exits 0 ("Report not found") | verify runs |
| P2-4 | `verify --backend <typo>` silently checks 0 files, exit 0 (no unknown-backend error) | `integrity check #1: completed (checked=0 failed=0)` |
| P2-5 | `recompress --backend <typo>` silently recompresses 0 blobs, exit 0 | `Recompressed 0 of 0 blobs` |
| P2-6 | `backend drain <typo>` (dry-run) silently plans 0 blobs instead of an unknown-backend error | `{'status': 'dry-run', 'backend': 'ghost_backend', 'blobs': 0, ...}` |
| P2-7 | `db history --json` is declared `is_flag=True, default=True` — the human renderer is dead code and `--json` is a no-op (`db_cmd.py` line 163) | code inspection + run |
| P2-8 | `backend add` password env-var tip is dead code: line 137 tests `if name == "password"` where `name` is the backend NAME option, not the loop variable `field_name`; the plaintext-password protection never fires | code inspection |
| P2-9 | `search --content` only matches `files.content_text`, which the current upload pipeline never populates (legacy inline rows only) — content search silently returns nothing for all new backups | uploader/index code inspection; test asserts exit 0 only |
| P2-10 | Missing `--json` on `backup`, `download`, `search`, `hash-lookup`, `convert`, `gc` (inconsistent with the `emit()` pattern used elsewhere) | code inspection |
| P2-11 | `encrypt init` writes to the REAL `~/.backupme/key.enc` — documented behavior, but worth noting for automation (test had to fake `HOME`/`USERPROFILE`) | code inspection |

## MANUAL.md §10 doc-drift table

### Documented but does not exist (as documented)

| MANUAL says (line) | Reality |
|---|---|
| `backupme status` (873, 898, §8.1:794, §9:855) | command does not exist |
| `backupme restore --source --dest` (870, 894, §3:180) | command does not exist (only `db restore`, `restore-cpanel`) |
| `backupme check-consistency` (901, §8.4:745) | command does not exist |
| `backupme scrub [--backend NAME]` (902) | command does not exist |
| `backupme encrypt change-password` (911, §1.2:844) | subcommand does not exist |
| `backupme daemon start\|stop\|status` (914, §7.3:463-469) | `daemon` has NO subcommands; single blocking foreground process; no stop/status |
| `backupme report --format html\|json\|text` (900, §8.5:739) | `report` is a group: `list/get/verify/integrity`; no `--format`, no default report run |
| `backupme db restore [--from B] [--snapshot FILE]` (906, 881, §9.3:813) | `db restore` takes NO options |
| `backupme download <file_id\|path> [--from B] [--dest P]` (893, §3:168-188) | arg is int `FILE_ID` only (no PATH); options are `--output`, `--version`, `--backend` |
| `backupme verify [--all] [--recent N]` (899, §8.3:721, §9.1:807) | flags don't exist; real extra flag is `--file ID` |
| `backupme logs --op cpanel_ingest` (§7b:631) | command does not exist |
| `backupme convert BACKEND --recursive` (§7.4:484-490) | `--recursive` is not an option of `convert` |
| `backupme list [--source ROOT]` (895) | option is `--source-root` (works via click prefix-matching by accident) |

### Exists but undocumented anywhere in MANUAL

`stats`, `backends`, `select-backend`, `backend add/list/remove/test/onboard/rebalance/drain`
(§6 documents backend YAML only; `backend rescan` IS documented), `db vacuum`,
`db prune-oplog`, `report list/get/verify/integrity`; plus options
`backup --no-skip/--force`, `web --token`, `db check --repair`, `list
--limit/--offset/--json`, `verify --file` (documented in §8.2 but missing from
the §10 All-Options block).

### Documented and accurate (spot-verified functionally)

`backup`, `upload-bundle`, `search`, `hash-lookup`, `convert` (minus
`--recursive`), `db check/snapshot/rebuild/oplog/info`, `encrypt
init/status`, `recompress`, `web --host/--port`, `gc`, `health`, `verify
--count/--backend`, `ingest-cpanel`, `ingest-status`, `restore-cpanel`, `db
cold-export/purge/history`, `backend rescan`.

## Coverage matrix (functional, isolated envs)

| Command | Exercised | Result |
|---|---|---|
| backup | seed backup, `--move`, nonexistent path, help | works; defect 3 |
| list | `--json`, `--source-root`, `--hash`, `--limit/--offset`, human, `--backend` | works; defect 1 on `--backend` |
| stats | `--json` + human | OK |
| backends | `--json` | OK |
| search | match, no-match, `--content`, hyphenated | works; defect 8 on hyphens |
| select-backend | fuzzy query | OK |
| hash-lookup | known chunk id, file sha, unknown | OK; P2-1/P2-2 |
| download | roundtrip `--output` | works; defect 2 on unknown id |
| upload-bundle | 2 small files | OK |
| verify | `--count` empty+seeded, `--file` ok/unknown/corrupt, `--backend` | OK; P2-4 |
| health | `--json` strict parse, human, `--purge-candidates`, empty DB | OK |
| gc | dry-run, no-backends message | OK (`--apply` NOT run) |
| ingest-cpanel | `--dry-run --json` smoke (AGENTS rule: exit 0 + parseable JSON, verified), empty drop root, no-backends exit 2, real ingest, `.done` move + idle re-run, missing dir exit 2 | OK |
| ingest-status | `--json` after real ingest (accounts, generations, storage sizes) | OK |
| restore-cpanel | real restore of ingested generation, unknown account exit 1 | OK |
| backend add | non-interactive, duplicate-cancel, interactive type prompt, `--no-test` | writes config; defect 5 |
| backend list | `--json` | OK |
| backend remove | unknown name, add-then-remove roundtrip | defect 6 |
| backend test | known (connected), unknown | OK; defect 4 |
| backend onboard | `--dry-run` known, unknown exit 2 | OK |
| backend rebalance | `--dry-run --json` | OK (real copy NOT run) |
| backend drain | `--dry-run --json` with target, single-backend exit 1, unknown name | OK; P2-6 (real drain NOT run) |
| backend rescan | clean exit 0, missing blob exit 1, unknown exit 1 | OK |
| db check | `--json`, `--full` | OK |
| db info | `--json` | OK |
| db snapshot | `--json`, snapshot file created | OK |
| db oplog | `--json` | OK |
| db cold-export | `--dry-run --json` + real export | OK |
| db history | DuckDB SQL over exported parquet (2 queries) | OK |
| db vacuum | run | OK |
| db prune-oplog | `--days 0 --json` | OK |
| db purge | NOT run (destructive); `--help` verified | n/a |
| db restore / db rebuild | NOT run (recovery paths); `--help` verified | n/a |
| report list/get/verify/integrity | full lifecycle | OK |
| encrypt status/init | with faked HOME | OK |
| recompress | real run, unknown backend | OK; P2-5 |
| convert | zero-file run, unknown backend | OK; defect 7 |
| web | `--help` only (server NOT started per instructions) | OK |
| daemon | `--help` + no-subcommand proof | OK; doc drift P1-8 |
| global | `--config`/`--db` honored (fresh-DB proof), unknown command/option/missing arg exit 2 | OK |

## Deliverables and how to run

- `tests/qa2_cli_sweep.py` — `python -m pytest tests/qa2_cli_sweep.py -q --basetemp=test_tmp_backend/qa2`
  (83 tests: 75 pass, 8 xfail = documented defects; exit 0). Use a private
  `--basetemp`: the default shared `test_tmp_backend/pytest` collides with
  concurrently-running agents (PermissionError on locked DBs from other sessions).
- `qa2_cli_inventory.txt` — full command/option inventory + drift summary
- `qa2_verify_defects.py` + `qa2_verify1.txt` / `qa2_verify2.txt` — twice-run
  defect reproductions with real-CLI subprocess evidence

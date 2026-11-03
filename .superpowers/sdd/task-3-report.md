# Task 3 Report — CLI output + progress helpers

## Status: COMPLETE

Commit: `9f2b092` — `feat(cli): shared --json emit + progress display helpers`

## What was built

### `backupme/cli/output.py` (new)
- `emit(data, as_json, human=None)` implementing the `--json` pattern:
  - `as_json=True` → `click.echo(json.dumps(data, indent=2, default=str))` (the `default=str`
    makes Paths/dates/etc. serialize instead of raising, matching existing db_cmd output style).
  - `as_json=False` with callable `human` → calls `human(data)`, nothing else printed.
  - `as_json=False` without `human` → default renderer:
    - empty (falsy) → `(none)`
    - dict → `key: value` lines
    - list of dicts → aligned columns from ordered union of keys, header + `---` separator
      row, values via `str()` truncated to 40 chars
    - list of scalars → one per line
- Dependency-free: imports only `click` + stdlib `json`. No backupme.core imports
  (verified by grep), so it stays lazy-import friendly like the rest of `backupme/cli`.

### `backupme/cli/progress.py` (new)
- `Progress(enabled, total=None, desc="")` with `update(n=1)`, `set_total(total)`,
  `finish()`, `__enter__`/`__exit__` (context manager calls `finish()` on exit, never suppresses exceptions).
- tqdm loaded at module import inside a guarded `try/except ImportError`; result stored as
  module attr `_HAS_TQDM` (import happens here rather than inside `__init__` because tqdm is
  imported once either way, and a module-level attr is exactly what the brief requires the
  tests to monkeypatch).
- tqdm path: `tqdm(total=..., desc=..., unit="item")`; `set_total` rewrites `bar.total` + refresh.
- Fallback path (tqdm missing): after every `update`/`set_total` (and at init when total given)
  renders `click.echo(f"\r{desc}: {done}/{total or '?'} ({pct}%)", nl=False)`; without a total
  shows `desc: N/? (?%)`. `finish()` emits the newline via `click.echo()` then a summary line
  `desc: done (done_count)` (or bare `done (N)` when desc empty).
- `enabled=False`: every method is a no-op, zero output.

### `tests/test_cli_helpers.py` (new, 14 tests)
Written first (red: `ImportError: cannot import name 'output'`), then implementation, then green.
Plain pytest + capsys, no fixtures. Coverage per brief:
1. emit json dict roundtrip via `json.loads` with stable key order.
2. `default=str` handles a `pathlib.Path` value.
3. human renderer receives the data object; default renderer not invoked.
4. default dict rendering → exact `key: value` lines; empty list → `(none)`.
5. default scalar-list rendering → one per line.
6. default dict-list rendering → header contains union of keys, values truncated at 40 chars.
7. Progress fallback with total → `\rwork: 3/10 (30%)`, `\rwork: 10/10 (100%)`, finish newline +
   `work: done (10)`.
8. Progress fallback without total → `\rscan: 1/?` + `(?%)`.
9. `set_total` mid-stream in fallback: `ingest: 2/?` → after `set_total(5)` + 3 updates →
   `ingest: 5/5 (100%)` and `ingest: done (5)`.
10. disabled → all methods called, zero bytes of output.
11. tqdm smoke test (`pytest.importorskip("tqdm")` + monkeypatch `_HAS_TQDM=True`): context
    manager enter/update/set_total/update/exit with no error.
12. `_HAS_TQDM` matches what `importlib.util.find_spec("tqdm")` says (detection is honest).
13. Natural fallback exercised when tqdm is genuinely absent (skips if tqdm is installed).

## Test evidence

- New file alone (red, before implementation):
  `ImportError: cannot import name 'output' from 'backupme.cli'` — collection interrupted.
- New file alone (green): `13 passed, 1 skipped` (skip = tqdm smoke test; tqdm not installed in
  C:\Python313).
- FULL suite: `120 passed, 1 skipped in 89.17s` (= baseline 107 + 14 new; the single skip is the
  tqdm smoke test, skipped because tqdm is not installed). No regressions.

## Contract compliance

- TDD followed: tests → red → implement → green → full suite.
- Modules import only click/stdlib/tqdm; zero `backupme.core` imports (grep-verified).
- No existing files modified (git shows only new files in the commit).
- Single commit with the exact required message.
- Broken-alias workaround used throughout (`command git`/`command ls`; pytest output
  redirected to a file under `/c/Users/Maruf/AppData/Local/Temp/` since `/tmp` resolves
  elsewhere in this subagent shell).

## Notes / concerns

- Per the brief's `git add -A`, the commit also carried in-flight coordinator files that were
  already in the working tree: `.superpowers/sdd/progress.md` (modified, Agrade note for
  tasks 1+2), plus untracked `.superpowers/sdd/task-3-brief.md` and
  `.superpowers/sdd/review-t12.diff`. No code files other than mine were touched.
- tqdm is not installed in this environment, so the fallback branch is the live code path
  today; the tqdm branch is smoke-tested but only runs once tqdm is added to requirements.
- A pre-existing broken shell startup script (`rg()` wrapper referencing undefined `_lc`)
  emits noise on every Bash call; worked around with `command` builtins. Not a repo issue.

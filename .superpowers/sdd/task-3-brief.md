# Task 3 Brief — CLI output + progress helpers

You are implementing Task 3 of the cPanel Ingest pipeline (Plan 01) for BackupMe, a Python deduplicating backup tool (click-based CLI). These two small modules are shared infrastructure: Task 5 and Task 6 wire them into commands.

## Environment notes
- Windows, Git Bash. Broken shell aliases: use `command git`, and `/c/Python313/python.exe -m pytest tests/test_cli_helpers.py -q > /tmp/pt.txt 2>&1` then read /tmp/pt.txt.
- TDD: write tests first, see them fail, implement, see them pass, run FULL suite (currently 107 passing).
- One commit: `command git add -A && command git commit -m "feat(cli): shared --json emit + progress display helpers"`.

## Module 1 — `backupme/cli/output.py` (new)

```python
import click, json

def emit(data, as_json: bool, human=None):
    """--json pattern: JSON to stdout when as_json, else human renderer (fn returning nothing) or default table."""
```

Requirements:
- `emit(data, as_json, human=None)`: when `as_json` → `click.echo(json.dumps(data, indent=2, default=str))`. When not → if `human` is callable, call `human(data)`; else render a simple default: dict → `key: value` lines, list of dicts → aligned columns from union of keys (str(value), truncated to 40 chars), list of scalars → one per line, empty → `(none)`.
- Keep it dependency-free (click + stdlib json only). This module must NOT import anything from backupme core (stays lazy-import friendly).

## Module 2 — `backupme/cli/progress.py` (new)

```python
class Progress:
    def __init__(self, enabled: bool, total: int | None = None, desc: str = "")
    def update(self, n: int = 1)
    def set_total(self, total: int)     # totals may be discovered mid-stream (see estimate_archive)
    def finish(self)
    def __enter__ / __exit__            # context manager; finish() on exit
```

Requirements:
- `tqdm` when importable (lazy import inside `__init__`, guarded try/except), else fallback rendering: after each `update`, `click.echo(f"\r{desc}: {done}/{total or '?'} ({pct}%)", nl=False)`, and on `finish()` a final newline `click.echo()` with `desc: done (done)`summary line. When `enabled=False`, everything is a no-op.
- `set_total` initializes/replaces the tqdm total (or stored total for fallback).
- Suppress tqdm output interference with `--json` mode isn't needed here — callers simply don't enable progress in json mode.
- Stdlib + tqdm only; no core imports.

## Tests — new file `tests/test_cli_helpers.py`

Use plain pytest (no fixtures needed — these modules are pure). Required coverage:
1. `emit` with as_json=True on a dict produces parseable JSON (json.loads roundtrip) with stable keys.
2. `emit` human mode with custom `human` fn calls it with the data.
3. `emit` default human rendering: dict → lines, empty list → `(none)`.
4. `Progress` fallback (monkeypatch tqdm import to fail, or pass a flag — simplest: monkeypatch `builtins.__import__` is overkill; instead structure the module so `_HAS_TQDM` is a module attr the test can monkeypatch to False): with total → updates render `\r...` lines; without total shows `?`; finish writes newline; disabled → no output (capture with click's CliRunner or capsys).
5. `Progress._HAS_TQDM=True` path smoke test if tqdm installed (skip otherwise): context manager enters/exits without error.
6. `set_total` mid-stream works in fallback mode.

Run new tests, then FULL suite to prove no regression (expect 107 + your new tests).

## Report
Write full report to `.superpowers/sdd/task-3-report.md` (what/why/test evidence incl. full-suite line). Return: status, commit hash, one-line test summary, concerns (max 2 lines).

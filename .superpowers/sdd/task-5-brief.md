# Task 5 Brief — cli/ingest_cmd.py (+ cli/__init__.py registration)

You are implementing Task 5 of the cPanel Ingest pipeline (Plan 01) for BackupMe. Three new click commands wiring the engine built in Tasks 1-4: `ingest-cpanel`, `restore-cpanel`, `ingest-status`. Plus registration of the command module in `backupme/cli/__init__.py`.

## Environment
- Windows, Git Bash. Broken aliases: `command git`; `/c/Python313/python.exe -m pytest ... -q > /tmp/pt.txt 2>&1` then read file.
- TDD: tests/test_cli_output.py (covers CLI command surface, invoked via CliRunner) FIRST — it can import `backupme.cli.ingest_cmd` which won't exist yet (red), then implement, green. Existing suite: 120 passed + 1 skip.
- One commit at end: `command git add backupme/cli/ingest_cmd.py backupme/cli/__init__.py tests/test_cli_output.py && command git commit -m "feat(cli): ingest-cpanel / restore-cpanel / ingest-status commands"` — ONLY these three files.

## Existing infrastructure to reuse (read these first)
- `backupme/cli/base.py` — `cli` click group, `ctx.obj["config"]`, ctx.obj["config_path"], `db_path` handling.
- `backupme/core/cpanel_ingest.py`: `discover(drop_root)`, `ingest_archive(...)`, `ingest_tree(...)`, `estimate_archive(archive_path)`, `restore_cpanel(account, generation, member_filter, out, retar, backends, config)`, `sanitize_member`, `next_version_seq`/`index_mod`.
- `backupme/config.py`: `load_config()`, `Config.cpanel_ingest`.
- `backupme/core/database.py`: `execute`, `commit`.
- `backupme/backends/__init__.py`: `get_all_backends(config)`.
- `backupme/cli/output.py`: `emit(data, as_json, human=None)` (Task 3).
- `backupme/cli/progress.py`: `Progress(enabled, total, desc)` (Task 3).
- `backupme/core/index.py`: `find_all_files(source_root=...)`, `find_by_path`.
- Tests fixtures: `env` (config+db+cwd; env/config.yaml has one local backend; DB_PATH = env/test.db), `backends`.

## backupme/cli/ingest_cmd.py (new)

```python
import click, json, os
from pathlib import Path
from .base import cli
from ..core import cpanel_ingest as ci
from ..core import database as dbmod
from ..core import index as index_mod
from ..core.database import execute
from ..backends import get_all_backends
from ..config import load_config
from ..cli.output import emit
from ..cli.progress import Progress
```

### Command 1: `ingest-cpanel DROP_ROOT [options]`
- `@click.argument("drop_root", type=click.Path(exists=True, file_okay=False))`
- options: `--config`, `--db-path`, `--redundancy` (default 1), `--account` (override account for all archives — optional), `--keep-raw` (override, int), `--watch` (flag, one-shot ingest loop), `--interval` (default 300), `--json`.
- Behavior:
  - `config = load_config(config_path)`; `backends = get_all_backends(config)`; if `backends` empty → `click.echo("no backends configured"); raise SystemExit(1)`.
  - `cfg_ingest = config.cpanel_ingest`; `drop_root` used as-is; respect `cfg_ingest.drop_root`? NO — `drop_root` is the positional arg; `config.cpanel_ingest.drop_root` is for the daemon tick (not used here).
  - If `--watch`: loop forever: run one ingest_tree iteration, then sleep interval; on KeyboardInterrupt exit cleanly. When watching, no --json output per iteration (progress only via console).
  - Else: one-shot `result = ci.ingest_tree(drop_root, backends, config, redundancy)`.
  - Output via `emit(result, as_json=json_mode, human=human_ingest)` where `human_ingest` renders: status line, per-archive {archive: members indexed OK / skipped: reason / failed: error}.
  - Exit code: 0 if status ok/idle; 1 if partial/failed.

### Command 2: `restore-cpanel ACCOUNT GENERATION [options]`
- args: account, generation (int). options: `--member-filter`, `--out` (default restore_out/<account>), `--retar` (optional tar.gz path), `--json`.
- calls `ci.restore_cpanel(account, generation, member_filter, out, retar, backends=get_all_backends(config), config)`.
- emit result.

### Command 3: `ingest-status [options]`
- `--drop-root` optional. Shows: latest checkpoint (`state_mod.get_last_checkpoint()`), op_log tail of cpanel_ingest ops (`SELECT * FROM op_log WHERE op_type='cpanel_ingest' ORDER BY id DESC LIMIT 10`), count of ingested files per account (`SELECT source_root, COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%' GROUP BY source_root`).
- emit as table or json.

### Registration — `backupme/cli/__init__.py`
Add `from . import ingest_cmd` to the import list (alphabetical-ish placement after existing imports).

## Tests — `tests/test_cli_output.py`
Use CliRunner from click + `env` fixture. Required:
1. `ingest-cpanel` on empty drop root → exit 0, output contains "idle" (json: status == "idle"; human: "idle").
2. `ingest-cpanel` on a drop root with a real cpmove tar.gz → exit 0; json status ok; archives counted; files indexed under `cpanel/<account>` (query DB via index_mod.get_all_files or execute directly).
3. `ingest-cpanel --json` → parseable JSON (json.loads), has "archives" key.
4. `restore-cpanel` on missing account/generation → exit nonzero or status ok with members 0 (no crash).
5. `ingest-status` → exit 0.
6. Invalid drop root (not a dir) → exit 2 (click argument validation) — optional but nice.
7. `--redundancy 2` accepted (no crash).

Run new tests, then FULL suite.

## Report
Write full report to `.superpowers/sdd/task-5-report.md`. Return: status, commit hash, one-line test summary, concerns (max 2 lines).

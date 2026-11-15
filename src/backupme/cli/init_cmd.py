"""`backupme init` — first-run scaffolding for a backup directory.

Before this command existed, a fresh user's first run was a silent no-op:
a missing config.yaml quietly fell back to defaults and every command
created a backupme.db wherever the user happened to be standing. `init`
makes the intended workflow explicit:

  1. scaffold config.yaml from config.example.yaml (never overwrite),
  2. create the SQLite hot store,
  3. print concrete next steps.

Contract notes (also documented in the command help):
  - Exit code is 0 in every outcome that is not a hard failure. Running
    `init` twice is a normal user action, so an existing config.yaml is
    reported as "already_initialized" (exit 0, nothing modified) rather
    than an error. The --json payload carries the machine-readable status.
  - config.example.yaml is copied byte-for-byte (no parse-and-rewrite) so
    the scaffolded file always matches the test-pinned reference template.
  - If the reference template cannot be found (non-editable install
    without repo files), the command says so loudly, still initializes the
    database, and exits 0 — the user is told to create config.yaml by hand.
"""
import shutil
from pathlib import Path

import click

from .base import cli
from .output import emit

EXAMPLE_NAME = "config.example.yaml"


def _find_example_file() -> Path | None:
    """Locate the reference config template.

    Editable installs run from the repo, where config.example.yaml sits at
    the repository root (three levels above src/backupme/cli/). A plain
    copy in the current directory is honored as a second source so the
    command also works from an unpacked release tree.
    """
    repo_root_example = Path(__file__).resolve().parents[3] / EXAMPLE_NAME
    if repo_root_example.is_file():
        return repo_root_example
    cwd_example = Path.cwd() / EXAMPLE_NAME
    if cwd_example.is_file():
        return cwd_example
    return None


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def init(ctx, as_json):
    """Initialize a backup directory: scaffold config.yaml and create the database.

    Copies config.example.yaml to the config path (never overwrites an
    existing config — a second run reports status "already_initialized"
    and exits 0 without changes), then initializes the SQLite database
    and prints the next steps.
    """
    config_path = Path(ctx.obj.get("config_path") or "config.yaml")
    from ..core import database as dbmod

    config_created = False
    warning = None

    if config_path.exists():
        # Refuse branch: never overwrite. Idempotent user action, not an
        # error — hence exit 0 in both modes (see module docstring).
        payload = {
            "status": "already_initialized",
            "config": str(config_path),
            "config_created": False,
            "database": str(dbmod.DB_PATH),
            "database_initialized": False,
        }
        if as_json:
            emit(payload, as_json=True)
        else:
            click.echo(f"{config_path} already exists — nothing changed (init never overwrites).")
            click.echo("Edit config.yaml directly, or see docs/MANUAL.md for the full guide.")
        return

    example = _find_example_file()
    if example is not None:
        # Straight byte-for-byte copy: the example file is test-pinned to the
        # config dataclasses, so parsing and re-emitting it here would only
        # risk drift. Comments in the scaffold are half its value.
        shutil.copyfile(example, config_path)
        config_created = True
        source_note = f" (copied from {example})"
    else:
        source_note = ""
        warning = (f"could not locate {EXAMPLE_NAME} — create {config_path} by hand "
                   "starting from it in the repository")

    # The group callback already ran initialize(), but call it explicitly so
    # this command owns its contract even if base.py changes. Idempotent.
    dbmod.initialize()

    next_steps = [
        "backupme backend add            # configure where backups go (or edit config.yaml)",
        "backupme backup <path> --recursive",
        "backupme web                    # dashboard on http://127.0.0.1:8080",
    ]
    payload = {
        "status": "initialized",
        "config": str(config_path),
        "config_created": config_created,
        "database": str(dbmod.DB_PATH),
        "database_initialized": True,
        "next_steps": next_steps,
    }
    if warning:
        payload["warning"] = warning

    if as_json:
        emit(payload, as_json=True)
        return

    if config_created:
        click.echo(f"Created {config_path}{source_note}")
    else:
        click.echo(f"WARNING: {warning}")
    click.echo(f"Created database {dbmod.DB_PATH}")
    click.echo("Next steps:")
    for i, step in enumerate(next_steps, start=1):
        click.echo(f"  {i}. {step}")
    click.echo("Full guide: docs/MANUAL.md — config.example.yaml documents every option.")

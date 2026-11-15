import sys

import click

from .. import __version__


def _help_only() -> bool:
    """True when this invocation only shows help and must not touch disk.

    ``--help``/``-h`` anywhere on the command line means click will print
    usage text and exit before any command body runs — but the group callback
    below runs FIRST even for subcommand help like ``backupme db --help``, so
    without this check every help invocation used to create a backupme.db in
    the current directory (audit P1-1). Click reads ``sys.argv[1:]`` when no
    args are passed to the group, which is exactly what we scan here.

    Everything after a bare ``--`` is an argument, not an option (click
    honors the same rule), so ``backupme backup -- -h`` still runs for real.
    """
    for arg in sys.argv[1:]:
        if arg == "--":
            return False
        if arg in ("--help", "-h"):
            return True
    return False


@click.group()
@click.version_option(__version__, "--version", prog_name="backupme")
@click.option("--config", "-c", default="config.yaml", help="Config file path")
@click.option("--db-path", "--db", default="backupme.db", help="Database path")
@click.option("--verbose", "-V", is_flag=True, help="Verbose (debug) logging")
@click.pass_context
def cli(ctx, config, db_path, verbose):
    ctx.ensure_object(dict)
    from ..log import setup_logging
    setup_logging(verbose)
    if _help_only():
        # Help-only run: keep ctx.obj's shape sane for anything that peeks at
        # it, but skip every side effect — no BACKUPME_CONFIG export, no config
        # load (which would log the missing-config warning), no DB creation.
        # No command body executes in this case, so nothing needs real values.
        ctx.obj["config"] = None
        ctx.obj["config_path"] = config
        return
    import os
    if config:
        os.environ["BACKUPME_CONFIG"] = config
    from .. import config as cfgmod
    cfg = cfgmod.load_config(config)
    ctx.obj["config"] = cfg
    ctx.obj["config_path"] = config
    import pathlib
    from ..core import database as dbmod
    dbmod.DB_PATH = pathlib.Path(db_path)
    dbmod.initialize()

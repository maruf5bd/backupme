import dataclasses
import time
import click

from .base import cli


@cli.command()
@click.pass_context
def daemon(ctx):
    cfg = ctx.obj["config"]
    if dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
        cfg = dataclasses.asdict(cfg)
    from ..scheduler import BackupScheduler
    from ..backends import get_all_backends
    from ..core import migration
    mresult = migration.run_migrations(get_all_backends(cfg), repair=True)
    if mresult.get("inline_migrated", {}).get("migrated"):
        click.echo(f"Migrated {mresult['inline_migrated']['migrated']} legacy inline file(s)")
    scheduler = BackupScheduler(cfg)
    scheduler.start()
    click.echo("Daemon started. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.stop()
        click.echo("Daemon stopped.")
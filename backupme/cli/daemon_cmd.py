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
    scheduler = BackupScheduler(cfg)
    scheduler.start()
    click.echo("Daemon started. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.stop()
        click.echo("Daemon stopped.")
import time
import click

from .base import cli


@cli.command()
@click.pass_context
def daemon(ctx):
    cfg = ctx.obj["config"]
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
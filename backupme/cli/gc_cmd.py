import click

from .base import cli


@cli.command()
@click.option("--apply", is_flag=True, help="Actually delete orphaned objects (default: dry run)")
@click.pass_context
def gc(ctx, apply):
    """Reclaim remote objects no longer referenced by the index."""
    from ..backends import get_all_backends
    from ..core.gc import run_gc
    result = run_gc(get_all_backends(ctx.obj["config"]), apply=apply)
    if not result:
        click.echo("no backends to scan")
        return
    total_orphans = total_bytes = 0
    for name, r in result.items():
        total_orphans += r["orphans"]
        total_bytes += r["bytes"]
        if apply:
            click.echo(f"{name}: {r['orphans']} orphan(s), {r['bytes']} bytes ({r.get('deleted', 0)} deleted)")
        else:
            click.echo(f"{name}: {r['orphans']} orphan(s), {r['bytes']} bytes (dry run — use --apply to delete)")
    click.echo(f"total: {total_orphans} orphan(s), {total_bytes} bytes")

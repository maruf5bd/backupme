import click

from .base import cli
from ..cli.output import emit


@cli.command()
@click.option("--apply", is_flag=True, help="Actually delete orphaned objects (default: dry run)")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def gc(ctx, apply, as_json):
    """Reclaim remote objects no longer referenced by the index."""
    from ..backends import get_all_backends
    from ..core.gc import run_gc
    result = run_gc(get_all_backends(ctx.obj["config"]), apply=apply)
    if not result:
        emit({"apply": apply, "backends": {}, "total_orphans": 0, "total_bytes": 0},
             as_json=as_json, human=lambda d: click.echo("no backends to scan"))
        return
    total_orphans = total_bytes = 0
    for name, r in result.items():
        total_orphans += r["orphans"]
        total_bytes += r["bytes"]

    def human(d):
        for name, r in d["backends"].items():
            if d["apply"]:
                click.echo(f"{name}: {r['orphans']} orphan(s), {r['bytes']} bytes ({r.get('deleted', 0)} deleted)")
            else:
                click.echo(f"{name}: {r['orphans']} orphan(s), {r['bytes']} bytes (dry run — use --apply to delete)")
        click.echo(f"total: {d['total_orphans']} orphan(s), {d['total_bytes']} bytes")

    emit({"apply": apply, "backends": result,
          "total_orphans": total_orphans, "total_bytes": total_bytes},
         as_json=as_json, human=human)

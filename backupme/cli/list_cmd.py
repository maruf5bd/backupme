import click

from .base import cli


@cli.command("list")
@click.option("--backend", "-b", help="Filter by backend")
@click.option("--hash", "sha256_val", help="Filter by SHA256")
@click.option("--limit", "-l", type=int, default=50)
@click.option("--offset", "-o", type=int, default=0)
@click.pass_context
def list_files(ctx, backend, sha256_val, limit, offset):
    from ..core import index as index_mod
    if sha256_val:
        files = index_mod.find_by_sha256(sha256_val)
    else:
        files = index_mod.get_all_files(limit=limit, offset=offset)
    if backend:
        files = [f for f in files if f.get("backend_name") == backend]
    for f in files:
        click.echo(f"{f['id']:>6}  {f.get('relative_path', f.get('rel_path', '?')):<60}  {f['sha256'][:16]}")


@cli.command()
@click.pass_context
def stats(ctx):
    from ..core import index as index_mod
    from ..core import dedup
    from ..core import database as dbmod
    total_files = index_mod.file_count()
    savings = dedup.total_dedup_savings()
    gen = dbmod.get_generation()
    click.echo(f"Total files: {total_files}")
    click.echo(f"Generation: {gen}")
    click.echo(f"Blobs: {savings['total_blobs']}")
    click.echo(f"Raw bytes: {savings['raw_bytes']}")
    click.echo(f"Stored bytes: {savings['stored_bytes']}")
    click.echo(f"Dedup savings: {savings['savings_bytes']} bytes (ratio: {savings['ratio']:.2f}x)")


@cli.command()
@click.pass_context
def backends(ctx):
    cfg = ctx.obj["config"]
    from ..backends import get_backend, get_all_backends
    backends = get_all_backends(cfg)
    for be in backends:
        status = "connected" if be.connected else "unavailable"
        click.echo(f"{be.name:<20} ({be.type:<8})  {status}")
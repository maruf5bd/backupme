import click

from .base import cli


@cli.command()
@click.argument("query")
@click.option("--content", is_flag=True, help="Search within file content")
@click.option("--source", help="Filter by source root")
@click.pass_context
def search(ctx, query, content, source):
    from ..core import index as index_mod
    if content:
        from ..core.database import execute
        rows = execute(
            "SELECT * FROM files WHERE content_text LIKE ? AND deleted_at IS NULL LIMIT 50",
            (f"%{query}%",),
        ).fetchall()
        results = [dict(r) for r in rows]
    else:
        results = index_mod.search_files(query, source_root=source)
    for r in results:
        click.echo(f"{r['id']:>6}  {r['relative_path']:<60}  {r['sha256'][:16]}")


@cli.command()
@click.argument("query")
@click.pass_context
def select_backend(ctx, query):
    cfg = ctx.obj["config"]
    from ..core import selector
    results = selector.fuzzy_find_backend(query, cfg)
    for r in results:
        click.echo(f"{r['name']:<20} ({r['type']:<8})  score={r.get('score', 1):.2f}")


@cli.command()
@click.argument("sha256")
@click.pass_context
def hash_lookup(ctx, sha256):
    from ..core import dedup
    h = dedup.get_blob(sha256)
    if h:
        click.echo(json.dumps(h, indent=2, default=str))
    else:
        click.echo("Hash not found")
import click

from .base import cli
from ..cli.output import emit


@cli.command("list")
@click.option("--backend", "-b", help="Filter by backend")
@click.option("--hash", "sha256_val", help="Filter by SHA256")
@click.option("--source-root", default=None, help="Filter by source_root")
@click.option("--limit", "-l", type=int, default=50)
@click.option("--offset", "-o", type=int, default=0)
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def list_files(ctx, backend, sha256_val, source_root, limit, offset, as_json):
    from ..core import index as index_mod
    if sha256_val:
        files = index_mod.find_by_sha256(sha256_val)
    else:
        files = index_mod.get_all_files(limit=limit, offset=offset)
    if backend:
        files = [f for f in files if f.get("source_root") == backend]
    if source_root:
        files = [f for f in files if f.get("source_root") == source_root]

    rows = []
    for f in files:
        rows.append({
            "id": f.get("id"),
            "source_root": f.get("source_root"),
            "relative_path": f.get("relative_path", f.get("rel_path")),
            "sha256": f.get("sha256"),
            "size": f.get("size"),
            "version_seq": f.get("version_seq"),
            "version_group": f.get("version_group"),
        })

    def human(data):
        for f in data["files"]:
            click.echo(f"{f['id']:>6}  {f.get('relative_path', '?'):<60}  {str(f.get('sha256', ''))[:16]}")

    emit({"files": rows, "count": len(rows)}, as_json=as_json, human=human)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def stats(ctx, as_json):
    from ..core import index as index_mod
    from ..core import dedup
    from ..core import database as dbmod
    total_files = index_mod.file_count()
    savings = dedup.total_dedup_savings()
    gen = dbmod.get_generation()
    data = {
        "total_files": total_files,
        "generation": gen,
        "blobs": savings["total_blobs"],
        "raw_bytes": savings["raw_bytes"],
        "stored_bytes": savings["stored_bytes"],
        "dedup_savings_bytes": savings["savings_bytes"],
        "ratio": savings["ratio"],
    }

    def human(d):
        click.echo(f"Total files: {d['total_files']}")
        click.echo(f"Generation: {d['generation']}")
        click.echo(f"Blobs: {d['blobs']}")
        click.echo(f"Raw bytes: {d['raw_bytes']}")
        click.echo(f"Stored bytes: {d['stored_bytes']}")
        click.echo(f"Dedup savings: {d['dedup_savings_bytes']} bytes (ratio: {d['ratio']:.2f}x)")

    emit(data, as_json=as_json, human=human)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backends(ctx, as_json):
    cfg = ctx.obj["config"]
    from ..backends import get_all_backends
    bes = get_all_backends(cfg)
    rows = []
    for be in bes:
        rows.append({
            "name": be.name,
            "type": be.type,
            "status": "connected" if be.connected else "unavailable",
            "priority": getattr(be, "priority", None),
            "weight": getattr(be, "weight", None),
        })

    def human(data):
        for be in data["backends"]:
            click.echo(f"{be['name']:<20} ({be['type']:<8})  {be['status']}")

    emit({"backends": rows, "count": len(rows)}, as_json=as_json, human=human)
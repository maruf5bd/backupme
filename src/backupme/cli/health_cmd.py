import json as jsonlib

import click

from .base import cli


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.option("--purge-candidates", is_flag=True, help="Also list purge candidates")
@click.option("--limit", default=20, show_default=True, help="Rows per listing")
@click.pass_context
def health(ctx, as_json, purge_candidates, limit):
    """Data health summary: broken files, missing blobs, same-name groups, purge candidates."""
    from ..core import data_health

    cfg = ctx.obj["config"]
    s = data_health.summary(cfg)
    payload = {"summary": s}
    if purge_candidates:
        payload["purge_candidates_page"] = data_health.purge_candidates(limit, 0)

    if as_json:
        click.echo(jsonlib.dumps(payload, indent=2))
        return

    s = payload["summary"]
    click.echo(f"live files          : {s['live_files']:,}")
    click.echo(f"no-chunk files      : {s['no_chunks_files']:,}")
    click.echo(f"missing-blob files  : {s['missing_blob_files']:,}")
    click.echo(f"missing blobs       : {s['missing_blobs']:,}")
    click.echo(f"same-name groups    : {s['same_name_groups']:,} ({s['same_name_files']:,} files)")
    click.echo(f"purge candidates    : {s['purge_candidates']:,}")
    for row in s.get("backend_blobs", []):
        click.echo(f"  backend {row.get('backend')!r}: {row.get('blobs', 0):,} blob(s)")
    page = payload.get("purge_candidates_page") or {}
    if page.get("items"):
        click.echo(f"purge candidates ({page.get('total', '?')} total):")
        for r in page["items"]:
            click.echo(f"  id={r.get('id')} {r.get('relative_path')}")

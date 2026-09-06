import json
import click

from .base import cli


@cli.group()
def db():
    """Database management commands"""


@db.command()
@click.option("--full", is_flag=True, help="Full integrity check")
@click.option("--repair", is_flag=True, help="Repair ref-counts and migrate legacy inline files")
@click.pass_context
def check(ctx, full, repair):
    from ..core import database as dbmod
    errors = dbmod.check_integrity()
    if errors:
        click.echo("Integrity check FAILED:")
        for e in errors:
            click.echo(f"  {e}")
    else:
        click.echo("ok")
    from ..backends import get_all_backends
    from ..core import migration
    result = migration.run_migrations(get_all_backends(ctx.obj["config"]), repair=repair)
    click.echo(f"refcounts repaired: {result['refcounts_repaired']}")
    if repair:
        m = result["inline_migrated"]
        click.echo(f"inline migrated: {m['migrated']} (lossy: {len(m['lossy'])}, failed: {len(m['failed'])})")
    else:
        click.echo(f"inline pending: {result['inline_pending']}")
    if result["unrecoverable"]:
        click.echo(f"unrecoverable rows: {len(result['unrecoverable'])} (run 'backupme report integrity')")


@db.command()
@click.pass_context
def restore(ctx):
    from ..backends import get_all_backends
    from ..core import recover
    cfg = ctx.obj["config"]
    backends = get_all_backends(cfg)
    result = recover.recover_database(backends)
    click.echo(json.dumps(result, indent=2))


@db.command()
@click.pass_context
def rebuild(ctx):
    from ..core import recover
    result = recover.rebuild_from_manifests()
    click.echo(json.dumps(result, indent=2))


@db.command()
@click.pass_context
def snapshot(ctx):
    from ..core.snapshot import create_snapshot, prune_snapshots
    result = create_snapshot()
    click.echo(f"Snapshot: {result['snapshot']} (gen={result['generation']})")
    prune_snapshots()


@db.command()
@click.pass_context
def info(ctx):
    from ..core import database as dbmod
    version = dbmod.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    file_count = dbmod.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    blob_count = dbmod.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    op_log_count = dbmod.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
    gen = dbmod.get_generation()
    page_count = dbmod.execute("PRAGMA page_count").fetchone()[0]
    page_size = dbmod.execute("PRAGMA page_size").fetchone()[0]
    click.echo(f"Schema version: {version}")
    click.echo(f"Files: {file_count}")
    click.echo(f"Blobs: {blob_count}")
    click.echo(f"Generation: {gen}")
    click.echo(f"Op log entries: {op_log_count}")
    click.echo(f"Database size: {page_count * page_size} bytes")


@db.command()
@click.option("--pending", is_flag=True, help="Show only pending ops")
@click.pass_context
def oplog(ctx, pending):
    from ..core import op_log as oplog_mod
    if pending:
        rows = oplog_mod.get_pending()
    else:
        from ..core.database import execute
        rows = [dict(r) for r in execute("SELECT * FROM op_log ORDER BY id DESC LIMIT 50").fetchall()]
    for r in rows:
        status = r["status"]
        click.echo(f"#{r['id']:>6}  {r['op_type']:<20}  {status:<12}  {r['idempotency_key'][:16]}")
    stats = oplog_mod.get_status_stats()
    click.echo(f"\nStats: {json.dumps(stats)}")
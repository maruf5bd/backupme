import json
import click

from .base import cli
from ..cli.output import emit


@cli.group()
def db():
    """Database management commands"""


@db.command()
@click.option("--full", is_flag=True, help="Full integrity check")
@click.option("--repair", is_flag=True, help="Repair ref-counts and migrate legacy inline files")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def check(ctx, full, repair, as_json):
    from ..core import database as dbmod
    errors = dbmod.check_integrity()
    from ..backends import get_all_backends
    from ..core import migration
    result = migration.run_migrations(get_all_backends(ctx.obj["config"]), repair=repair)
    data = {
        "integrity": "ok" if not errors else "failed",
        "errors": errors,
        "refcounts_repaired": result["refcounts_repaired"],
        "inline_pending": result.get("inline_pending"),
        "inline_migrated": result.get("inline_migrated"),
        "unrecoverable_count": len(result.get("unrecoverable", [])),
    }

    def human(d):
        if d["errors"]:
            click.echo("Integrity check FAILED:")
            for e in d["errors"]:
                click.echo(f"  {e}")
        else:
            click.echo("ok")
        click.echo(f"refcounts repaired: {d['refcounts_repaired']}")
        if repair:
            m = (d["inline_migrated"] or {})
            click.echo(f"inline migrated: {m.get('migrated', 0)} (lossy: {len(m.get('lossy', []))}, failed: {len(m.get('failed', []))})")
        else:
            click.echo(f"inline pending: {d['inline_pending']}")
        if d["unrecoverable_count"]:
            click.echo(f"unrecoverable rows: {d['unrecoverable_count']} (run 'backupme report integrity')")

    emit(data, as_json=as_json, human=human)


@db.command()
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def restore(ctx, as_json):
    from ..backends import get_all_backends
    from ..core import recover
    cfg = ctx.obj["config"]
    backends = get_all_backends(cfg)
    result = recover.recover_database(backends)
    emit(result, as_json=as_json, human=lambda d: click.echo(json.dumps(d, indent=2)))


@db.command()
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def rebuild(ctx, as_json):
    from ..core import recover
    result = recover.rebuild_from_manifests()
    emit(result, as_json=as_json, human=lambda d: click.echo(json.dumps(d, indent=2)))


@db.command()
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def snapshot(ctx, as_json):
    from ..core.snapshot import create_snapshot, prune_snapshots
    result = create_snapshot()
    prune_snapshots()
    data = {"snapshot": result["snapshot"], "generation": result["generation"]}
    emit(data, as_json=as_json, human=lambda d: click.echo(f"Snapshot: {d['snapshot']} (gen={d['generation']})"))


@db.command()
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def info(ctx, as_json):
    from ..core import database as dbmod
    version = dbmod.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    file_count = dbmod.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    blob_count = dbmod.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    op_log_count = dbmod.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
    gen = dbmod.get_generation()
    page_count = dbmod.execute("PRAGMA page_count").fetchone()[0]
    page_size = dbmod.execute("PRAGMA page_size").fetchone()[0]
    data = {
        "schema_version": version,
        "files": file_count,
        "blobs": blob_count,
        "generation": gen,
        "op_log_entries": op_log_count,
        "database_size_bytes": page_count * page_size,
    }

    def human(d):
        click.echo(f"Schema version: {d['schema_version']}")
        click.echo(f"Files: {d['files']}")
        click.echo(f"Blobs: {d['blobs']}")
        click.echo(f"Generation: {d['generation']}")
        click.echo(f"Op log entries: {d['op_log_entries']}")
        click.echo(f"Database size: {d['database_size_bytes']} bytes")

    emit(data, as_json=as_json, human=human)


@db.command()
@click.option("--pending", is_flag=True, help="Show only pending ops")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def oplog(ctx, pending, as_json):
    from ..core import op_log as oplog_mod
    if pending:
        rows = oplog_mod.get_pending()
    else:
        from ..core.database import execute
        rows = [dict(r) for r in execute("SELECT * FROM op_log ORDER BY id DESC LIMIT 50").fetchall()]
    stats = oplog_mod.get_status_stats()
    data = {"rows": rows, "stats": stats}

    def human(d):
        for r in d["rows"]:
            status = r["status"]
            click.echo(f"#{r['id']:>6}  {r['op_type']:<20}  {status:<12}  {r['idempotency_key'][:16]}")
        click.echo(f"\nStats: {json.dumps(d['stats'])}")

    emit(data, as_json=as_json, human=human)
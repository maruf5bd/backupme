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
    result = migration.run_migrations(
        get_all_backends(ctx.obj["config"]), repair=repair, reconcile=repair)
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
@click.option("--offsite", is_flag=True,
              help="Also ship the snapshot to every reachable backend (off-site guard)")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def snapshot(ctx, offsite, as_json):
    from ..core.snapshot import create_snapshot, prune_snapshots
    result = create_snapshot()
    ship = None
    if offsite:
        from ..core import offsite_guard
        ship = offsite_guard.ship_snapshot(ctx.obj["config"], snapshot=result)
    prune_snapshots()
    data = {"snapshot": result["snapshot"], "generation": result["generation"]}
    if ship is not None:
        data["offsite"] = ship

    def human(d):
        click.echo(f"Snapshot: {d['snapshot']} (gen={d['generation']})")
        guard_result = d.get("offsite")
        if guard_result:
            click.echo(f"Off-site: {guard_result.get('status')}")
            for name, entry in (guard_result.get("backends") or {}).items():
                if entry.get("status") == "ok":
                    click.echo(f"  {name}: ok {entry.get('remote_path')} "
                               f"({entry.get('size')} bytes, verify={entry.get('verify')})")
                else:
                    click.echo(f"  {name}: error {entry.get('error', 'unknown')}")

    emit(data, as_json=as_json, human=human)


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


@db.command("cold-export")
@click.option("--dry-run", is_flag=True, help="Report what would be exported without writing")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def cold_export(ctx, dry_run, as_json):
    """Export files rows past the watermark to monthly Zstd Parquet partitions."""
    from ..core import db_lifecycle as lc
    if dry_run:
        from ..core.database import execute
        pdir = lc.parquet_dir_for()
        watermark = lc.load_watermark(pdir)
        row = execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM files WHERE id > ?",
                      (watermark,)).fetchone()
        data = {"status": "dry-run", "watermark": watermark,
                "eligible_rows": row[0], "max_id": row[1],
                "parquet_dir": str(pdir)}
        emit(data, as_json=as_json, human=lambda d: (
            click.echo(f"parquet_dir: {d['parquet_dir']}"),
            click.echo(f"watermark: {d['watermark']}"),
            click.echo(f"eligible rows: {d['eligible_rows']} (max id {d['max_id']})"),
        ))
        return
    result = lc.cold_export_to_parquet()
    emit(result, as_json=as_json, human=lambda d: (
        click.echo(f"status: {d.get('status')}"),
        click.echo(f"partition: {d.get('partition')}") if d.get("partition") else None,
        click.echo(f"rows exported: {d.get('rows')}"),
        click.echo(f"watermark: {d.get('watermark')}"),
        click.echo(f"bytes: {d.get('bytes')}") if d.get("bytes") else None,
        click.echo(f"reason: {d.get('reason')}") if d.get("reason") else None,
    ))


@db.command("purge")
@click.option("--grace-days", type=int, default=None, help="Override purge grace days")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def purge(ctx, grace_days, as_json):
    """Hard-delete soft-deleted rows past grace that are already cold-exported."""
    from ..core import db_lifecycle as lc
    result = lc.hard_purge_versions(grace_days)
    emit(result, as_json=as_json, human=lambda d: click.echo(
        f"purged: {d['purged']} (above watermark: {d['above_watermark']} kept, "
        f"watermark {d['watermark']}, grace {d['grace_days']}d)"))


@db.command("history")
@click.argument("sql")
@click.option("--json/--no-json", "as_json", default=True,
              help="JSON output (default); --no-json renders a human table")
@click.pass_context
def history(ctx, sql, as_json):
    """Run read-only DuckDB SQL over the parquet partitions (view: files)."""
    from ..core import db_lifecycle as lc
    rows = lc.query_history_duckdb(sql)
    payload = {"rows": rows, "count": len(rows)}

    _MAX_RENDER_ROWS = 100
    _MAX_CELL_WIDTH = 40

    def _render_history_table(rows):
        columns = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        cells = [
            {key: _truncate(row.get(key)) for key in columns}
            for row in rows
        ]
        widths = {key: max(len(key), *(len(row[key]) for row in cells)) for key in columns}
        click.echo("  ".join(key.ljust(widths[key]) for key in columns))
        click.echo("  ".join("-" * widths[key] for key in columns))
        for row in cells:
            click.echo("  ".join(row[key].ljust(widths[key]) for key in columns))

    def _truncate(value) -> str:
        text = "None" if value is None else str(value)
        return text if len(text) <= _MAX_CELL_WIDTH else text[:_MAX_CELL_WIDTH - 3] + "..."

    def human(d):
        rows = d["rows"]
        if not rows:
            click.echo("(no rows)")
        else:
            _render_history_table(rows[:_MAX_RENDER_ROWS])
            if len(rows) > _MAX_RENDER_ROWS:
                click.echo(f"... {len(rows) - _MAX_RENDER_ROWS} more row(s) not shown")
        click.echo(f"{d['count']} row(s)")

    emit(payload, as_json=as_json, human=human)


@db.command("vacuum")
@click.pass_context
def vacuum(ctx):
    """VACUUM the hot SQLite database."""
    from ..core import db_lifecycle as lc
    result = lc.vacuum_if_needed()
    click.echo("vacuumed" if result.get("vacuumed") else "vacuum failed")


@db.command("prune-oplog")
@click.option("--days", type=int, default=30, help="Archive op_log rows older than N days")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def prune_oplog_cmd(ctx, days, as_json):
    """Archive terminal op_log rows to JSONL and remove them from the hot DB."""
    from ..core import db_lifecycle as lc
    result = lc.prune_oplog(days)
    emit(result, as_json=as_json, human=lambda d: (
        click.echo(f"pruned: {d['pruned']}"),
        click.echo(f"file: {d['file']}") if d.get("file") else None,
    ))


@db.command("recover-from-manifests")
@click.argument("backend")
@click.option("--into", "into_path", default=None,
              help="Path of the NEW sqlite file to build (an existing non-empty "
                   "file is refused; the live DB is never touched). "
                   "Default: recover-<UTC ts>.db")
@click.option("--verify", type=click.Choice(["full", "none"]), default="full",
              show_default=True,
              help="full: download + decompress + sha256 every chunk (default). "
                   "none: trust listings, no downloads or hashes (bundle tars "
                   "are still parsed for member ranges)")
@click.option("--dry-run", is_flag=True,
              help="Scan + verify + report counts only; no sqlite file is written")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def recover_from_manifests_cmd(ctx, backend, into_path, verify, dry_run, as_json):
    """Rebuild a fresh database from the ARCHIVE manifest sidecars on BACKEND.

    Safety: this command never opens or writes the live database — it only
    reads manifests + blobs from the backend and writes the --into file.
    (The FILE-level counterpart is `backupme db rebuild`, which works from
    the per-file receipts under manifests/.)
    """
    cfg = ctx.obj["config"]
    names = {b.name for blist in cfg.backends.values() for b in blist}
    if backend not in names:
        click.echo(f"Error: backend '{backend}' not found")
        ctx.exit(1)
    from ..core.manifest_recover import recover_from_manifests, ManifestRecoverError
    try:
        result = recover_from_manifests(cfg, backend, into=into_path,
                                        verify=verify, dry_run=dry_run)
    except ManifestRecoverError as e:
        click.echo(f"Error: {e}")
        ctx.exit(1)

    def human(d):
        click.echo(f"status: {d['status']}  backend: {d['backend']}  "
                   f"verify: {d['verify']}")
        click.echo(f"into: {d['into']}")
        click.echo(f"manifests scanned: {d['manifests_scanned']} "
                   f"(receipts skipped: {d['skipped_receipts']})")
        click.echo(f"archives recovered: {d['archives_recovered']}  "
                   f"members: {d['members_recovered']}  "
                   f"blobs: {d['blobs_recovered']}  "
                   f"bytes: {d['bytes_recoverable']}")
        if d["problems"]:
            click.echo(f"problems: {len(d['problems'])}")
            for p in d["problems"][:20]:
                miss = f" missing={','.join(p.get('missing') or [])}" if p.get("missing") else ""
                click.echo(f"  [{p.get('kind')}] {p.get('fingerprint') or '-'} "
                           f"{p.get('path') or '-'}{miss} — {p.get('error')}")
            if len(d["problems"]) > 20:
                click.echo(f"  ... {len(d['problems']) - 20} more (use --json)")

    emit(result, as_json=as_json, human=human)


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


@db.command("backfill-content")
@click.option("--limit", type=int, default=500, show_default=True,
              help="Max files to process in this run")
@click.option("--max-size", "max_size", type=int, default=65536, show_default=True,
              help="Only files up to this many bytes are indexed")
@click.option("--backend", "backend_name", default=None,
              help="Only files with at least one chunk copy on this backend")
@click.option("--dry-run", is_flag=True,
              help="Count candidates only; no downloads, no updates")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backfill_content_cmd(ctx, limit, max_size, backend_name, dry_run, as_json):
    """Backfill content_text for existing rows (small UTF-8 text files only).

    Makes content search work on historical data. Small text files only;
    never runs when encryption is enabled; resumable — re-run until
    'remaining' is 0.
    """
    cfg = ctx.obj["config"]
    names = {b.name for blist in cfg.backends.values() for b in blist}
    if backend_name and backend_name not in names:
        # An unknown backend would otherwise silently plan/process nothing
        # (same validation the verify command applies).
        click.echo(f"Error: backend '{backend_name}' not found")
        ctx.exit(1)
    if not names:
        click.echo("Error: no backends configured")
        ctx.exit(1)
    from ..core.content_backfill import backfill_content
    result = backfill_content(cfg, limit=limit, max_size=max_size,
                              backend=backend_name, dry_run=dry_run)

    def human(d):
        line = (f"status: {d['status']}  scanned: {d['scanned']}  "
                f"updated: {d['updated']}  binary skipped: {d['skipped_binary']}  "
                f"too large: {d['too_large']}  errors: {d['errors']}  "
                f"remaining: {d['remaining']}")
        if "candidates" in d:
            line += f"  candidates: {d['candidates']}"
        click.echo(line)

    emit(result, as_json=as_json, human=human)
    ctx.exit(0)
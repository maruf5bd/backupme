"""CLI commands for the cPanel ingest pipeline: ingest-cpanel, restore-cpanel, ingest-status."""
import os
import time

import click
from pathlib import Path

from .base import cli

from ..core import cpanel_ingest as ci
from ..core import state as state_mod
from ..backends import get_all_backends
from ..config import load_config
from ..cli.output import emit


def _human_ingest(result):
    status = result.get("status")
    click.echo(f"status: {status}")
    if result.get("archives") is not None:
        click.echo(f"archives: {result.get('archives')}")
    for r in result.get("results", []) or []:
        arch = os.path.basename(r.get("archive", "?"))
        st = r.get("status")
        if st == "ok":
            click.echo(f"  {arch}: {r.get('members', 0)} members indexed")
        elif st == "skipped":
            click.echo(f"  {arch}: skipped ({r.get('reason')})")
        elif st == "failed":
            click.echo(f"  {arch}: FAILED ({r.get('error')})")
        elif st == "deferred":
            click.echo(f"  {arch}: deferred ({r.get('reason')})")
        else:
            click.echo(f"  {arch}: {st}")
    if result.get("files_ignored"):
        click.echo(f"files ignored: {result['files_ignored']}")
    if result.get("error"):
        click.echo(f"error: {result['error']}")


def _human_restore(result):
    click.echo(f"status: {result.get('status')}")
    click.echo(f"account: {result.get('account')}")
    click.echo(f"generation: {result.get('generation')}")
    click.echo(f"members restored: {result.get('members')}")
    click.echo(f"out: {result.get('out')}")
    if result.get("error"):
        click.echo(f"error: {result['error']}")
    if result.get("retar"):
        click.echo(f"retar: {result['retar']}")


def _human_ingest_status(result):
    ck = result.get("checkpoint")
    if ck is None:
        click.echo("checkpoint: (none)")
    else:
        click.echo(f"checkpoint: {ck.get('type')} @ {ck.get('timestamp')}")
        data = ck.get("data") or {}
        click.echo(f"  archive: {data.get('archive')}")
        click.echo(f"  done: {data.get('done')}/{data.get('total')}")
    click.echo("op_log (cpanel_ingest):")
    for op in result.get("ops", []):
        click.echo(f"  id={op.get('id')} status={op.get('status')} key={op.get('idempotency_key')}")
    click.echo("ingested files per account:")
    for a in result.get("accounts", []):
        click.echo(f"  {a['source_root']}: {a['files']} files, {a['bytes']} bytes")
    dedup = result.get("dedup") or {}
    click.echo(f"dedup (cpanel sources): ratio {dedup.get('ratio', 1.0)}x "
               f"(logical {dedup.get('logical_bytes', 0)} B, unique {dedup.get('unique_bytes', 0)} B)")
    storage = result.get("storage") or {}
    click.echo(f"storage: sqlite {storage.get('sqlite_size', 0)} B, "
               f"parquet {storage.get('parquet_size', 0)} B "
               f"({len(storage.get('parquet_partitions', []))} partition(s))")
    gens = result.get("generations") or []
    if gens:
        click.echo("recent generations:")
        for g in gens[:10]:
            click.echo(f"  {g['source_root']} gen {g['generation']}: {g['members']} members, {g['bytes']} bytes")


@cli.command("ingest-cpanel")
@click.argument("drop_root", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--redundancy", "-n", type=int, default=1, help="Redundancy")
@click.option("--keep-raw", type=int, default=None, help="Keep raw generations")
@click.option("--dry-run", is_flag=True, help="List archives and member estimates without ingesting")
@click.option("--progress", is_flag=True, help="Show per-member progress (percentage lines)")
@click.option("--watch", is_flag=True, help="Watch mode: re-scan until Ctrl+C")
@click.option("--interval", type=int, default=300, help="Watch re-scan interval seconds")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def ingest_cpanel(ctx, drop_root, redundancy, keep_raw, dry_run, progress,
                  watch, interval, as_json):
    """Ingest cpmove-*.tar.gz archives from DROP_ROOT into the vault.

    Config and database come from the global --config/--db options.
    """
    cfg = load_config(ctx.obj.get("config_path", "config.yaml"))
    backends = get_all_backends(cfg)
    if not backends:
        click.echo("no backends configured")
        raise SystemExit(2)

    if keep_raw is not None:
        cfg.cpanel_ingest.keep_raw = keep_raw

    if dry_run:
        discovered = ci.discover(drop_root)
        archives = [d for d in discovered if d["type"] == "archive"]
        entries = []
        for item in archives:
            try:
                est = ci.estimate_archive(item["path"])
            except Exception as e:
                est = {"members": 0, "bytes": 0, "skipped": 0, "error": str(e)}
            entries.append({"archive": item["path"], "account": item["account"], **est})
        result = {"status": "dry-run", "archives": len(archives),
                  "files_ignored": sum(1 for d in discovered if d["type"] == "file"),
                  "entries": entries}
        emit(result, as_json=as_json, human=_human_dry_run)
        return

    progress_cb = None
    if progress and not as_json:
        from ..cli.progress import Progress
        bar = Progress(enabled=True, total=None, desc="ingest")
        bar.__enter__()
        seen = {"count": 0}

        def cb(member, done, total):
            seen["count"] += 1
            bar.update(1)

        progress_cb = cb

    if watch:
        try:
            while True:
                try:
                    res = ci.ingest_tree(drop_root, backends, cfg, redundancy,
                                         progress_cb=progress_cb)
                except KeyboardInterrupt:
                    click.echo("stopped")
                    return
                click.echo(f"ingest status: {res.get('status')} archives={res.get('archives')}")
                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    click.echo("stopped")
                    return
        finally:
            if progress_cb:
                bar.finish()

    result = ci.ingest_tree(drop_root, backends, cfg, redundancy,
                            progress_cb=progress_cb)
    if progress_cb:
        bar.finish()
    emit(result, as_json=as_json, human=_human_ingest)
    if result.get("status") in ("partial", "failed"):
        raise SystemExit(1)


def _human_dry_run(result):
    click.echo(f"status: {result['status']} — {result['archives']} archive(s), "
               f"{result['files_ignored']} other file(s) ignored")
    for e in result["entries"]:
        if "error" in e:
            click.echo(f"  {e['archive']}: ERROR ({e['error']})")
        else:
            click.echo(f"  [{e['account']}] {e['archive']}: "
                       f"{e['members']} member(s), {e['bytes']} bytes, "
                       f"{e['skipped']} skipped")


@cli.command("restore-cpanel")
@click.argument("account")
@click.argument("generation", type=int)
@click.option("--member-filter", default=None, help="fnmatch filter on member path")
@click.option("--out", default=None, help="Output directory (default: restore_out/<account>)")
@click.option("--retar", default=None, help="Write a tar.gz containing restored members")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def restore_cpanel(ctx, account, generation, member_filter, out, retar, as_json):
    """Restore generation N of a cPanel account's ingested files."""
    cfg = load_config(ctx.obj.get("config_path", "config.yaml"))
    backends = get_all_backends(cfg)
    if out is None:
        out = f"restore_out/{account}"
    result = ci.restore_cpanel(account, generation, member_filter, out, retar,
                               backends=backends, config=cfg)
    emit(result, as_json=as_json, human=_human_restore)
    if result.get("status") != "ok":
        raise SystemExit(1)


@cli.command("ingest-status")
@click.option("--account", default=None, help="Filter summary to one account")
@click.option("--drop-root", default=None, help="Drop root (informational)")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def ingest_status(ctx, account, drop_root, as_json):
    """cPanel ingest summary: generations, dedup ratio, SQLite + Parquet sizes."""
    from ..core.database import execute
    from ..core import db_lifecycle as lc

    ck = state_mod.get_last_checkpoint()
    ops = [dict(r) for r in execute(
        "SELECT * FROM op_log WHERE op_type='cpanel_ingest' ORDER BY id DESC LIMIT 10"
    ).fetchall()]

    acct_filter = f"cpanel/{account}" if account else "cpanel/%"
    counts = execute(
        "SELECT source_root, COUNT(*) AS files, SUM(size) AS bytes "
        "FROM files WHERE source_root LIKE ? AND deleted_at IS NULL GROUP BY source_root",
        (acct_filter,),
    ).fetchall()

    generations = execute(
        "SELECT source_root, version_seq AS generation, COUNT(*) AS members, SUM(size) AS bytes "
        "FROM files WHERE source_root LIKE ? AND deleted_at IS NULL "
        "GROUP BY source_root, version_seq ORDER BY source_root, version_seq DESC LIMIT 20",
        (acct_filter,),
    ).fetchall()

    dedup_row = execute(
        "SELECT COUNT(DISTINCT fc.blob_id) AS unique_blobs, "
        "COALESCE(SUM(b.pt_size), 0) AS unique_bytes, "
        "COALESCE((SELECT SUM(f2.size) FROM files f2 WHERE f2.source_root LIKE ? AND f2.deleted_at IS NULL), 0) AS logical_bytes "
        "FROM files f "
        "JOIN file_chunks fc ON fc.file_id = f.id "
        "JOIN blobs b ON b.blob_id = fc.blob_id "
        "WHERE f.source_root LIKE ? AND f.deleted_at IS NULL",
        (acct_filter, acct_filter),
    ).fetchone()
    logical = dedup_row["logical_bytes"] or 0
    unique = dedup_row["unique_bytes"] or 0
    ratio = (logical / unique) if unique > 0 else 1.0

    from ..core import database as dbmod
    db_file = Path(str(dbmod.DB_PATH))
    sqlite_size = db_file.stat().st_size if db_file.exists() else 0
    pdir = lc.parquet_dir_for()
    parquet_size = sum(p.stat().st_size for p in pdir.glob("files_*.parquet"))
    partitions = sorted(p.name for p in pdir.glob("files_*.parquet"))

    result = {
        "drop_root": drop_root,
        "checkpoint": ck,
        "ops": ops,
        "accounts": [{"source_root": r["source_root"], "files": r["files"], "bytes": r["bytes"]}
                     for r in counts],
        "generations": [dict(r) for r in generations],
        "dedup": {"logical_bytes": logical, "unique_bytes": unique, "ratio": round(ratio, 3)},
        "storage": {"sqlite_size": sqlite_size, "parquet_size": parquet_size,
                    "parquet_partitions": partitions},
    }
    emit(result, as_json=as_json, human=_human_ingest_status)

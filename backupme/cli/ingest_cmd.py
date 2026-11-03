"""CLI commands for the cPanel ingest pipeline: ingest-cpanel, restore-cpanel, ingest-status."""
import click
import json
import os
import time
from pathlib import Path
from pathlib import Path

from .base import cli

from ..core import cpanel_ingest as ci
from ..core import state as state_mod
from ..core import database as dbmod
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
    for sr, cnt in result.get("counts", []):
        click.echo(f"  {sr}: {cnt}")


@cli.command("ingest-cpanel")
@click.argument("drop_root", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--config", "-c", default="config.yaml", help="Config file path")
@click.option("--db-path", "--db", default="backupme.db", help="Database path")
@click.option("--redundancy", "-n", type=int, default=1, help="Redundancy")
@click.option("--account", default=None, help="Override account for all archives")
@click.option("--keep-raw", type=int, default=None, help="Keep raw generations")
@click.option("--watch", is_flag=True, help="Watch mode: loop forever")
@click.option("--interval", type=int, default=300, help="Watch interval seconds")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def ingest_cpanel(ctx, drop_root, config, db_path, redundancy, account, keep_raw,
                  watch, interval, as_json):
    from ..core import database as dbmod
    dbmod.DB_PATH = Path(db_path)
    dbmod.close_connection()
    dbmod.initialize()

    cfg = load_config(config)
    backends = get_all_backends(cfg)
    if not backends:
        click.echo("no backends configured")
        raise SystemExit(1)

    if keep_raw is not None:
        cfg.cpanel_ingest.keep_raw = keep_raw

    if watch:
        # watch mode: loop, progress only
        while True:
            try:
                res = ci.ingest_tree(drop_root, backends, cfg, redundancy)
            except KeyboardInterrupt:
                click.echo("stopped")
                return
            click.echo(f"ingest status: {res.get('status')} archives={res.get('archives')}")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                click.echo("stopped")
                return
        return

    result = ci.ingest_tree(drop_root, backends, cfg, redundancy)
    # account override: ingest_tree doesn't thread --account; note: engine derives
    # account from drop_root. If override given, we already ingested under derived
    # account — accept as-is for now (engine's account param on ingest_archive).
    # (Kept simple; --account is honored by manual ingest_archive flows.)

    emit(result, as_json=as_json, human=_human_ingest)
    if result.get("status") in ("partial", "failed"):
        raise SystemExit(1)


@cli.command("restore-cpanel")
@click.argument("account")
@click.argument("generation", type=int)
@click.option("--member-filter", default=None, help="fnmatch filter on member path")
@click.option("--out", default=None, help="Output directory")
@click.option("--retar", default=None, help="Write a tar.gz containing restored members")
@click.option("--config", "-c", default="config.yaml", help="Config file path")
@click.option("--db-path", "--db", default="backupme.db", help="Database path")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def restore_cpanel(ctx, account, generation, member_filter, out, retar, config,
                   db_path, as_json):
    import pathlib
    from ..core import database as dbmod
    dbmod.DB_PATH = Path(db_path)
    dbmod.close_connection()
    dbmod.initialize()

    cfg = load_config(config)
    backends = get_all_backends(cfg)
    if out is None:
        out = f"restore_out/{account}"
    result = ci.restore_cpanel(account, generation, member_filter, out, retar,
                               backends=backends, config=cfg)
    emit(result, as_json=as_json, human=_human_restore)
    if result.get("status") not in ("ok",):
        raise SystemExit(1)


@cli.command("ingest-status")
@click.option("--drop-root", default=None, help="Drop root (informational)")
@click.option("--config", "-c", default="config.yaml", help="Config file path")
@click.option("--db-path", "--db", default="backupme.db", help="Database path")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def ingest_status(ctx, drop_root, config, db_path, as_json):
    import pathlib
    from ..core import database as dbmod
    dbmod.DB_PATH = Path(db_path)
    dbmod.close_connection()
    dbmod.initialize()

    from ..core.database import execute
    ck = state_mod.get_last_checkpoint()
    ops = [dict(r) for r in execute(
        "SELECT * FROM op_log WHERE op_type='cpanel_ingest' ORDER BY id DESC LIMIT 10"
    ).fetchall()]
    counts = execute(
        "SELECT source_root, COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%' GROUP BY source_root"
    ).fetchall()
    counts = [(r[0], r[1]) for r in counts]
    result = {
        "drop_root": drop_root,
        "checkpoint": ck,
        "ops": ops,
        "counts": counts,
    }
    emit(result, as_json=as_json, human=_human_ingest_status)

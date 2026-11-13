import click

from .base import cli


@cli.command()
@click.option("--count", type=int, default=None, help="Files to restore-check (default: verification.verify_restore.count)")
@click.option("--backend", "backend_name", default=None, help="Restrict the check to one backend name")
@click.option("--file", "file_id", type=int, default=None, help="Verify a single file id instead of a sample")
@click.option("--heal", is_flag=True, default=False, help="Automatically heal missing/damaged chunks using healthy donor backends or source")
@click.option("--audit-disk", is_flag=True, default=False, help="Audit physical presence of files on source disk vs DB")
@click.option("--audit-storage", is_flag=True, default=False, help="Audit physical presence of blobs/bundles on storage backends vs DB")
@click.pass_context
def verify(ctx, count, backend_name, file_id, heal, audit_disk, audit_storage):
    """Restore-and-hash verification, automated self-healing, and physical presence auditing."""
    if file_id is not None and heal:
        from ..core.verify import heal_damaged_file
        res = heal_damaged_file(file_id, backend_name)
        click.echo(f"Self-heal file id={file_id}: {res['status']} (healed_chunks={res['healed_chunks']})")
        if res.get("errors"):
            for err in res["errors"]:
                click.echo(f"  error: {err}")
        ctx.exit(0 if res["status"] in ("healed", "partial") else 1)
    if audit_disk:
        from ..core.presence_auditor import audit_source_disk_presence
        limit = count or 1000
        click.echo(f"Auditing source disk presence (sampling up to {limit} files)...")
        res = audit_source_disk_presence(limit=limit)
        click.echo(f"Total checked: {res['total_checked']}")
        click.echo(f"Present on local disk: {res['present_on_disk']}")
        click.echo(f"Source deleted/gone: {res['source_gone']}")
        if res["samples_gone"]:
            click.echo("\nSample files missing on disk:")
            for s in res["samples_gone"][:5]:
                click.echo(f"  id={s['file_id']} {s['path']}")
        ctx.exit(0)

    if audit_storage:
        from ..core.presence_auditor import audit_storage_existence
        limit = count or 1000
        click.echo(f"Auditing physical backend storage presence (sampling up to {limit} blobs)...")
        res = audit_storage_existence(limit=limit, backend_filter=backend_name)
        click.echo(f"Total checked: {res['total_checked']}")
        click.echo(f"Present on storage: {res['present']}")
        click.echo(f"Missing on storage: {res['missing']}")
        click.echo(f"Bundle members resolved: {res['bundle_members_resolved']}")
        if res["missing_samples"]:
            click.echo("\nSample blobs missing on storage:")
            for s in res["missing_samples"][:5]:
                click.echo(f"  blob={s['blob_id'][:16]}.. backend={s['backend']} path={s.get('path')}")
        ctx.exit(0 if res["missing"] == 0 else 1)
    if backend_name:
        # An unknown backend silently sampled 0 files and exited 0 (qa P2) —
        # validate the name against the configured backends first.
        names = {b.name for blist in ctx.obj["config"].backends.values() for b in blist}
        if backend_name not in names:
            click.echo(f"Error: backend '{backend_name}' not found")
            ctx.exit(1)
    if file_id is not None:
        from ..core.verify import verify_file_integrity

        result = verify_file_integrity(file_id, backend_name)
        status = result.get("status")
        if status == "ok":
            click.echo(f"id={file_id} OK  {result.get('file')}")
            ctx.exit(0)
        click.echo(f"id={file_id} {status.upper()}  {result.get('file') or result.get('message')}")
        if result.get("error"):
            click.echo(f"  error: {result['error']}")
        ctx.exit(1)
    from ..core.verify import run_integrity_check

    if count is None:
        count = ctx.obj["config"].verification.verify_restore_count or 5
    result = run_integrity_check("manual", count, backend_name, auto_heal=heal)
    click.echo(f"integrity check #{result['check_id']}: {result['status']} "
               f"(checked={result['checked']} failed={result['failed']})")
    ctx.exit(0 if result["failed"] == 0 else 1)

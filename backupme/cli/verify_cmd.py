import click

from .base import cli


@cli.command()
@click.option("--count", type=int, default=None, help="Files to restore-check (default: verification.verify_restore.count)")
@click.option("--backend", "backend_name", default=None, help="Restrict the check to one backend name")
@click.option("--file", "file_id", type=int, default=None, help="Verify a single file id instead of a sample")
@click.pass_context
def verify(ctx, count, backend_name, file_id):
    """Restore-and-hash verification: download files and compare sha256."""
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
    result = run_integrity_check("manual", count, backend_name)
    click.echo(f"integrity check #{result['check_id']}: {result['status']} "
               f"(checked={result['checked']} failed={result['failed']})")
    ctx.exit(0 if result["failed"] == 0 else 1)

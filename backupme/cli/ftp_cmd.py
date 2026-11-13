"""CLI command: backupme ingest-ftp"""
import click

from .base import cli
from ..config import load_config
from ..backends import get_all_backends
from ..core.ftp_ingest import FTPFileProcessor


@cli.command("ingest-ftp")
@click.option("--host", prompt="Source FTP Host", help="FTP hostname or IP address")
@click.option("--port", default=21, type=int, help="FTP port (default 21)")
@click.option("--user", prompt="FTP Username", default="anonymous", help="FTP username")
@click.option("--password", prompt="FTP Password", hide_input=True, default="", help="FTP password")
@click.option("--remote-dir", default="/", help="Remote directory path to ingest")
@click.option("--dest", default="auto", help="Destination backend name or 'auto'")
@click.option("--delete/--no-delete", "delete_source", default=True, help="Delete file from source FTP after successful backup")
@click.pass_context
def ingest_ftp_cmd(ctx, host, port, user, password, remote_dir, dest, delete_source):
    """
    Ingest files from an FTP source one by one, verify locally,
    back them up to destination storage, and delete from source FTP.
    """
    config = ctx.obj["config"] if "config" in ctx.obj else load_config()
    backends = get_all_backends(config)
    available_names = [b.name for b in backends]

    target_backend = None
    if dest and dest != "auto":
        if dest not in available_names:
            click.echo(f"Error: destination backend '{dest}' not found. Available backends: {', '.join(available_names)}")
            ctx.exit(1)
        target_backend = dest
    else:
        click.echo(f"Destination: AUTO (replicates across default backends: {', '.join(available_names)})")

    click.echo(f"Starting sequential FTP ingest from {host}:{port}{remote_dir}")
    click.echo(f"Delete source file after upload: {'YES' if delete_source else 'NO'}\n")

    processor = FTPFileProcessor(
        host=host,
        port=port,
        username=user,
        password=password,
        remote_dir=remote_dir,
        delete_source=delete_source,
        target_backend_name=target_backend,
    )

    results = processor.run_all()
    
    click.echo("\n================ INGEST SUMMARY ================")
    succeeded = sum(1 for r in results if r["status"] in ("ok", "dedup", "partial"))
    failed = sum(1 for r in results if r["status"] == "failed")
    click.echo(f"Total processed: {len(results)} | Succeeded: {succeeded} | Failed: {failed}")
    for r in results:
        del_tag = " [DELETED FROM FTP]" if r.get("deleted_source") else ""
        click.echo(f" - {r['file']} ({r['bytes']} bytes): {r['status']}{del_tag}")
        if r.get("error"):
            click.echo(f"     error: {r['error']}")
    click.echo("================================================")

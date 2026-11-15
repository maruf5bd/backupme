import click

from .base import cli
from ..cli.output import emit


@cli.command("process-files")
@click.argument("backend_name")
@click.option("--prefix", default="", help="Only process files under this prefix")
@click.option("--delete", is_flag=True,
              help="Delete each source file after its processed copy is stored")
@click.option("--target", help="Target backend to store processed blobs")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def process_files(ctx, backend_name, prefix, delete, target, as_json):
    """Serial helper: download one file at a time from a backend, run it
    through the real pipeline, and store the processed result back.

    Progress lands in files_ingest_progress.json; checkpoint/resume is on
    (reruns skip already-indexed files).
    """
    from ..core.files_ingest import ingest_files
    try:
        result = ingest_files(
            backend_name=backend_name,
            config=ctx.obj["config"],
            prefix=prefix,
            target_backend_name=target,
            delete=delete,
        )
    except ValueError as e:
        emit({"status": "error", "error": str(e)}, as_json=as_json,
             human=lambda d: click.echo(f"Error: {d['error']}"))
        raise SystemExit(1)
    if result.get("status") == "busy":
        emit(result, as_json=as_json,
             human=lambda d: click.echo("Busy: another process-files run on this backend is already active"))
        raise SystemExit(1)
    if result.get("status") == "error":
        emit(result, as_json=as_json,
             human=lambda d: click.echo(f"Error: {d['error']}"))
        raise SystemExit(1)
    emit(result, as_json=as_json, human=lambda d: click.echo(
        f"Processed {d['completed']} files ({d['skipped']} skipped, "
        f"{d['errors']} errors) in {d.get('elapsed_min', 0)} min"))


@cli.command()
@click.argument("backend_name")
@click.option("--prefix", default="", help="Only convert files under this prefix")
@click.option("--delete", is_flag=True, help="Delete original after conversion")
@click.option("--target", help="Target backend to store converted blobs")
@click.option("--resume", is_flag=True, help="Resume from last checkpoint")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def convert(ctx, backend_name, prefix, delete, target, resume, as_json):
    from ..core.converter import convert_backend
    try:
        result = convert_backend(
            backend_name=backend_name,
            prefix=prefix,
            target_backend_name=target,
            delete=delete,
            resume=resume,
        )
    except ValueError as e:
        # Unknown backend / bad arguments: clean error, not a traceback.
        emit({"status": "error", "error": str(e)}, as_json=as_json,
             human=lambda d: click.echo(f"Error: {d['error']}"))
        raise SystemExit(1)
    emit(result, as_json=as_json, human=lambda d: click.echo(
        f"Converted {d['completed']} files, {d['errors']} errors, {d['skipped']} skipped"))

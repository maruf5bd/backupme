import click

from .base import cli
from ..cli.output import emit


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

import click

from .base import cli


@cli.command()
@click.argument("backend_name")
@click.option("--prefix", default="", help="Only convert files under this prefix")
@click.option("--delete", is_flag=True, help="Delete original after conversion")
@click.option("--target", help="Target backend to store converted blobs")
@click.option("--resume", is_flag=True, help="Resume from last checkpoint")
@click.pass_context
def convert(ctx, backend_name, prefix, delete, target, resume):
    from ..core.converter import convert_backend
    result = convert_backend(
        backend_name=backend_name,
        prefix=prefix,
        target_backend_name=target,
        delete=delete,
        resume=resume,
    )
    click.echo(f"Converted {result['completed']} files, {result['errors']} errors, {result['skipped']} skipped")
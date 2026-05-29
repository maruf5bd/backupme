import os
import click

from .base import cli


@cli.command()
@click.argument("file_id", type=int)
@click.option("--output", "-o", default=".", help="Output directory")
@click.option("--version", "-v", type=int, help="Specific version")
@click.option("--backend", help="Preferred backend")
@click.pass_context
def download(ctx, file_id, output, version, backend):
    from ..core.downloader import download_file
    out_path = os.path.join(output, f"file_{file_id}")
    result = download_file(file_id, out_path, backend_name=backend, version=version)
    click.echo(f"Downloaded to {result['path']} ({result['size']} bytes, SHA256={result['sha256'][:16]}...)")
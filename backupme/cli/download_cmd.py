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
    try:
        result = download_file(file_id, out_path, backend_name=backend, version=version)
    except FileNotFoundError:
        click.echo(f"Error: file id {file_id} not found")
        raise SystemExit(1)
    except Exception as e:
        click.echo(f"Error: {e}")
        raise SystemExit(1)
    if not isinstance(result, dict) or result.get("status") != "ok":
        msg = result.get("message", "download failed") if isinstance(result, dict) else str(result)
        click.echo(f"Error: {msg}")
        raise SystemExit(1)
    click.echo(f"Downloaded to {result['path']} ({result['size']} bytes, SHA256={result['sha256'][:16]}...)")
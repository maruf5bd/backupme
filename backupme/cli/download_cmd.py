import os
import click

from .base import cli
from ..cli.output import emit


@cli.command()
@click.argument("file_id", type=int)
@click.option("--output", "-o", default=".", help="Output directory")
@click.option("--version", "-v", type=int, help="Specific version")
@click.option("--backend", help="Preferred backend")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def download(ctx, file_id, output, version, backend, as_json):
    from ..core.downloader import download_file
    out_path = os.path.join(output, f"file_{file_id}")
    try:
        result = download_file(file_id, out_path, backend_name=backend, version=version)
    except FileNotFoundError:
        _fail(as_json, file_id, f"file id {file_id} not found")
    except Exception as e:
        _fail(as_json, file_id, str(e))
    if not isinstance(result, dict) or result.get("status") != "ok":
        msg = result.get("message", "download failed") if isinstance(result, dict) else str(result)
        _fail(as_json, file_id, msg)
    payload = {
        "status": "ok",
        "file_id": file_id,
        "path": result["path"],
        "size": result["size"],
        "sha256": result["sha256"],
    }
    if as_json:
        emit(payload, as_json=True)
    else:
        click.echo(f"Downloaded to {result['path']} ({result['size']} bytes, SHA256={result['sha256'][:16]}...)")


def _fail(as_json: bool, file_id: int, message: str):
    """Clean error with the historical exit 1; --json emits one payload."""
    if as_json:
        emit({"status": "error", "file_id": file_id, "error": message}, as_json=True)
    else:
        click.echo(f"Error: {message}")
    raise SystemExit(1)

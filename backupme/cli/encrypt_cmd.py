import os
import click

from .base import cli


@cli.group()
def encrypt():
    """Encryption key management"""


@encrypt.command()
@click.pass_context
def init(ctx):
    from ..core.crypto import generate_key, save_key
    key = generate_key()
    key_path = os.path.expanduser("~/.backupme/key.enc")
    save_key(key, key_path)
    click.echo(f"Key saved to {key_path}")


@encrypt.command()
@click.pass_context
def status(ctx):
    key_path = os.path.expanduser("~/.backupme/key.enc")
    if os.path.exists(key_path):
        click.echo(f"Encryption key exists at {key_path}")
    else:
        click.echo("No encryption key found. Run 'encrypt init' to create one.")


@cli.command()
@click.option("--algo", default="zstd", help="Compression algorithm")
@click.option("--level", type=int, default=3, help="Compression level")
@click.option("--backend", "-b", help="Target backend")
@click.pass_context
def recompress(ctx, algo, level, backend):
    from ..core.compress import recompress_blobs
    cfg = ctx.obj["config"]
    enc_cfg = getattr(cfg, "encryption", None)
    key = None
    if enc_cfg and getattr(enc_cfg, "enabled", False):
        from ..core.crypto import load_key
        key = load_key(getattr(enc_cfg, "key_file", "~/.backupme/key.enc"))
    result = recompress_blobs(algo=algo, level=level, backend_name=backend, key=key)
    click.echo(f"Recompressed {result['recompressed']} of {result['total_scanned']} blobs")


@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False), required=True)
@click.option("--backend", "-b", help="Target backend name")
@click.option("--redundancy", "-n", type=int, default=1, help="Number of copies")
@click.pass_context
def upload_bundle(ctx, directory, backend, redundancy):
    """Batch-upload all small files (1KB-64KB) in a directory as a compressed tar bundle."""
    cfg = ctx.obj["config"]
    from ..backends import get_backend, get_all_backends

    if backend:
        b = get_backend(backend, cfg)
        targets = [b] if b else []
    else:
        targets = get_all_backends(cfg)

    if not targets:
        click.echo("No backends available")
        return

    files = []
    for fn in os.listdir(directory):
        fp = os.path.join(directory, fn)
        if os.path.isfile(fp):
            sz = os.path.getsize(fp)
            if 1024 < sz < 64 * 1024:
                files.append({
                    "local_path": fp,
                    "source_root": directory,
                    "relative_path": fn,
                })

    if not files:
        click.echo("No eligible small files (1KB-64KB) found")
        return

    from ..core import uploader
    results = uploader.upload_bundle(files, targets, cfg, redundancy)
    bundled = sum(1 for r in results if r.get("bundled"))
    dedup = sum(1 for r in results if r.get("dedup"))
    click.echo(f"Uploaded {len(results)} files ({bundled} bundled, {dedup} dedup)")
import os
import click

from .base import cli


@cli.command()
@click.argument("sources", nargs=-1, required=True)
@click.option("--backend", "-b", help="Target backend name")
@click.option("--redundancy", "-n", type=int, default=1, help="Number of copies")
@click.option("--recursive", "-r", is_flag=True, help="Recurse directories")
@click.pass_context
def backup(ctx, sources, backend, redundancy, recursive):
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

    from ..core import uploader

    small_batch = []
    _batch_max_bytes = 14 * 1024 * 1024

    def _flush_batch():
        nonlocal small_batch
        if small_batch:
            results = uploader.upload_bundle(small_batch, targets, cfg, redundancy)
            for r in results:
                click.echo(f"  {r.get('sha256', r.get('file_id', '?'))[:12]} dedup={r.get('dedup', False)} bundled={r.get('bundled', False)}")
            small_batch.clear()

    for path in sources:
        if os.path.isfile(path):
            sz = os.path.getsize(path)
            rel = os.path.basename(path)
            if 1024 < sz < 64 * 1024:
                batch_size = sum(os.path.getsize(f["local_path"]) for f in small_batch) + sz
                small_batch.append({
                    "local_path": path,
                    "source_root": os.path.dirname(path),
                    "relative_path": rel,
                })
                if batch_size >= _batch_max_bytes:
                    _flush_batch()
            else:
                _flush_batch()
                result = uploader.upload_file(
                    source_root=os.path.dirname(path),
                    relative_path=rel,
                    local_path=path,
                    backends=targets,
                    config=cfg,
                    redundancy=redundancy,
                )
                click.echo(f"{rel}: file_id={result.get('file_id', '?')} dedup={result.get('dedup', False)} inline={result.get('inline', False)}")

        elif os.path.isdir(path) and recursive:
            for root, _dirs, files in os.walk(path):
                for fn in files:
                    fp = os.path.join(root, fn)
                    sz = os.path.getsize(fp)
                    rel = os.path.relpath(fp, path)

                    if 1024 < sz < 64 * 1024:
                        batch_size = sum(os.path.getsize(f["local_path"]) for f in small_batch) + sz
                        small_batch.append({
                            "local_path": fp,
                            "source_root": path,
                            "relative_path": rel,
                        })
                        if batch_size >= _batch_max_bytes:
                            _flush_batch()
                    else:
                        _flush_batch()
                        result = uploader.upload_file(
                            source_root=path,
                            relative_path=rel,
                            local_path=fp,
                            backends=targets,
                            config=cfg,
                            redundancy=redundancy,
                        )
                        click.echo(f"{rel}: file_id={result.get('file_id', '?')} dedup={result.get('dedup', False)} inline={result.get('inline', False)}")

    _flush_batch()
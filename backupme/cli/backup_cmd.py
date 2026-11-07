import os
import click

from .base import cli


def _safe_echo(msg: str):
    try:
        click.echo(msg)
    except UnicodeEncodeError:
        try:
            import sys
            enc = sys.stdout.encoding or 'utf-8'
            click.echo(msg.encode(enc, errors='replace').decode(enc))
        except Exception:
            try:
                click.echo(msg.encode('ascii', errors='replace').decode('ascii'))
            except Exception:
                pass


@cli.command()
@click.argument("sources", nargs=-1, required=True)
@click.option("--backend", "-b", help="Target backend name")
@click.option("--redundancy", "-n", type=int, default=1, help="Number of copies")
@click.option("--recursive", "-r", is_flag=True, help="Recurse directories")
@click.option("--no-skip", is_flag=True, help="Disable system/temp file skipping")
@click.option("--force", is_flag=True, help="Re-offer every file, skipping the unchanged check")
@click.option("--move", "-m", is_flag=True, help="Move files to backend (deletes source file after successful upload)")
@click.pass_context
def backup(ctx, sources, backend, redundancy, recursive, no_skip, force, move):
    cfg = ctx.obj["config"]
    from ..backends import get_backend, get_all_backends

    if backend:
        b = get_backend(backend, cfg)
        targets = [b] if b else []
    else:
        targets = get_all_backends(cfg)

    if not targets:
        _safe_echo("No backends available")
        return

    from ..core import uploader
    from ..core.skip import should_skip, should_skip_dir

    files_to_backup = []

    for path in sources:
        if os.path.isfile(path):
            if not no_skip:
                skip, reason = should_skip(path)
                if skip:
                    _safe_echo(f"  Skipped {os.path.basename(path)} ({reason})")
                    continue
            files_to_backup.append({
                "local_path": path,
                "source_root": os.path.dirname(path),
                "relative_path": os.path.basename(path),
            })

        elif os.path.isdir(path) and recursive:
            from ..scheduler import collect_changes
            candidates, skipped = collect_changes(path, [], force=force, no_skip=no_skip)
            if skipped:
                _safe_echo(f"{path}: {skipped} unchanged (skipped)")
            for fp in candidates:
                files_to_backup.append({
                    "local_path": str(fp),
                    "source_root": path,
                    "relative_path": os.path.relpath(str(fp), path),
                })

        elif os.path.isdir(path):
            _safe_echo(f"Error: {path} is a directory — use --recursive")
            raise SystemExit(1)
        else:
            # Silent ignore made `backup missing.txt` exit 0 with empty
            # output (qa P1) — a nonexistent path is an error.
            _safe_echo(f"Error: {path} does not exist")
            raise SystemExit(1)

    if files_to_backup:
        try:
            results = uploader.upload_bundle(files_to_backup, targets, cfg, redundancy)
            errors = partials = 0
            for r in results:
                rel_path = r.get("local_path", "")
                if rel_path:
                    for src in sources:
                        if rel_path.startswith(src):
                            rel_path = os.path.relpath(rel_path, src)
                            break
                    if not rel_path:
                        rel_path = os.path.basename(r.get("local_path", ""))
                else:
                    rel_path = r.get("sha256", r.get("file_id", "?"))

                if r.get("status") == "error" or r.get("error"):
                    errors += 1
                    _safe_echo(f"{rel_path}: ERROR {r.get('error', 'upload failed')}")
                    continue
                if r.get("status") == "partial":
                    partials += 1
                    _safe_echo(f"{rel_path}: file_id={r.get('file_id', '?')} status=partial "
                               f"failed_backends={','.join(r.get('failed_backends', []))}")
                    continue

                _safe_echo(f"{rel_path}: file_id={r.get('file_id', '?')} dedup={r.get('dedup', False)} bundled={r.get('bundled', False)}")

                if move and "file_id" in r and r.get("local_path"):
                    try:
                        os.remove(r["local_path"])
                    except OSError:
                        pass
            if errors or partials:
                _safe_echo(f"Summary: {errors} failed, {partials} partial "
                           f"({len(results) - errors - partials} ok)")
        except Exception as e:
            _safe_echo(f"  Error running backup: {e}")
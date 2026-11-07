import os
import click

from .base import cli
from ..cli.output import emit


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
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backup(ctx, sources, backend, redundancy, recursive, no_skip, force, move, as_json):
    cfg = ctx.obj["config"]
    from ..backends import get_backend, get_all_backends

    # --json payload: human output below stays the default and is unchanged.
    payload = {
        "sources": [],
        "files": [],
        "counts": {"ok": 0, "skipped": 0, "partial": 0, "error": 0},
        "bytes": 0,
    }

    def note_source(path, status, **extra):
        entry = {"source": path, "status": status}
        entry.update(extra)
        payload["sources"].append(entry)

    def fail(message: str, path: str):
        """Source-level error: same exit code in both modes; --json additionally
        emits one parseable payload."""
        if as_json:
            payload["error"] = message
            note_source(path, "error", error=message)
            emit(payload, as_json=True)
        else:
            _safe_echo(message)
        raise SystemExit(1)

    if backend:
        b = get_backend(backend, cfg)
        targets = [b] if b else []
    else:
        targets = get_all_backends(cfg)

    if not targets:
        if as_json:
            payload["error"] = "No backends available"
            emit(payload, as_json=True)
        else:
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
                    payload["counts"]["skipped"] += 1
                    note_source(path, "skipped", reason=reason)
                    if not as_json:
                        _safe_echo(f"  Skipped {os.path.basename(path)} ({reason})")
                    continue
            note_source(path, "ok")
            files_to_backup.append({
                "local_path": path,
                "source_root": os.path.dirname(path),
                "relative_path": os.path.basename(path),
            })

        elif os.path.isdir(path) and recursive:
            from ..scheduler import collect_changes
            candidates, skipped = collect_changes(path, [], force=force, no_skip=no_skip)
            payload["counts"]["skipped"] += skipped
            note_source(path, "ok", unchanged_skipped=skipped, files=len(candidates))
            if not as_json and skipped:
                _safe_echo(f"{path}: {skipped} unchanged (skipped)")
            for fp in candidates:
                files_to_backup.append({
                    "local_path": str(fp),
                    "source_root": path,
                    "relative_path": os.path.relpath(str(fp), path),
                })

        elif os.path.isdir(path):
            fail(f"Error: {path} is a directory — use --recursive", path)
        else:
            # Silent ignore made `backup missing.txt` exit 0 with empty
            # output (qa P1) — a nonexistent path is an error.
            fail(f"Error: {path} does not exist", path)

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
                    if as_json:
                        payload["files"].append({
                            "relative_path": rel_path,
                            "sha256": r.get("sha256"),
                            "status": "error",
                            "error": r.get("error", "upload failed"),
                        })
                    else:
                        _safe_echo(f"{rel_path}: ERROR {r.get('error', 'upload failed')}")
                    continue
                if r.get("status") == "partial":
                    partials += 1
                    if as_json:
                        payload["files"].append({
                            "relative_path": rel_path,
                            "file_id": r.get("file_id"),
                            "sha256": r.get("sha256"),
                            "status": "partial",
                            "failed_backends": r.get("failed_backends", []),
                        })
                    else:
                        _safe_echo(f"{rel_path}: file_id={r.get('file_id', '?')} status=partial "
                                   f"failed_backends={','.join(r.get('failed_backends', []))}")
                    continue

                payload["counts"]["ok"] += 1
                try:
                    payload["bytes"] += os.path.getsize(r["local_path"])
                except OSError:
                    pass
                if as_json:
                    payload["files"].append({
                        "relative_path": rel_path,
                        "file_id": r.get("file_id"),
                        "sha256": r.get("sha256"),
                        "status": "ok",
                        "dedup": r.get("dedup", False),
                        "bundled": r.get("bundled", False),
                    })
                else:
                    _safe_echo(f"{rel_path}: file_id={r.get('file_id', '?')} dedup={r.get('dedup', False)} bundled={r.get('bundled', False)}")

                if move and "file_id" in r and r.get("local_path"):
                    try:
                        os.remove(r["local_path"])
                    except OSError:
                        pass
            if as_json:
                payload["counts"]["error"] = errors
                payload["counts"]["partial"] = partials
            elif errors or partials:
                _safe_echo(f"Summary: {errors} failed, {partials} partial "
                           f"({len(results) - errors - partials} ok)")
        except Exception as e:
            # Exit code unchanged (historical behavior): the failure is
            # reported, not raised, in both modes.
            if as_json:
                payload["error"] = f"Error running backup: {e}"
            else:
                _safe_echo(f"  Error running backup: {e}")
    if as_json:
        emit(payload, as_json=True)

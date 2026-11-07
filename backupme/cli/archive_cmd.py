import click

from .base import cli
from ..cli.output import emit


@cli.group()
def archive():
    """Extract-first archive manifests (list, inspect, restore)."""


def _resolve_or_fail(fingerprint: str):
    """Resolve a full fingerprint or a unique hex prefix to its archive row.

    Returns (row, error_message); exactly one is non-None."""
    from ..core import archives as archives_mod

    row, match_type, candidates = archives_mod.resolve_fingerprint(fingerprint)
    if row is not None:
        return row, None
    if match_type == "prefix-ambiguous":
        lines = [
            f"Fingerprint prefix '{fingerprint}' matches {len(candidates)} "
            "archives; not unique. Candidates:"
        ]
        for candidate in candidates:
            lines.append(f"  {candidate['fingerprint']}  "
                         f"{candidate['original_name'] or ''}")
        return None, "\n".join(lines)
    return None, f"No archive with fingerprint '{fingerprint}'"


@archive.command("list")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def archive_list(ctx, as_json):
    """List ingested archives (virtual manifests)."""
    from ..core.database import execute

    rows = execute(
        "SELECT id, fingerprint, format, original_name, original_size, "
        "member_count, total_uncompressed, created_at FROM archives "
        "ORDER BY created_at DESC, id DESC",
    ).fetchall()
    items = [dict(r) for r in rows]

    def human(d):
        for r in d["archives"]:
            click.echo(
                f"{r['id']:>6}  {r['fingerprint'][:16]}  {r['format'] or '?':<8} "
                f"{(r['original_name'] or ''):<40}  {r['member_count']} members  "
                f"{r['total_uncompressed']} B  {r['created_at']}")

    emit({"archives": items, "count": len(items)}, as_json=as_json, human=human)


@archive.command("show")
@click.argument("fingerprint")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def archive_show(ctx, fingerprint, as_json):
    """Show an archive's full member list (full fingerprint or unique prefix)."""
    from ..core import archives as archives_mod

    row, error = _resolve_or_fail(fingerprint)
    if row is None:
        if as_json:
            emit({"status": "error", "error": error}, as_json=True)
        else:
            click.echo(error)
        raise SystemExit(1)
    members = archives_mod.get_archive_members(row["id"])
    payload = {"archive": dict(row), "members": members, "count": len(members)}

    def human(d):
        a = d["archive"]
        click.echo(f"fingerprint {a['fingerprint']}")
        click.echo(f"format      {a['format']}  original {a['original_name']} "
                   f"({a['original_size']} B)")
        click.echo(f"members     {a['member_count']}  total_uncompressed "
                   f"{a['total_uncompressed']} B  created {a['created_at']}")
        for m in d["members"]:
            nested = (f"  -> nested {m['nested_archive'][:16]}"
                      if m["nested_archive"] else "")
            click.echo(f"  {m['relative_path']:<60} {m['member_size']:>12}  "
                       f"{m['member_sha256'][:16]}{nested}")

    emit(payload, as_json=as_json, human=human)


@archive.command("restore")
@click.argument("fingerprint")
@click.option("--output", "-o", required=True, help="Output directory")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def archive_restore(ctx, fingerprint, output, as_json):
    """Restore every member of an archive under --output (structure preserved).

    Accepts the full fingerprint or a unique prefix. A nested archive member
    is restored as a directory at its path containing the nested archive's
    own members.
    """
    from ..core import archives as archives_mod

    result = archives_mod.restore_archive(fingerprint, output)

    def human(d):
        if d.get("status") == "error":
            click.echo(f"Error: {d.get('error')}")
            for c in d.get("candidates", []):
                click.echo(f"  {c['fingerprint']}  {c['original_name'] or ''}")
            return
        suffix = f" ({d['errors']} errors)" if d.get("errors") else ""
        click.echo(f"Restored {d['members']} member(s) of "
                   f"{d['fingerprint'][:16]} to {d['out']}{suffix}")

    emit(result, as_json=as_json, human=human)
    if result.get("status") != "ok":
        raise SystemExit(1)

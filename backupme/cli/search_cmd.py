import json

import click

from .base import cli
from ..cli.output import emit


@cli.command()
@click.argument("query")
@click.option("--content", is_flag=True, help="Search within file content")
@click.option("--source", help="Filter by source root")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def search(ctx, query, content, source, as_json):
    from ..core import index as index_mod
    if content:
        from ..core.database import execute
        rows = execute(
            "SELECT * FROM files WHERE content_text LIKE ? AND deleted_at IS NULL LIMIT 50",
            (f"%{query}%",),
        ).fetchall()
        results = [dict(r) for r in rows]
    else:
        results = index_mod.search_files(query, source_root=source)

    def human(d):
        for r in d["results"]:
            click.echo(f"{r['id']:>6}  {r['relative_path']:<60}  {r['sha256'][:16]}")

    emit({"results": results, "count": len(results)}, as_json=as_json, human=human)


@cli.command()
@click.argument("query")
@click.pass_context
def select_backend(ctx, query):
    cfg = ctx.obj["config"]
    from ..core import selector
    results = selector.fuzzy_find_backend(query, cfg)
    for btype, name, score in results:
        click.echo(f"{name:<20} ({btype:<8})  score={score:.2f}")


_HEX_DIGITS = set("0123456789abcdefABCDEF")

_FILE_COLUMNS = ("id, source_root, relative_path, sha256, size, version_seq, deleted_at")


def _is_hex(text: str) -> bool:
    return bool(text) and all(c in _HEX_DIGITS for c in text)


def _is_bundle_member(blob_id: str, storage_path: str) -> bool:
    """Bundle members live as byte ranges inside their parent bundle tar:
    their blob ids look like ``bundle/a/b/tar/mem`` and/or their storage
    path carries an ``@offset:len`` suffix (AGENTS.md bundle-member rule)."""
    return (blob_id or "").startswith("bundle/") or "@" in (storage_path or "")


def _file_result(row) -> dict:
    """One ``files`` row plus its chunk->blob mapping with every backend copy."""
    from ..core.database import execute
    chunk_rows = execute(
        "SELECT fc.seq, fc.blob_id, fc.pt_size, bs.backend_name, bs.storage_path "
        "FROM file_chunks fc LEFT JOIN blob_storage bs ON fc.blob_id = bs.blob_id "
        "WHERE fc.file_id = ? ORDER BY fc.seq",
        (row["id"],),
    ).fetchall()
    chunks: dict = {}
    for c in chunk_rows:
        entry = chunks.setdefault(c["blob_id"], {
            "seq": c["seq"],
            "blob_id": c["blob_id"],
            "size": c["pt_size"],
            "bundle_member": _is_bundle_member(c["blob_id"], c["storage_path"]),
            "copies": [],
        })
        if c["backend_name"]:
            if "@" in (c["storage_path"] or ""):
                entry["bundle_member"] = True
            entry["copies"].append({
                "backend_name": c["backend_name"],
                "storage_path": c["storage_path"],
            })
    return {
        "id": row["id"],
        "source_root": row["source_root"],
        "relative_path": row["relative_path"],
        "sha256": row["sha256"],
        "size": row["size"],
        "version_seq": row["version_seq"],
        "deleted": row["deleted_at"] is not None,
        "deleted_at": row["deleted_at"],
        "chunks": list(chunks.values()),
    }


def _file_row_light(row) -> dict:
    return {
        "id": row["id"],
        "source_root": row["source_root"],
        "relative_path": row["relative_path"],
        "sha256": row["sha256"],
        "size": row["size"],
        "version_seq": row["version_seq"],
        "deleted": row["deleted_at"] is not None,
    }


def _lookup_file_rows(query: str):
    """Resolve a FILE sha256: exact match first, then a unique hex prefix."""
    from ..core.database import execute
    rows = execute(
        f"SELECT {_FILE_COLUMNS} FROM files WHERE sha256 = ? ORDER BY id",
        (query,),
    ).fetchall()
    if rows:
        return rows, "file"
    if _is_hex(query) and len(query) < 64:
        rows = execute(
            f"SELECT {_FILE_COLUMNS} FROM files WHERE sha256 LIKE ? ORDER BY id",
            (query.lower() + "%",),
        ).fetchall()
        if rows:
            return rows, "file-prefix"
    return [], None


@cli.command("hash-lookup")
@click.argument("sha256")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def hash_lookup(ctx, sha256, as_json):
    """Resolve a blob id, a FILE sha256 (exact, or unique hex prefix)."""
    from ..core import dedup
    from ..core.database import execute

    query = (sha256 or "").strip()
    if _is_hex(query):
        query = query.lower()  # stored hashes are lowercase hex
    payload = {
        "query": query, "resolved": False, "match_type": None,
        "blob": None, "files": [], "count": 0,
    }

    blob_row = dedup.get_blob(query)
    if blob_row is not None:
        storage = [dict(r) for r in execute(
            "SELECT backend_name, storage_path, stored_size FROM blob_storage "
            "WHERE blob_id = ?",
            (query,),
        ).fetchall()]
        payload["resolved"] = True
        payload["match_type"] = "blob"
        payload["blob"] = {**dict(blob_row), "storage": storage}
    else:
        file_rows, match_type = _lookup_file_rows(query)
        if match_type == "file-prefix" and len(file_rows) > 1:
            # Ambiguous prefix: refuse to guess, list the candidates.
            payload["match_type"] = "prefix-ambiguous"
            payload["files"] = [_file_row_light(r) for r in file_rows]
            payload["count"] = len(file_rows)
        elif file_rows:
            payload["resolved"] = True
            payload["match_type"] = match_type
            payload["files"] = [_file_result(r) for r in file_rows]
            payload["count"] = len(payload["files"])

    def human(d):
        if d["match_type"] == "prefix-ambiguous":
            click.echo(f"Hash prefix matches {d['count']} files; not unique. Candidates:")
            for f in d["files"]:
                click.echo(f"  {f['sha256']}  id={f['id']} {f['relative_path']}")
            return
        if not d["resolved"]:
            click.echo("Hash not found")
            return
        if d["blob"] is not None:
            # Historical contract: a blob match printed the blob row as JSON
            # (qa2 parses it); keep that, now with the storage locations.
            click.echo(json.dumps(d["blob"], indent=2, default=str))
        for f in d["files"]:
            status = "deleted" if f["deleted"] else "live"
            click.echo(f"id={f['id']} v{f['version_seq']} [{status}] "
                       f"{f['relative_path']} ({f['size']} bytes) "
                       f"sha256={f['sha256'][:16]}...")
            for ch in f["chunks"]:
                backends = ", ".join(c["backend_name"] for c in ch["copies"]) or "(no storage rows)"
                tag = " [bundle member]" if ch["bundle_member"] else ""
                click.echo(f"  chunk {ch['seq']}: blob {ch['blob_id'][:32]}...{tag} copies: {backends}")

    emit(payload, as_json=as_json, human=human)
    if not payload["resolved"]:
        raise SystemExit(1)

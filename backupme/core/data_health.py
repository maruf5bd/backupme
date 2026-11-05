"""Data health: live issue buckets computed from the SQLite index.

Every query here is read-only over the database (core.database.execute) and
cheap enough to run on request. The summary and drill-down helpers never
touch the filesystem; physical reachability checks live in the web layer
(see web.routes POST /api/health/scan) which consumes ``local_scan_plan``
and stats files at each local backend root.

Issue buckets
-------------
- ``no_chunks``      live files with zero ``file_chunks`` rows (content was
                     never stored).
- ``missing_blob``   live files with at least one chunk whose blob has no
                     ``blob_storage`` row at all (any backend).
- same-name groups   live files sharing a basename across source roots.
- purge candidates   live files that can never restore again: zero chunks,
                     or every chunk's blob missing from ``blob_storage``.
                     Files with only SOME blobs missing are reported under
                     ``missing_blob`` (they cannot restore fully but are not
                     total-loss rows), not as purge candidates.
"""

from .database import execute

REASON_NO_CHUNKS = "no_chunks"
REASON_MISSING_BLOB = "missing_blob"
REASON_PURGE_CANDIDATES = "purge_candidates"

KNOWN_REASONS = (REASON_NO_CHUNKS, REASON_MISSING_BLOB, REASON_PURGE_CANDIDATES)

_FILE_COLUMNS = "f.id, f.source_root, f.relative_path, f.size"

# Live files that can never restore again: no chunk rows at all, or none of
# the file's chunk blobs has a single blob_storage row.
_PURGE_CONDITION = (
    "NOT EXISTS (SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id) "
    "OR NOT EXISTS ("
    "  SELECT 1 FROM file_chunks fc2"
    "  JOIN blob_storage bs ON bs.blob_id = fc2.blob_id"
    "  WHERE fc2.file_id = f.id)"
)

# Basename of a path, computed in SQL so grouping stays a single pass over
# the live set. Backslashes are normalized to '/' first; the segment after
# the last '/' is substr(p, length(rtrim(p, replace(p, '/', ''))) + 1) —
# rtrim with a set of chars strips the directory part, leaving "dirname/".
_BASENAME_SQL = (
    "substr(p, length(rtrim(p, replace(p, '/', ''))) + 1)"
)


def _scalar(sql: str, params: tuple = ()) -> int:
    row = execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def summary(config=None) -> dict:
    """One cheap-SQL snapshot of every known data-health problem.

    ``config`` is accepted for interface symmetry with the web layer (all
    data comes from the DB; no backend connections are made).
    """
    live_files = _scalar("SELECT COUNT(*) FROM files WHERE deleted_at IS NULL")
    no_chunks_files = _scalar(
        "SELECT COUNT(*) FROM files f WHERE f.deleted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id)"
    )
    missing_blob_files = _scalar(
        "SELECT COUNT(DISTINCT f.id) FROM files f "
        "JOIN file_chunks fc ON fc.file_id = f.id "
        "WHERE f.deleted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM blob_storage bs WHERE bs.blob_id = fc.blob_id)"
    )
    missing_blobs = _scalar(
        "SELECT COUNT(DISTINCT fc.blob_id) FROM file_chunks fc "
        "JOIN files f ON f.id = fc.file_id "
        "WHERE f.deleted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM blob_storage bs WHERE bs.blob_id = fc.blob_id)"
    )
    backend_rows = execute(
        "SELECT backend_name, COUNT(DISTINCT blob_id) AS blob_count, "
        "SUM(stored_size) AS stored_bytes "
        "FROM blob_storage GROUP BY backend_name ORDER BY backend_name"
    ).fetchall()
    groups_total, groups_files = _same_name_counts()
    return {
        "live_files": live_files,
        "no_chunks_files": no_chunks_files,
        "missing_blob_files": missing_blob_files,
        "missing_blobs": missing_blobs,
        "backend_blobs": [
            {
                "backend": r["backend_name"],
                "blobs": int(r["blob_count"]),
                "stored_bytes": int(r["stored_bytes"] or 0),
            }
            for r in backend_rows
        ],
        "same_name_groups": groups_total,
        "same_name_files": groups_files,
        "purge_candidates": purge_candidate_count(),
    }


def issues(reason: str, limit: int = 50, offset: int = 0) -> dict:
    """Paginated drill-down for one reason bucket.

    Raises ValueError for an unknown reason (route maps it to 400).
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    if reason == REASON_NO_CHUNKS:
        total = _scalar(
            "SELECT COUNT(*) FROM files f WHERE f.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id)"
        )
        rows = execute(
            f"SELECT {_FILE_COLUMNS} FROM files f "
            "WHERE f.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id) "
            "ORDER BY f.id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        why = "no chunk rows stored for this file"
        items = [_issue(r, why) for r in rows]
    elif reason == REASON_MISSING_BLOB:
        total = _scalar(
            "SELECT COUNT(DISTINCT f.id) FROM files f "
            "JOIN file_chunks fc ON fc.file_id = f.id "
            "WHERE f.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM blob_storage bs WHERE bs.blob_id = fc.blob_id)"
        )
        rows = execute(
            f"SELECT {_FILE_COLUMNS}, "
            "(SELECT MIN(fc2.blob_id) FROM file_chunks fc2 "
            " WHERE fc2.file_id = f.id "
            " AND NOT EXISTS (SELECT 1 FROM blob_storage bs2 WHERE bs2.blob_id = fc2.blob_id)"
            ") AS broken_blob "
            "FROM files f "
            "WHERE f.deleted_at IS NULL AND EXISTS ("
            "  SELECT 1 FROM file_chunks fc3"
            "  WHERE fc3.file_id = f.id"
            "  AND NOT EXISTS (SELECT 1 FROM blob_storage bs3 WHERE bs3.blob_id = fc3.blob_id)) "
            "ORDER BY f.id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        items = []
        for r in rows:
            items.append(_issue(
                r, f"chunk blob {r['broken_blob']} has no storage rows"))
    elif reason == REASON_PURGE_CANDIDATES:
        total = purge_candidate_count()
        rows = execute(
            f"SELECT {_FILE_COLUMNS}, "
            "CASE WHEN NOT EXISTS ("
            "    SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id"
            "  ) THEN 'no_chunks' ELSE 'missing_blob' END AS kind "
            "FROM files f "
            f"WHERE f.deleted_at IS NULL AND ({_PURGE_CONDITION}) "
            "ORDER BY f.id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        items = []
        for r in rows:
            why = ("no chunk rows stored for this file"
                   if r["kind"] == "no_chunks"
                   else "every chunk blob has no storage rows")
            items.append(_issue(r, why))
    else:
        raise ValueError(f"unknown reason: {reason!r}")
    return {"reason": reason, "total": total, "limit": limit,
            "offset": offset, "items": items}


def _issue(row, why: str) -> dict:
    return {
        "file_id": int(row["id"]),
        "source_root": row["source_root"],
        "relative_path": row["relative_path"],
        "size": int(row["size"]),
        "why": why,
    }


def _same_name_counts() -> tuple[int, int]:
    """(group count, total files) for live files sharing a basename."""
    row = execute(
        "WITH live AS ("
        "  SELECT id, replace(relative_path, '\\', '/') AS p"
        "  FROM files WHERE deleted_at IS NULL"
        "), names AS ("
        f"  SELECT id, {_BASENAME_SQL} AS base FROM live"
        ") "
        "SELECT COUNT(*) AS groups, COALESCE(SUM(c), 0) AS files FROM ("
        "  SELECT base, COUNT(*) AS c FROM names GROUP BY base HAVING COUNT(*) > 1"
        ")"
    ).fetchone()
    return (int(row["groups"]), int(row["files"])) if row else (0, 0)


def same_name_groups(limit: int = 50, offset: int = 0) -> dict:
    """Paginated same-basename groups with their member files."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    groups_total, files_total = _same_name_counts()
    rows = execute(
        "WITH live AS ("
        "  SELECT id, replace(relative_path, '\\', '/') AS p"
        "  FROM files WHERE deleted_at IS NULL"
        "), names AS ("
        f"  SELECT id, {_BASENAME_SQL} AS base FROM live"
        ") "
        "SELECT base, COUNT(*) AS c FROM names GROUP BY base "
        "HAVING COUNT(*) > 1 ORDER BY c DESC, base LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    bases = [r["base"] for r in rows]
    groups = []
    if bases:
        placeholders = ", ".join("?" for _ in bases)
        members = execute(
            "WITH live AS ("
            "  SELECT id, source_root, relative_path, size,"
            "  replace(relative_path, '\\', '/') AS p"
            "  FROM files WHERE deleted_at IS NULL"
            "), names AS ("
            f"  SELECT id, source_root, relative_path, size, {_BASENAME_SQL} AS base FROM live"
            ") "
            f"SELECT id, source_root, relative_path, size, base FROM names "
            f"WHERE base IN ({placeholders}) "
            "ORDER BY base, source_root, relative_path",
            tuple(bases),
        ).fetchall()
        by_base: dict[str, list] = {}
        for m in members:
            by_base.setdefault(m["base"], []).append({
                "file_id": int(m["id"]),
                "source_root": m["source_root"],
                "relative_path": m["relative_path"],
                "size": int(m["size"]),
            })
        for r in rows:
            groups.append({
                "base_name": r["base"],
                "file_count": int(r["c"]),
                "members": by_base.get(r["base"], []),
            })
    return {"total_groups": groups_total, "total_files": files_total,
            "limit": limit, "offset": offset, "groups": groups}


def purge_candidate_count() -> int:
    """Live files that can never restore: zero chunks or all blobs missing."""
    return _scalar(
        f"SELECT COUNT(*) FROM files f "
        f"WHERE f.deleted_at IS NULL AND ({_PURGE_CONDITION})"
    )


def purge_candidates(limit: int = 50, offset: int = 0) -> dict:
    """Paginated purge candidates, re-computed at call time."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    total = purge_candidate_count()
    rows = execute(
        f"SELECT {_FILE_COLUMNS}, "
        "CASE WHEN NOT EXISTS ("
        "    SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id"
        "  ) THEN 'no_chunks' ELSE 'missing_blob' END AS kind "
        "FROM files f "
        f"WHERE f.deleted_at IS NULL AND ({_PURGE_CONDITION}) "
        "ORDER BY f.id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    items = []
    for r in rows:
        why = ("no chunk rows stored for this file"
               if r["kind"] == "no_chunks"
               else "every chunk blob has no storage rows")
        items.append(_issue(r, why))
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def filter_purge_candidate_ids(file_ids: list[int]) -> set[int]:
    """Which of the given ids are purge candidates right now.

    Used by the purge route to re-verify every id at purge time.
    """
    ids = [int(i) for i in file_ids]
    if not ids:
        return set()
    placeholders = ", ".join("?" for _ in ids)
    rows = execute(
        f"SELECT f.id FROM files f "
        f"WHERE f.deleted_at IS NULL AND f.id IN ({placeholders}) "
        f"AND ({_PURGE_CONDITION})",
        tuple(ids),
    ).fetchall()
    return {int(r["id"]) for r in rows}


def parse_member_path(storage_path: str) -> tuple[str, int, int] | None:
    """Parse a bundle-member storage path ``<parent>@<offset>:<length>``.

    Returns (parent_path, offset, length), or None when the path is a plain
    object (no byte-range suffix).
    """
    if not storage_path or "@" not in storage_path:
        return None
    parent, _, range_part = storage_path.rpartition("@")
    offset_s, sep, length_s = range_part.rpartition(":")
    if not sep or not parent:
        return None
    try:
        offset = int(offset_s)
        length = int(length_s)
    except ValueError:
        return None
    if offset < 0 or length < 0:
        return None
    return parent, offset, length


def local_scan_plan(backend_names: list[str]) -> dict:
    """Group blob_storage rows per local backend for the physical scan.

    Pure DB reads. Returns ``{backend_name: {"plain": [...], "members": [...]}}``
    where ``plain`` holds one entry per distinct non-member storage_path
    (object blobs and bundle parents) and ``members`` holds bundle-member
    byte ranges that must resolve through their parent bundle file.

    Plain entries carry ``pt_size`` (plaintext size from ``blobs``) next to
    ``stored_size`` (on-disk/compressed size): member byte ranges are offsets
    into the parent bundle's PLAINTEXT stream, so reachability must be judged
    against ``pt_size``, never the compressed on-disk size.
    """
    plan: dict[str, dict] = {}
    for name in backend_names:
        rows = execute(
            "SELECT bs.blob_id, bs.storage_path, bs.stored_size, b.pt_size "
            "FROM blob_storage bs LEFT JOIN blobs b ON b.blob_id = bs.blob_id "
            "WHERE bs.backend_name = ?",
            (name,),
        ).fetchall()
        plain: dict[str, dict] = {}
        members: list[dict] = []
        for r in rows:
            parsed = parse_member_path(r["storage_path"])
            if parsed is None:
                entry = plain.setdefault(r["storage_path"], {
                    "path": r["storage_path"],
                    "stored_size": int(r["stored_size"] or 0),
                    "pt_size": int(r["pt_size"] or 0),
                    "blob_id": r["blob_id"],
                    # Bundle parents carry a bundle/ blob_id (uploader style);
                    # the storage_path prefix guards against id variants.
                    "is_bundle_parent": r["blob_id"].startswith("bundle/")
                                        or r["storage_path"].startswith("bundle/"),
                })
                # Same path advertised by several blob rows: keep the largest
                # expected size so a truncated file still gets flagged.
                if int(r["stored_size"] or 0) > entry["stored_size"]:
                    entry["stored_size"] = int(r["stored_size"] or 0)
            else:
                parent, offset, length = parsed
                members.append({
                    "blob_id": r["blob_id"],
                    "storage_path": r["storage_path"],
                    "parent": parent,
                    "offset": offset,
                    "length": length,
                })
        plan[name] = {
            "plain": sorted(plain.values(), key=lambda e: e["path"]),
            "members": members,
        }
    return plan

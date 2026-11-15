"""Walk-vs-DB diff for a backend (backlog B13).

Answers "what does the index expect on this backend vs what is really there":
missing files, unreferenced files, and size mismatches (the cheap corruption
signal before any hashing). Bundle MEMBER rows (`path@offset:len`) are excluded
from the path diff — their content lives inside the parent bundle, which IS
compared as a plain path.
"""
from backupme.core.database import execute
from backupme.log import get_logger

log = get_logger("core.backend_scan")


def rescan_backend(backend) -> dict:
    """Diff one connected backend's real contents against blob_storage.

    Returns a dict with counts + sorted lists (missing_on_box, extra_on_box,
    size_mismatch) suitable for both CLI printing and JSON output.
    """
    walked = backend.walk("")
    on_box = {}
    for item in walked:
        try:
            on_box[str(item.get("path", ""))] = int(item.get("size", 0) or 0)
        except (TypeError, ValueError):
            log.warning("backend %s: walk row without usable size: %r", backend.name, item)

    rows = execute(
        "SELECT storage_path, stored_size FROM blob_storage "
        "WHERE backend_name = ? AND storage_path NOT LIKE '%@%'",
        (backend.name,),
    ).fetchall()
    in_db = {r["storage_path"]: r["stored_size"] for r in rows}

    missing_on_box = sorted(p for p in in_db if p not in on_box)
    extra_on_box = sorted(p for p in on_box if p not in in_db)
    size_mismatch = [
        {"path": p, "db_size": in_db[p], "box_size": on_box[p]}
        for p in sorted(set(in_db) & set(on_box))
        if in_db[p] is not None and on_box[p] != in_db[p]
    ]

    result = {
        "backend": backend.name,
        "walk_files": len(on_box),
        "walk_errors": getattr(backend, "walk_errors", None),
        "db_paths": len(in_db),
        "missing_on_box": missing_on_box,
        "extra_on_box": extra_on_box,
        "size_mismatch": size_mismatch,
        "missing_bytes": sum(in_db[p] or 0 for p in missing_on_box),
    }
    if missing_on_box:
        log.warning("backend %s: %d path(s) missing on box (%d bytes)",
                    backend.name, len(missing_on_box), result["missing_bytes"])
    if size_mismatch:
        log.warning("backend %s: %d path(s) with size mismatch",
                    backend.name, len(size_mismatch))
    return result

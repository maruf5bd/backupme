from backupme.log import get_logger

log = get_logger("gc")


def _referenced_remote_paths() -> dict[str, set[str]]:
    """backend name -> set of remote object paths referenced by the index.

    Range-suffixed rows (bundle members, ``path@offset:len``) count as a
    reference to the whole bundle file.
    """
    from backupme.core.database import execute

    refs: dict[str, set[str]] = {}
    for r in execute("SELECT DISTINCT backend_name, storage_path FROM blob_storage").fetchall():
        path = r["storage_path"].split("@", 1)[0]
        refs.setdefault(r["backend_name"], set()).add(path)
    return refs


def find_orphans(backends: list) -> dict[str, list[dict]]:
    """Per backend: remote blob files (objects/, bundle/) the index no longer
    references. Manifests and tree pointers are never candidates."""
    refs = _referenced_remote_paths()
    orphans: dict[str, list[dict]] = {}
    for bk in backends:
        refs_bk = refs.get(bk.name, set())
        items = []
        try:
            files = bk.walk("objects") + bk.walk("bundle")
        except Exception:
            log.warning("gc: cannot walk backend %s", bk.name, exc_info=True)
            continue
        for f in files:
            if f["path"] not in refs_bk:
                items.append(f)
        orphans[bk.name] = items
    return orphans


def run_gc(backends: list, apply: bool = False) -> dict:
    """Dry-run by default: report orphaned remote objects per backend.
    With apply=True, delete them."""
    orphans = find_orphans(backends)
    result = {}
    for name, items in orphans.items():
        entry = {"orphans": len(items), "bytes": sum(f["size"] for f in items)}
        if apply:
            bk = next((b for b in backends if b.name == name), None)
            deleted = 0
            for f in items:
                try:
                    bk.delete(f["path"])
                    deleted += 1
                except Exception as e:
                    log.warning("gc: delete failed for %s on %s: %s", f["path"], name, e)
            entry["deleted"] = deleted
        result[name] = entry
    return result

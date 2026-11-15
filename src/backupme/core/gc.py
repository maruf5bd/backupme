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


_DEFAULT_MIN_AGE_SECONDS = 3600


def find_orphans(backends: list, min_age_seconds: float = _DEFAULT_MIN_AGE_SECONDS) -> dict[str, list[dict]]:
    """Per backend: remote blob files (objects/, bundle/) the index no longer
    references. Manifests and tree pointers are never candidates.

    Objects younger than ``min_age_seconds`` are never candidates: an upload
    lands on the backend before its DB rows commit, so a freshly written
    object can look unreferenced for a short window (TOCTOU with in-flight
    uploads). Deleting it would corrupt the file the uploader is about to
    index. Files whose walk entry carries no mtime are also skipped —
    a leak is recoverable, a deleted live blob is not.
    """
    import time

    refs = _referenced_remote_paths()
    cutoff = time.time() - min_age_seconds
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
            if f["path"] in refs_bk:
                continue
            mtime = f.get("mtime")
            if min_age_seconds > 0 and (mtime is None or mtime > cutoff):
                continue
            items.append(f)
        orphans[bk.name] = items
    return orphans


def run_gc(backends: list, apply: bool = False,
           min_age_seconds: float = _DEFAULT_MIN_AGE_SECONDS) -> dict:
    """Dry-run by default: report orphaned remote objects per backend.
    With apply=True, delete them. See find_orphans for the min-age grace."""
    orphans = find_orphans(backends, min_age_seconds=min_age_seconds)
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

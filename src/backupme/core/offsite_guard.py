"""Off-site DB snapshot shipping: the DB-loss insurance layer.

The live SQLite database is the only map of the backup vault, and plain
``db snapshot`` files historically never left the working directory — a
single point of failure. ``ship_snapshot`` creates a fresh local snapshot
through the existing ``snapshot.create_snapshot()`` machinery (never
reimplemented here) and uploads that file to every configured backend under
a guarded, stable, sortable remote prefix:

    guard/snapshots/<UTC timestamp>-gen<generation>.db.zst

e.g. ``guard/snapshots/20261108T093015Z-gen3.db.zst`` — lexicographic order
equals chronological order, so a directory listing reads as a timeline.

Upload verification: after the put, the guarded prefix is listed on the
target backend and the reported object size must equal the local file size.
This is the cheapest size-capable check every backend API offers
(``list_files`` returns {path, size, mtime}) — a download-and-hash round
trip is deliberately avoided. ``list_files`` path conventions differ per
backend (full relative path vs directory listing basename), so entries are
matched by full path or basename. If no listing is obtainable, the check
degrades to ``exists()`` and the verification method is recorded per
backend as ``verify: "size"`` / ``"exists"``.

Deletion policy: this feature version NEVER deletes anything — remote
pruning of ``guard/snapshots/`` and local pruning driven by the advisory
``guard.keep_local_snapshots`` field are deliberately deferred.

Failure policy: one backend's failure never aborts the others — each
backend gets its own status entry and the overall status is
``"ok"`` (all uploaded) / ``"partial"`` (some) / ``"failed"`` (none, or no
reachable backends). The function never raises.
"""
import datetime
from pathlib import Path

from backupme.log import get_logger

log = get_logger("offsite_guard")

# Guarded remote prefix (stable + sortable: <UTC ts>-gen<gen>.db.zst names).
GUARD_PREFIX = "guard/snapshots"


def _resolve_backends(config, backends):
    """Use the given backend subset, or discover every reachable backend."""
    if backends is not None:
        return list(backends)
    from backupme.backends import get_all_backends
    return get_all_backends(config)


def _remote_name(generation) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-gen{int(generation or 0)}.db.zst"


def _verify_uploaded(bk, remote_path: str, local_size: int) -> dict:
    """Confirm the backend now serves an object of the same byte size.

    Primary: list the guarded prefix and match the object by full path or
    basename, comparing sizes. Fallback when listing is unavailable or
    unusable: a plain existence check. Never raises.
    """
    basename = remote_path.rsplit("/", 1)[-1]
    try:
        for entry in bk.list_files(GUARD_PREFIX):
            entry_path = str(entry.get("path") or "")
            if not (entry_path == remote_path or entry_path == basename
                    or entry_path.endswith("/" + basename)):
                continue
            try:
                remote_size = int(entry.get("size"))
            except (TypeError, ValueError):
                remote_size = None
            if remote_size == local_size:
                return {"verified": "size"}
            return {"verified": "size-mismatch", "remote_size": remote_size}
    except Exception as e:
        log.warning("guard verify: listing failed on backend %s: %s", bk.name, e)
    try:
        if bk.exists(remote_path):
            return {"verified": "exists"}
    except Exception as e:
        log.warning("guard verify: exists check failed on backend %s: %s", bk.name, e)
    return {"verified": "missing"}


def ship_snapshot(config=None, backends=None, snapshot: dict | None = None) -> dict:
    """Create (or reuse) a local DB snapshot and ship it to every backend.

    ``snapshot`` optionally carries a ``create_snapshot()`` result
    (``{"snapshot": name, "generation": gen}``) so callers that already made
    the snapshot (e.g. ``db snapshot --offsite``) avoid a second VACUUM INTO;
    when omitted a fresh snapshot is created and left in the working dir,
    exactly like every other snapshot.

    Returns::

        {
          "status": "ok" | "partial" | "failed",
          "snapshot": {"snapshot", "generation", "path", "size"} | None,
          "backends": {name: {"status": "ok"|"error", "remote_path", "size",
                              "verify"?, "error"?}},
        }

    plus a ``reason`` on fatal outcomes. Never raises; deletes nothing.
    """
    resolved = _resolve_backends(config, backends)
    if not resolved:
        return {
            "status": "failed",
            "reason": "no reachable backends",
            "snapshot": None,
            "backends": {},
        }

    snap = snapshot
    if snap is None:
        from backupme.core.snapshot import create_snapshot
        snap = create_snapshot()

    snap_path = Path(snap["snapshot"])
    if not snap_path.exists():
        return {
            "status": "failed",
            "reason": f"snapshot file missing: {snap['snapshot']}",
            "snapshot": None,
            "backends": {},
        }
    local_size = snap_path.stat().st_size
    snap_info = {
        "snapshot": snap["snapshot"],
        "generation": snap.get("generation"),
        "path": str(snap_path),
        "size": local_size,
    }

    remote_path = f"{GUARD_PREFIX}/{_remote_name(snap.get('generation'))}"

    results: dict = {}
    for bk in resolved:
        entry = {"status": "error", "remote_path": remote_path, "size": None}
        try:
            bk.upload(str(snap_path), remote_path)
            verdict = _verify_uploaded(bk, remote_path, local_size)
            if verdict["verified"] == "size":
                entry.update(status="ok", size=local_size, verify="size")
            elif verdict["verified"] == "exists":
                entry.update(status="ok", size=local_size, verify="exists")
            elif verdict["verified"] == "size-mismatch":
                entry["error"] = (
                    f"post-upload size verification failed "
                    f"(remote {verdict.get('remote_size')} != local {local_size})")
            else:
                entry["error"] = "uploaded object not found on backend after put"
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            log.warning("guard ship: backend %s failed: %s", bk.name, e)
        results[bk.name] = entry

    statuses = [e["status"] for e in results.values()]
    if all(s == "ok" for s in statuses):
        overall = "ok"
    elif any(s == "ok" for s in statuses):
        overall = "partial"
    else:
        overall = "failed"
    log.info("guard ship: status=%s snapshot=%s backends=%s",
             overall, snap_info["snapshot"], results)
    return {"status": overall, "snapshot": snap_info, "backends": results}

"""Rebalance / onboard / drain: fill a new storage backend with vault data
and retire old ones — reusing replicate_to_n (one download, re-upload), no
new copy engine.

Blob-level subtlety: bundle *member* rows (5-part ``bundle/a/b/tar/mem`` ids)
are byte ranges inside their parent bundle blob — their storage_path is
``<bundle>@<offset>:<len>``, not a standalone object. Rebalance therefore
operates on top-level blobs only (content chunks, bundle parents, raw
archives); copying a parent bundle carries all of its members.
"""
import json
from pathlib import Path

from backupme.log import get_logger

log = get_logger("rebalance")

from backupme.core.database import execute, commit
from backupme.core.flock import path_lock, safe_name

_CHECKPOINT_EVERY = 100


def _checkpoint_file(target_backend: str) -> Path:
    """Per-target checkpoint — a shared file would make blobs completed for
    target A be silently skipped when rebalancing to target B."""
    return Path(f"rebalance_checkpoint_{safe_name(target_backend)}.json")


def _top_level_missing(target_backend: str):
    """Blobs missing on the target: anti-join, excluding bundle member rows.

    Only REFERENCED blobs are planned — unreferenced blobs are gc candidates,
    and replicating them would spread garbage to a new backend (D14 fill run
    showed 29,620 such ids). Bundle member rows ride their parent, which the
    NOT LIKE filter handles by copying parents instead.
    """
    return execute(
        """SELECT b.blob_id, b.pt_size
           FROM blobs b
           WHERE b.ref_count > 0
             AND b.blob_id NOT IN (
                         SELECT blob_id FROM blob_storage WHERE backend_name = ?)
             AND b.blob_id NOT LIKE 'bundle/%/%/%/%'
           ORDER BY b.first_seen ASC, b.blob_id ASC""",
        (target_backend,),
    ).fetchall()


def plan_rebalance(target_backend: str, source_filter: str | None = None) -> dict:
    """Count blobs missing on the target and estimate bytes (dry-run data)."""
    rows = _top_level_missing(target_backend)
    if source_filter:
        rows = [r for r in rows if r["blob_id"].startswith(source_filter)]
    return {
        "target": target_backend,
        "blobs": len(rows),
        "bytes": sum(r["pt_size"] or 0 for r in rows),
    }


def _load_checkpoint(target: str) -> dict:
    ck = _checkpoint_file(target)
    if not ck.exists():
        return {"completed": [], "failed": []}
    try:
        data = json.loads(ck.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"completed": [], "failed": []}
    data.setdefault("completed", [])
    data.setdefault("failed", [])
    return data


def _save_checkpoint(target: str, data: dict) -> None:
    ck = _checkpoint_file(target)
    tmp = ck.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(ck)


def rebalance_to(target_backend, backends: list, config,
                 limit: int | None = None, progress_cb=None,
                 resume: bool = True) -> dict:
    """Copy every top-level blob missing on the target backend to it.

    Oldest first, per-blob try/except (continue on failure), checkpoint every
    100 blobs; resumable — completed blobs are skipped both via the checkpoint
    and the blob_storage anti-join itself. Serialized across processes with a
    per-target lock file (a web + CLI double-run would otherwise clobber the
    shared checkpoint).
    """
    from backupme.mirror import replicate_to_n

    target = next((b for b in backends if b.name == target_backend), None)
    if target is None:
        return {"status": "error", "error": f"backend '{target_backend}' not found or not connected"}

    ck_file = _checkpoint_file(target_backend)
    try:
        with path_lock(ck_file.with_suffix(".lock"), stale_seconds=3600.0, wait=0.0):
            return _rebalance_locked(target_backend, target, config, backends,
                                     limit, progress_cb, resume)
    except TimeoutError:
        log.warning("another rebalance to %s is running", target_backend)
        return {"status": "busy", "reason": f"another rebalance to {target_backend} is running"}


def _rebalance_locked(target_backend, target, config, backends,
                      limit, progress_cb, resume) -> dict:
    from backupme.mirror import replicate_to_n

    checkpoint = _load_checkpoint(target_backend) if resume else {"completed": [], "failed": []}
    completed: list = list(checkpoint["completed"])
    failed: list = []

    rows = _top_level_missing(target_backend)
    if limit:
        rows = rows[: int(limit)]

    copied = 0
    bytes_copied = 0
    for i, row in enumerate(rows):
        blob_id = row["blob_id"]
        if blob_id in completed:
            continue
        try:
            n = replicate_to_n(blob_id, [target], config)
            if n:
                copied += 1
                bytes_copied += row["pt_size"] or 0
            completed.append(blob_id)
        except Exception as e:
            log.warning("rebalance: failed to copy %s: %s", blob_id, e)
            failed.append(blob_id)
        if progress_cb is not None:
            progress_cb(blob_id, i + 1, len(rows))
        if (i + 1) % _CHECKPOINT_EVERY == 0:
            _save_checkpoint(target_backend, {"completed": completed[-1000:], "failed": failed})

    _save_checkpoint(target_backend, {"completed": completed[-1000:], "failed": failed})
    remaining = plan_rebalance(target_backend)
    return {
        "status": "ok" if not failed else "partial",
        "target": target_backend,
        "copied": copied,
        "bytes": bytes_copied,
        "failed": failed,
        "remaining_blobs": remaining["blobs"],
    }


def drain_backend(backend_name: str, backends: list, config,
                  progress_cb=None, dry_run: bool = False) -> dict:
    """Copy every blob that lives on ``backend_name`` onto the remaining
    healthy backends, then report the remote bytes eligible for GC.

    Never deletes anything — after a clean drain, remove the backend from the
    config and run ``backupme gc --apply`` to reclaim the orphaned objects.
    """
    targets = [b for b in backends if b.name != backend_name]
    if not targets:
        return {"status": "error", "error": "no other healthy backend to receive data"}

    rows = execute(
        """SELECT DISTINCT bs.blob_id, b.pt_size,
                  (SELECT COUNT(DISTINCT bs2.backend_name) FROM blob_storage bs2
                   WHERE bs2.blob_id = bs.blob_id) AS replica_count
           FROM blob_storage bs
           JOIN blobs b ON b.blob_id = bs.blob_id
           WHERE bs.backend_name = ?
             AND bs.blob_id NOT LIKE 'bundle/%/%/%/%'""",
        (backend_name,),
    ).fetchall()

    total_bytes = sum(r["pt_size"] or 0 for r in rows)
    single_replica = [r for r in rows if r["replica_count"] <= 1]
    drain_bytes = sum(r["pt_size"] or 0 for r in single_replica)

    if dry_run:
        return {
            "status": "dry-run",
            "backend": backend_name,
            "blobs": len(rows),
            "bytes": total_bytes,
            "blobs_to_copy": len(single_replica),
            "bytes_to_copy": drain_bytes,
        }

    from backupme.mirror import replicate_to_n

    copied = 0
    failed: list = []
    for i, row in enumerate(single_replica):
        try:
            n = replicate_to_n(row["blob_id"], targets, config)
            if n:
                copied += 1
        except Exception as e:
            log.warning("drain: failed to copy %s: %s", row["blob_id"], e)
            failed.append(row["blob_id"])
        if progress_cb is not None:
            progress_cb(row["blob_id"], i + 1, len(single_replica))

    # Post-drain accounting: what still exists ONLY on the drained backend.
    # Member rows are excluded — their data is carried by the parent bundle.
    leftover = execute(
        """SELECT COUNT(DISTINCT bs.blob_id) AS blobs,
                  COALESCE(SUM(b.pt_size), 0) AS bytes
           FROM blob_storage bs
           JOIN blobs b ON b.blob_id = bs.blob_id
           WHERE bs.backend_name = ?
             AND bs.blob_id NOT LIKE 'bundle/%/%/%/%'
             AND bs.blob_id NOT IN (
                 SELECT blob_id FROM blob_storage WHERE backend_name != ?)""",
        (backend_name, backend_name),
    ).fetchone()
    return {
        "status": "ok" if not failed else "partial",
        "backend": backend_name,
        "copied": copied,
        "failed": failed,
        "gc_eligible_blobs": rows and (len(rows) - leftover["blobs"]) or 0,
        "remaining_only_here_blobs": leftover["blobs"],
        "remaining_only_here_bytes": leftover["bytes"],
        "runbook": [
            f"1. lower '{backend_name}' priority to 0 in config.yaml (stop new writes)",
            f"2. remove '{backend_name}' from config.yaml",
            "3. backupme gc --apply   # reclaims now-orphaned remote objects",
        ],
    }


def backend_usage() -> list[dict]:
    """Per-backend stored bytes, distinct blob count, and last-24h ingest bytes."""
    rows = execute(
        """SELECT bs.backend_name,
                  COALESCE(SUM(bs.stored_size), 0) AS stored_bytes,
                  COUNT(DISTINCT bs.blob_id) AS blobs,
                  COALESCE(SUM(CASE WHEN bs.uploaded_at >= datetime('now', '-1 day')
                                    THEN bs.stored_size ELSE 0 END), 0) AS ingest_24h_bytes
           FROM blob_storage bs
           GROUP BY bs.backend_name""",
    ).fetchall()
    return [dict(r) for r in rows]

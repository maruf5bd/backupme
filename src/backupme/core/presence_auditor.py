"""
Physical Existence / Presence Auditor for BackupMe
Checks if files and blobs recorded in the SQLite database actually exist on:
1. Physical storage backends (Local disk, SFTP, Gdrive, etc.) - checks both loose objects and bundle members.
2. Local source disk (checks if original files in files table are still present on disk).
"""
import os
from typing import Dict, Any, List

from backupme.config import load_config
from backupme.backends import get_all_backends
from backupme.core.database import execute


def audit_storage_existence(limit: int = 1000, backend_filter: str = None) -> Dict[str, Any]:
    """
    Scans blob_storage records and verifies physical presence on target backend storage.
    Resolves bundle members (bundle/...@offset:len) by checking the parent bundle tar!
    """
    config = load_config()
    backends_list = get_all_backends(config)
    backends = {b.name: b for b in backends_list}

    # Also build a fallback list of local directory roots to check (e.g. H:\New folder\backupme, H:\Backupme)
    local_roots = []
    for b in backends_list:
        p = getattr(b, "base_path", None)
        if p and os.path.exists(p):
            local_roots.append(p)

    sql = "SELECT blob_id, backend_name, storage_path, stored_size FROM blob_storage"
    params = []
    if backend_filter:
        sql += " WHERE backend_name = ?"
        params.append(backend_filter)
    if limit > 0:
        sql += f" LIMIT {int(limit)}"

    rows = execute(sql, params).fetchall()

    checked = 0
    missing = 0
    bundle_resolved = 0
    errors = []

    parent_bundle_cache = {}

    for row in rows:
        checked += 1
        b_name = row["backend_name"]
        raw_path = row["storage_path"]
        blob_id = row["blob_id"]

        backend = backends.get(b_name)
        if not backend:
            missing += 1
            errors.append({"blob_id": blob_id, "backend": b_name, "error": f"Backend '{b_name}' not configured"})
            continue

        try:
            if "@" in raw_path:
                parent_path, _ = raw_path.split("@", 1)
                cache_key = f"{b_name}::{parent_path}"
                if cache_key in parent_bundle_cache:
                    exists = parent_bundle_cache[cache_key]
                else:
                    exists = backend.exists(parent_path)
                    if not exists and local_roots:
                        # Check alternate configured local roots
                        for root in local_roots:
                            if os.path.exists(os.path.join(root, parent_path.replace("/", os.sep))):
                                exists = True
                                break
                    parent_bundle_cache[cache_key] = exists
                bundle_resolved += 1
            else:
                exists = backend.exists(raw_path)
                if not exists and local_roots:
                    for root in local_roots:
                        if os.path.exists(os.path.join(root, raw_path.replace("/", os.sep))):
                            exists = True
                            break

            if not exists:
                missing += 1
                errors.append({"blob_id": blob_id, "backend": b_name, "path": raw_path, "status": "missing_on_storage"})
        except Exception as e:
            missing += 1
            errors.append({"blob_id": blob_id, "backend": b_name, "path": raw_path, "error": str(e)})

    return {
        "type": "storage_audit",
        "total_checked": checked,
        "present": checked - missing,
        "missing": missing,
        "bundle_members_resolved": bundle_resolved,
        "missing_samples": errors[:20]
    }


def audit_source_disk_presence(limit: int = 1000) -> Dict[str, Any]:
    """
    Checks if active files in DB (deleted_at IS NULL) are physically present
    on the original source filesystem.
    """
    sql = "SELECT id, source_root, relative_path, size FROM files WHERE deleted_at IS NULL"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"

    rows = execute(sql).fetchall()

    checked = 0
    present = 0
    source_gone = 0
    missing_samples = []

    for row in rows:
        checked += 1
        s_root = row["source_root"]
        r_path = row["relative_path"]

        # Resolve full path on local filesystem
        if s_root.startswith("cpanel/"):
            # cPanel virtual source
            continue

        full_path = os.path.join(s_root, r_path)
        if os.path.exists(full_path):
            present += 1
        else:
            source_gone += 1
            if len(missing_samples) < 20:
                missing_samples.append({
                    "file_id": row["id"],
                    "path": full_path,
                    "size": row["size"]
                })

    return {
        "type": "source_disk_audit",
        "total_checked": checked,
        "present_on_disk": present,
        "source_gone": source_gone,
        "samples_gone": missing_samples
    }

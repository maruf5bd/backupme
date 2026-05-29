import hashlib
import json
import os
import random
import tempfile
from pathlib import Path

from backupme.core.database import execute, commit
from backupme.core import hasher as hasher_mod
from backupme.core.downloader import download_file
from backupme.core.manifest import read_manifest
from backupme.backends import get_all_backends


def run_integrity_check(
    check_type: str = "random",
    count: int = 5,
    backend_name: str | None = None,
) -> dict:
    execute(
        "INSERT INTO integrity_checks (check_type, status, total_files, checked_files, failed_files) VALUES (?, 'running', ?, 0, 0)",
        (check_type, count),
    )
    check_id = execute("SELECT last_insert_rowid()").fetchone()[0]
    commit()

    files = execute(
        "SELECT id, relative_path, sha256 FROM files WHERE deleted_at IS NULL ORDER BY RANDOM() LIMIT ?",
        (count,),
    ).fetchall()

    checked = 0
    failed = 0
    details = []

    for f in files:
        try:
            tmp = Path(tempfile.mkdtemp()) / f["relative_path"].replace("/", "_")
            download_file(f["id"], str(tmp), backend_name)
            actual = hasher_mod.sha256_file(str(tmp))
            tmp.unlink()
            ok = actual == f["sha256"]
            if ok:
                checked += 1
            else:
                failed += 1
            details.append({"file_id": f["id"], "file": f["relative_path"], "status": "ok" if ok else "failed", "expected": f["sha256"], "actual": actual})
        except Exception as e:
            failed += 1
            details.append({"file_id": f["id"], "file": f["relative_path"], "status": "error", "error": str(e)})

    execute(
        "UPDATE integrity_checks SET status = ?, checked_files = ?, failed_files = ?, completed_at = datetime('now'), details = ? WHERE id = ?",
        ("completed" if failed == 0 else "failed", checked, failed, json.dumps(details), check_id),
    )
    commit()

    return {"check_id": check_id, "status": "completed" if failed == 0 else "failed", "checked": checked, "failed": failed}


def run_verify_restore():
    return run_integrity_check("full", 100)


def get_report() -> dict:
    file_count = execute("SELECT COUNT(*) FROM files WHERE deleted_at IS NULL").fetchone()[0]
    total_size = execute("SELECT COALESCE(SUM(size), 0) FROM files WHERE deleted_at IS NULL").fetchone()[0]
    blob_count = execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    blob_bytes = execute("SELECT COALESCE(SUM(pt_size), 0) FROM blobs").fetchone()[0]
    stored_bytes = execute("SELECT COALESCE(SUM(stored_size), 0) FROM blob_storage").fetchone()[0]

    last_check = execute(
        "SELECT * FROM integrity_checks ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    recovery = execute(
        "SELECT * FROM recovery_events ORDER BY created_at DESC LIMIT 5"
    ).fetchall()

    backend_stats = execute(
        "SELECT backend_name, SUM(stored_size) AS used_bytes, COUNT(*) AS blob_count FROM blob_storage GROUP BY backend_name"
    ).fetchall()

    dedup_ratio = (blob_bytes / stored_bytes) if stored_bytes > 0 else 1.0
    compression_ratio = (total_size / blob_bytes) if blob_bytes > 0 else 1.0

    recommendations = []
    if last_check and last_check["status"] == "failed":
        recommendations.append("Last integrity check failed — run 'backupme verify'")

    return {
        "file_count": file_count,
        "total_size": total_size,
        "blob_count": blob_count,
        "blob_bytes": blob_bytes,
        "stored_bytes": stored_bytes,
        "dedup_ratio": round(dedup_ratio, 2),
        "compression_ratio": round(compression_ratio, 2),
        "last_integrity_check": dict(last_check) if last_check else None,
        "recent_recovery_events": [dict(r) for r in recovery],
        "backend_stats": [dict(r) for r in backend_stats],
        "recommendations": recommendations,
    }


def list_reports(limit: int = 10) -> list[dict]:
    rows = execute(
        "SELECT * FROM integrity_checks ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
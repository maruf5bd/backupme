import hashlib
import json
import os
import random
import sqlite3
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

    if backend_name:
        files = execute(
            "SELECT DISTINCT f.id, f.relative_path, f.sha256 FROM files f "
            "INNER JOIN file_chunks fc ON f.id = fc.file_id "
            "INNER JOIN blob_storage bs ON fc.blob_id = bs.blob_id "
            "WHERE f.deleted_at IS NULL AND bs.backend_name = ? "
            "ORDER BY RANDOM() LIMIT ?",
            (backend_name, count),
        ).fetchall()
    else:
        files = execute(
            "SELECT id, relative_path, sha256 FROM files WHERE deleted_at IS NULL ORDER BY RANDOM() LIMIT ?",
            (count,),
        ).fetchall()

    checked = 0
    failed = 0
    details = []

    for f in files:
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / "check.bin"
                download_file(f["id"], str(tmp), backend_name)
                actual = hasher_mod.sha256_file(str(tmp))
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


def verify_file_integrity(file_id: int, backend_name: str | None = None) -> dict:
    row = execute("SELECT id, relative_path, sha256 FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        return {"status": "error", "message": "File not found"}

    tmp_dir = Path(tempfile.mkdtemp())
    tmp = tmp_dir / row["relative_path"].replace("/", "_").replace("\\", "_")
    try:
        download_file(file_id, str(tmp), backend_name)
        actual = hasher_mod.sha256_file(str(tmp))
        ok = actual == row["sha256"]
        return {
            "status": "ok" if ok else "failed",
            "file_id": file_id,
            "file": row["relative_path"],
            "expected": row["sha256"],
            "actual": actual,
            "match": ok
        }
    except Exception as e:
        return {
            "status": "error",
            "file_id": file_id,
            "file": row["relative_path"],
            "error": str(e)
        }
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
            if tmp_dir.exists():
                tmp_dir.rmdir()
        except Exception:
            pass


def run_verify_restore():
    from backupme.config import load_config
    try:
        count = load_config().verification.verify_restore_count or 100
    except Exception:
        count = 100
    return run_integrity_check("full", count)


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

    try:
        migrated_files = execute(
            "SELECT COUNT(*) FROM files WHERE migrated = 1"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        migrated_files = 0

    unrecoverable_rows = execute(
        """SELECT fc.file_id, f.relative_path, fc.blob_id
           FROM file_chunks fc
           LEFT JOIN blobs b ON b.blob_id = fc.blob_id
           LEFT JOIN files f ON f.id = fc.file_id
           WHERE b.blob_id IS NULL"""
    ).fetchall()
    unrecoverable_files = [
        {"file_id": r["file_id"], "file": r["relative_path"], "blob_id": r["blob_id"]}
        for r in unrecoverable_rows
    ]

    dedup_ratio = (blob_bytes / stored_bytes) if stored_bytes > 0 else 1.0
    compression_ratio = (total_size / blob_bytes) if blob_bytes > 0 else 1.0

    recommendations = []
    if last_check and last_check["status"] == "failed":
        recommendations.append("Last integrity check failed — run 'backupme verify'")
    if unrecoverable_files:
        recommendations.append(
            f"{len(unrecoverable_files)} file(s) reference missing blobs — re-backup required"
        )

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
        "migrated_files": migrated_files,
        "unrecoverable_files": unrecoverable_files,
        "recommendations": recommendations,
    }


def list_reports(limit: int = 10) -> list[dict]:
    rows = execute(
        "SELECT * FROM integrity_checks ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
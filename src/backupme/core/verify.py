"""Restore-and-hash verification plus self-healing of damaged or missing blobs.
"""
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
from backupme.log import get_logger

log = get_logger("verify")


def resolve_repair_donor(blob_id: str, damaged_backend: str | None = None) -> tuple[str | None, str | None, str | None]:
    """
    Finds a donor source to repair a damaged or missing blob.
    1. Check alternate backends for healthy stored copy
    2. Fall back to local source file on disk
    """
    # 1. Check alternate backends
    if damaged_backend:
        row = execute(
            "SELECT backend_name, storage_path FROM blob_storage "
            "WHERE blob_id = ? AND backend_name != ? ORDER BY uploaded_at DESC LIMIT 1",
            (blob_id, damaged_backend),
        ).fetchone()
    else:
        row = execute(
            "SELECT backend_name, storage_path FROM blob_storage "
            "WHERE blob_id = ? ORDER BY uploaded_at DESC LIMIT 1",
            (blob_id,),
        ).fetchone()

    if row:
        return ("backend", row["backend_name"], row["storage_path"])

    # 2. Check live source on disk
    file_ref = execute(
        "SELECT f.source_root, f.relative_path, f.sha256 FROM files f "
        "JOIN file_chunks fc ON f.id = fc.file_id "
        "WHERE fc.blob_id = ? AND f.deleted_at IS NULL LIMIT 1",
        (blob_id,),
    ).fetchone()

    if file_ref:
        full_path = os.path.join(file_ref["source_root"], file_ref["relative_path"])
        if os.path.exists(full_path):
            return ("source", full_path, file_ref["sha256"])

    return (None, None, None)


def heal_damaged_file(file_id: int, target_backend: str | None = None) -> dict:
    """
    Attempts to heal all missing or corrupt blobs for a given file.
    """
    from backupme.config import load_config
    config = load_config()
    backends_list = get_all_backends(config)
    backends = {b.name: b for b in backends_list}

    chunks = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ? ORDER BY seq",
        (file_id,),
    ).fetchall()

    if not chunks:
        return {"status": "error", "message": "No chunks found"}

    healed_count = 0
    errors = []

    for chunk in chunks:
        blob_id = chunk["blob_id"]
        donor_type, src, aux = resolve_repair_donor(blob_id, target_backend)

        if donor_type == "backend":
            donor_be = backends.get(src)
            target_be = backends.get(target_backend) if target_backend else None

            # If no specific target backend, restore to first available local backend
            if not target_be:
                for b in backends_list:
                    if getattr(b, "type", "") == "local":
                        target_be = b
                        break

            if donor_be and target_be:
                try:
                    # Bundle-safe transfer: if it's a bundle member, aux has "@offset:len"
                    remote_file = aux.split("@", 1)[0] if "@" in aux else aux
                    with tempfile.NamedTemporaryFile(delete=False) as tf:
                        tmp_name = tf.name
                    try:
                        donor_be.download(remote_file, tmp_name)
                        target_be.upload(tmp_name, remote_file)
                        healed_count += 1
                        log.info(f"Healed blob {blob_id[:16]} from backend {src} to {target_be.name}")
                    finally:
                        if os.path.exists(tmp_name):
                            os.remove(tmp_name)
                except Exception as e:
                    errors.append(f"Failed donor transfer for {blob_id[:16]}: {e}")

        elif donor_type == "source":
            # Live source re-back: re-run the real upload pipeline for the live
            # file that owns this blob (INT-3: the old call used a long-gone
            # upload_file signature, so every source heal died as TypeError
            # before touching a backend).
            try:
                owner = execute(
                    "SELECT f.source_root, f.relative_path FROM files f "
                    "JOIN file_chunks fc ON f.id = fc.file_id "
                    "WHERE fc.blob_id = ? AND f.deleted_at IS NULL LIMIT 1",
                    (blob_id,),
                ).fetchone()
                if not owner:
                    raise RuntimeError("no live file row references this blob")
                from backupme.core import uploader as uploader_mod
                res = uploader_mod.upload_file(
                    owner["source_root"], owner["relative_path"], src,
                    backends_list, config,
                )
                if res.get("status") not in ("ok", "partial"):
                    raise RuntimeError(str(res.get("error") or res.get("status")))
                healed_count += 1
                log.info(f"Healed blob {blob_id[:16]} directly from live disk source {src}")
            except Exception as e:
                errors.append(f"Failed source re-upload for {blob_id[:16]}: {e}")
        else:
            errors.append(f"No donor or live source found for {blob_id[:16]}")

    status = "healed" if healed_count > 0 and len(errors) == 0 else ("partial" if healed_count > 0 else "failed")
    return {"status": status, "healed_chunks": healed_count, "errors": errors}


def run_integrity_check(
    check_type: str = "random",
    count: int = 5,
    backend_name: str | None = None,
    auto_heal: bool = False,
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
                res = download_file(f["id"], str(tmp), backend_name)
                # Soft failures come back as a status dict without creating
                # the file — hashing then raised a misleading FileNotFoundError
                # instead of the real reason ("No chunks found").
                if isinstance(res, dict) and res.get("status") != "ok":
                    raise RuntimeError(str(res.get("message") or "download failed"))
                actual = hasher_mod.sha256_file(str(tmp))
            ok = actual == f["sha256"]
            if ok:
                checked += 1
                details.append({"file_id": f["id"], "file": f["relative_path"], "status": "ok", "expected": f["sha256"], "actual": actual})
            else:
                healed = False
                if auto_heal:
                    heal_res = heal_damaged_file(f["id"], backend_name)
                    if heal_res.get("status") in ("healed", "partial"):
                        healed = True
                        checked += 1
                        details.append({"file_id": f["id"], "file": f["relative_path"], "status": "healed", "expected": f["sha256"], "actual": actual, "heal_details": heal_res})
                if not healed:
                    failed += 1
                    details.append({"file_id": f["id"], "file": f["relative_path"], "status": "failed", "expected": f["sha256"], "actual": actual})
        except Exception as e:
            healed = False
            if auto_heal:
                heal_res = heal_damaged_file(f["id"], backend_name)
                if heal_res.get("status") in ("healed", "partial"):
                    healed = True
                    checked += 1
                    details.append({"file_id": f["id"], "file": f["relative_path"], "status": "healed", "error": str(e), "heal_details": heal_res})
            if not healed:
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
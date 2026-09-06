import hashlib
import tempfile
from pathlib import Path

from backupme.core.database import execute, commit, bump_generation
from backupme.core import op_log as op_log_mod
from backupme.core import dedup as dedup_mod
from backupme.core import compress as compress_mod
from backupme.core import crypto as crypto_mod
from backupme.backends import get_backend
from backupme.core.downloader import _find_chunk_storage
from backupme.core.uploader import _do_upload


def replicate_to_n(blob_id: str, target_backends: list, config: dict) -> int:
    storage_rows = _find_chunk_storage(blob_id)
    existing_backends = {s["backend_name"] for s in storage_rows}
    needed = [b for b in target_backends if b.name not in existing_backends]
    if not needed:
        return 0

    source_row = storage_rows[0]
    bk = get_backend(source_row["backend_name"], config)
    if bk is None:
        return 0

    tmp = Path(tempfile.mkdtemp()) / "replicate"
    bk.download(source_row["storage_path"], str(tmp))

    replicated = 0
    for target in needed:
        ok = _do_upload(target, str(tmp), source_row["storage_path"])
        if ok:
            execute(
                """INSERT OR IGNORE INTO blob_storage
                   (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                    compression_algo, compression_level, encrypted, nonce, auth_tag)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (blob_id, target.name, source_row["storage_path"],
                 source_row["stored_size"], source_row["stored_sha256"],
                 source_row["compression_algo"], source_row["compression_level"],
                 source_row["encrypted"], source_row["nonce"], source_row["auth_tag"]),
            )
            replicated += 1

    commit()
    tmp.unlink(missing_ok=True)
    return replicated


def mirror_file(file_id: int, target_backends: list, source_backend: str, config: dict) -> dict:
    chunks = execute(
        "SELECT * FROM file_chunks WHERE file_id = ? ORDER BY seq",
        (file_id,),
    ).fetchall()
    if not chunks:
        return {"status": "error", "message": "No chunks"}

    replicated = 0
    for chunk in chunks:
        n = replicate_to_n(chunk["blob_id"], target_backends, config)
        replicated += n

    if replicated > 0:
        execute("UPDATE files SET redundancy = ? WHERE id = ?",
                (len(target_backends) + 1, file_id))
        commit()

    op_log_mod.append("mirror", {"file_id": file_id, "replicated": replicated},
                       idempotency_key=f"mirror_{file_id}")
    return {"status": "ok", "chunks_replicated": replicated}


def check_consistency() -> dict:
    rows = execute(
        "SELECT DISTINCT f.id, f.sha256, f.size FROM files f WHERE f.deleted_at IS NULL AND f.redundancy >= 2",
    ).fetchall()

    total = len(rows)
    ok = 0
    mismatched = 0
    missing = 0

    for row in rows:
        chunks = execute(
            "SELECT * FROM file_chunks WHERE file_id = ? ORDER BY seq",
            (row["id"],),
        ).fetchall()
        for chunk in chunks:
            storage = _find_chunk_storage(chunk["blob_id"])
            if not storage:
                missing += 1
                continue
            hashes = {s["stored_sha256"] for s in storage}
            if len(hashes) > 1:
                mismatched += 1
            else:
                ok += 1

    return {"total": total, "ok": ok, "mismatched": mismatched, "missing": missing}
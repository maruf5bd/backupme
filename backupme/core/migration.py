import hashlib

from backupme.log import get_logger

log = get_logger("migration")
from backupme.core.database import execute, commit, get_connection
from backupme.core import dedup as dedup_mod


def run_migrations(backends: list, repair: bool = False) -> dict:
    """Idempotent maintenance migrations. Safe to run on every startup/check."""
    from backupme.core.schema import apply_migrations
    apply_migrations(get_connection())

    result: dict = {}
    result["refcounts_repaired"] = dedup_mod.reconcile_refcounts()
    if repair:
        result["inline_migrated"] = migrate_inline_files(backends)
    else:
        pending = execute(
            "SELECT COUNT(*) FROM files WHERE content_text IS NOT NULL AND deleted_at IS NULL"
        ).fetchone()[0]
        result["inline_pending"] = pending
    result["unrecoverable"] = dedup_mod.find_unrecoverable()
    return result


def migrate_inline_files(backends: list) -> dict:
    """Rebuild legacy <=1KB 'inline' rows as real blobs so they can be restored.

    The stored text is the only data that exists, so bytes are
    content_text.encode('utf-8'); when the recomputed hash differs from the
    originally recorded one the row is flagged (lossy) in the result.
    """
    from backupme.core.uploader import _upload_chunk_to_backends, _record_chunk_upload

    rows = execute(
        "SELECT id, sha256, size, relative_path, content_text FROM files "
        "WHERE content_text IS NOT NULL AND deleted_at IS NULL"
    ).fetchall()
    migrated, lossy, failed = 0, [], []
    for r in rows:
        existing_chunk = execute(
            "SELECT 1 FROM file_chunks WHERE file_id = ? LIMIT 1", (r["id"],)
        ).fetchone()
        if existing_chunk:
            execute("UPDATE files SET content_text = NULL WHERE id = ?", (r["id"],))
            commit()
            continue
        data = (r["content_text"] or "").encode("utf-8")
        new_sha = hashlib.sha256(data).hexdigest()
        storage_rows = _upload_chunk_to_backends(
            data, new_sha, backends, None, None, False, None, None
        )
        if not storage_rows:
            failed.append({"file_id": r["id"], "file": r["relative_path"]})
            continue
        _record_chunk_upload(new_sha, len(data), storage_rows, r["id"], 0, 0, len(data))
        execute(
            "UPDATE files SET sha256 = ?, size = ?, migrated = 1, content_text = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (new_sha, len(data), r["id"]),
        )
        commit()
        migrated += 1
        if new_sha != r["sha256"]:
            lossy.append({"file_id": r["id"], "file": r["relative_path"],
                          "old_sha256": r["sha256"], "new_sha256": new_sha})
    if lossy:
        log.warning("%d inline file(s) migrated lossily (non-UTF-8 original bytes "
                    "cannot be reconstructed); see report", len(lossy))
    return {"migrated": migrated, "lossy": lossy, "failed": failed}

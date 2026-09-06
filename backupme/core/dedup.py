from backupme.core.database import execute, commit


def record_blob(blob_id: str, pt_size: int) -> dict:
    row = execute("SELECT * FROM blobs WHERE blob_id = ?", (blob_id,)).fetchone()
    if row:
        execute("UPDATE blobs SET ref_count = ref_count + 1, last_seen = datetime('now') WHERE blob_id = ?", (blob_id,))
        commit()
        return dict(row)
    execute(
        "INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
        (blob_id, pt_size),
    )
    commit()
    return {"blob_id": blob_id, "pt_size": pt_size, "ref_count": 1}


def unref_blob(blob_id: str) -> int:
    execute("UPDATE blobs SET ref_count = ref_count - 1, last_seen = datetime('now') WHERE blob_id = ?", (blob_id,))
    row = execute("SELECT ref_count FROM blobs WHERE blob_id = ?", (blob_id,)).fetchone()
    if row and row[0] <= 0:
        execute("DELETE FROM blob_storage WHERE blob_id = ?", (blob_id,))
        execute("DELETE FROM blobs WHERE blob_id = ?", (blob_id,))
    commit()
    return row[0] if row and row[0] > 0 else 0


def _parent_blob_id(blob_id: str) -> str | None:
    """Parent bundle blob for a bundle member id, else None.

    Members look like 'bundle/<xx>/<yy>/<tarsha>/<membersha>'; the bundle itself
    has 4 slash-separated parts and has no parent. Other ids are untouched.
    """
    if not blob_id.startswith("bundle/"):
        return None
    if blob_id.count("/") < 4:
        return None
    return blob_id.rsplit("/", 1)[0]


def ensure_blob(blob_id: str, pt_size: int) -> None:
    """Insert the blob row if missing. Never touches ref_count."""
    execute(
        "INSERT OR IGNORE INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 0)",
        (blob_id, pt_size),
    )


def add_ref(blob_id: str) -> None:
    """Count one reference to this blob (one file_chunks row)."""
    execute(
        "UPDATE blobs SET ref_count = ref_count + 1, last_seen = datetime('now') WHERE blob_id = ?",
        (blob_id,),
    )


def unref_blob(blob_id: str) -> int:
    execute(
        "UPDATE blobs SET ref_count = MAX(ref_count - 1, 0), last_seen = datetime('now') WHERE blob_id = ?",
        (blob_id,),
    )
    row = execute("SELECT ref_count FROM blobs WHERE blob_id = ?", (blob_id,)).fetchone()
    ref = row[0] if row else 0
    if ref <= 0:
        parent = _parent_blob_id(blob_id)
        execute("DELETE FROM blob_storage WHERE blob_id = ?", (blob_id,))
        execute("DELETE FROM blobs WHERE blob_id = ?", (blob_id,))
        commit()
        if parent:
            return unref_blob(parent)
    commit()
    return ref


def reconcile_refcounts() -> int:
    """Enforce ref_count == (file_chunks references) + (bundle members)."""
    cur = execute(
        """UPDATE blobs SET ref_count =
               (SELECT COUNT(*) FROM file_chunks WHERE file_chunks.blob_id = blobs.blob_id)
             + (SELECT COUNT(*) FROM blobs m WHERE m.blob_id LIKE blobs.blob_id || '/%')
           WHERE ref_count !=
               (SELECT COUNT(*) FROM file_chunks WHERE file_chunks.blob_id = blobs.blob_id)
             + (SELECT COUNT(*) FROM blobs m WHERE m.blob_id LIKE blobs.blob_id || '/%')"""
    )
    commit()
    return cur.rowcount


def find_unrecoverable() -> list[dict]:
    rows = execute(
        """SELECT fc.file_id, fc.blob_id, f.relative_path
           FROM file_chunks fc
           LEFT JOIN blobs b ON b.blob_id = fc.blob_id
           LEFT JOIN files f ON f.id = fc.file_id
           WHERE b.blob_id IS NULL"""
    ).fetchall()
    return [
        {"file_id": r["file_id"], "blob_id": r["blob_id"], "file": r["relative_path"]}
        for r in rows
    ]


def get_blob(blob_id: str) -> dict | None:
    row = execute("SELECT * FROM blobs WHERE blob_id = ?", (blob_id,)).fetchone()
    return dict(row) if row else None


def list_orphans() -> list[dict]:
    rows = execute("SELECT * FROM blobs WHERE ref_count <= 0").fetchall()
    return [dict(r) for r in rows]


def total_dedup_savings() -> dict:
    row = execute(
        "SELECT COUNT(DISTINCT bs.blob_id) AS total_blobs, "
        "COALESCE(SUM(b.pt_size), 0) AS raw_bytes, "
        "COALESCE(SUM(bs.stored_size), 0) AS stored_bytes "
        "FROM blob_storage bs "
        "LEFT JOIN blobs b ON bs.blob_id = b.blob_id"
    ).fetchone()
    total_blobs = row["total_blobs"] or 0 if row else 0
    raw = row["raw_bytes"] or 0 if row else 0
    stored = row["stored_bytes"] or 0 if row else 0
    return {
        "total_blobs": total_blobs,
        "raw_bytes": raw,
        "stored_bytes": stored,
        "savings_bytes": raw - stored,
        "ratio": (raw / stored) if stored > 0 else 1.0,
    }
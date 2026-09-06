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
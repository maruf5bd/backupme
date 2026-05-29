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
    commit()
    row = execute("SELECT ref_count FROM blobs WHERE blob_id = ?", (blob_id,)).fetchone()
    if row and row[0] <= 0:
        execute("DELETE FROM blobs WHERE blob_id = ?", (blob_id,))
        commit()
        return 0
    commit()
    return row[0] if row else 0


def get_blob(blob_id: str) -> dict | None:
    row = execute("SELECT * FROM blobs WHERE blob_id = ?", (blob_id,)).fetchone()
    return dict(row) if row else None


def list_orphans() -> list[dict]:
    rows = execute("SELECT * FROM blobs WHERE ref_count <= 0").fetchall()
    return [dict(r) for r in rows]


def total_dedup_savings() -> dict:
    row = execute(
        "SELECT COUNT(*) AS total_blobs, SUM(pt_size) AS raw_bytes, "
        "SUM(stored_size) AS stored_bytes "
        "FROM blob_storage"
    ).fetchone()
    if not row or not row["total_blobs"]:
        return {"total_blobs": 0, "raw_bytes": 0, "stored_bytes": 0, "savings_bytes": 0}
    raw = row["raw_bytes"] or 0
    stored = row["stored_bytes"] or 0
    return {
        "total_blobs": row["total_blobs"],
        "raw_bytes": raw,
        "stored_bytes": stored,
        "savings_bytes": raw - stored,
        "ratio": (raw / stored) if stored > 0 else 1.0,
    }
from backupme.core.database import execute, commit


def index_file(
    source_root: str,
    relative_path: str,
    sha256: str,
    size: int,
    mtime: float = 0.0,
    chunked: bool = False,
    redundancy: int = 1,
    version_group: str | None = None,
    version_seq: int = 1,
    content_text: str | None = None,
    auto_commit: bool = True,
) -> int:
    vg = version_group or sha256[:16]
    row = execute(
        "SELECT id FROM files WHERE source_root = ? AND relative_path = ? AND version_seq = ? AND deleted_at IS NULL",
        (source_root, relative_path, version_seq),
    ).fetchone()
    if row:
        execute(
            "UPDATE files SET sha256 = ?, size = ?, mtime = ?, chunked = ?, updated_at = datetime('now') WHERE id = ?",
            (sha256, size, mtime, 1 if chunked else 0, row[0]),
        )
        execute(
            "DELETE FROM files_fts WHERE rowid = ?", (row[0],)
        )
        execute(
            "INSERT INTO files_fts(rowid, relative_path, content_text) VALUES (?, ?, ?)",
            (row[0], relative_path, content_text),
        )
        if auto_commit:
            commit()
        return row[0]

    execute(
        """INSERT INTO files
           (source_root, relative_path, sha256, size, mtime, chunked, redundancy, version_group, version_seq, content_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_root, relative_path, sha256, size, mtime,
         1 if chunked else 0, redundancy, vg, version_seq, content_text),
    )
    new_id = execute("SELECT last_insert_rowid()").fetchone()[0]
    execute(
        "INSERT INTO files_fts(rowid, relative_path, content_text) VALUES (?, ?, ?)",
        (new_id, relative_path, content_text),
    )
    if auto_commit:
        commit()
    return new_id


def find_by_path(source_root: str, relative_path: str) -> list[dict]:
    rows = execute(
        "SELECT * FROM files WHERE source_root = ? AND relative_path = ? AND deleted_at IS NULL ORDER BY version_seq DESC",
        (source_root, relative_path),
    ).fetchall()
    return [dict(r) for r in rows]


def find_by_sha256(sha256: str) -> list[dict]:
    rows = execute(
        "SELECT * FROM files WHERE sha256 = ? AND deleted_at IS NULL ORDER BY created_at DESC",
        (sha256,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_latest_version(source_root: str, relative_path: str) -> dict | None:
    row = execute(
        "SELECT * FROM files WHERE source_root = ? AND relative_path = ? AND deleted_at IS NULL ORDER BY version_seq DESC LIMIT 1",
        (source_root, relative_path),
    ).fetchone()
    return dict(row) if row else None


def search_files(query: str, source_root: str | None = None) -> list[dict]:
    if source_root:
        rows = execute(
            "SELECT f.* FROM files f INNER JOIN files_fts ON f.id = files_fts.rowid "
            "WHERE files_fts MATCH ? AND f.source_root = ? AND f.deleted_at IS NULL "
            "ORDER BY rank LIMIT 100",
            (query, source_root),
        ).fetchall()
    else:
        rows = execute(
            "SELECT f.* FROM files f INNER JOIN files_fts ON f.id = files_fts.rowid "
            "WHERE files_fts MATCH ? AND f.deleted_at IS NULL ORDER BY rank LIMIT 100",
            (query,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_files(source_root: str | None = None, backend: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    if backend:
        sql = ("SELECT DISTINCT f.* FROM files f "
               "INNER JOIN file_chunks fc ON f.id = fc.file_id "
               "INNER JOIN blob_storage bs ON fc.blob_id = bs.blob_id "
               "WHERE f.deleted_at IS NULL AND bs.backend_name = ?")
        params: list = [backend]
        if source_root:
            sql += " AND f.source_root = ?"
            params.append(source_root)
        sql += " ORDER BY f.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    else:
        sql = "SELECT * FROM files WHERE deleted_at IS NULL"
        params = []
        if source_root:
            sql += " AND source_root = ?"
            params.append(source_root)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def file_count() -> int:
    row = execute("SELECT COUNT(*) FROM files WHERE deleted_at IS NULL").fetchone()
    return row[0] if row else 0


def file_count_by_source() -> list[dict]:
    rows = execute(
        "SELECT source_root, COUNT(*) AS cnt FROM files WHERE deleted_at IS NULL GROUP BY source_root"
    ).fetchall()
    return [dict(r) for r in rows]


def next_version_seq(source_root: str, relative_path: str) -> int:
    """Next version_seq for a path, over ALL rows (soft-deleted included).

    Deliberately no deleted_at filter: a purged-but-undeleted history must
    never reuse a seq (files UNIQUE(source_root, relative_path, version_seq)).
    """
    row = execute(
        "SELECT MAX(version_seq) FROM files WHERE source_root = ? AND relative_path = ?",
        (source_root, relative_path),
    ).fetchone()
    if row and row[0] is not None:
        return row[0] + 1
    return 1


def hard_delete_file(file_id: int):
    """Hard-delete a file row and its chunk mappings, unreferencing blobs."""
    from backupme.core import dedup as dedup_mod

    blob_rows = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ?", (file_id,)
    ).fetchall()
    execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))
    execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
    execute("DELETE FROM files WHERE id = ?", (file_id,))
    for r in blob_rows:
        dedup_mod.unref_blob(r[0])
    commit()


def delete_file(file_id: int):
    fts_id = execute("SELECT id FROM files WHERE id = ?", (file_id,)).fetchone()
    if fts_id:
        execute("DELETE FROM files_fts WHERE rowid = ?", (fts_id[0],))
    execute("UPDATE files SET deleted_at = datetime('now') WHERE id = ?", (file_id,))
    commit()
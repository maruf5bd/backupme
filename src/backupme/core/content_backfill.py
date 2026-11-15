"""Backfill files.content_text for EXISTING rows so content search covers
historical data.

Companion to the upload-time population in core/uploader.py, which only fills
content_text for NEW uploads: rows that predate that change keep
content_text NULL, so 'backupme search --content' and the dashboard content
search cannot find them. backfill_content() reconstructs each candidate's
plaintext through the normal restore path (downloader.download_file, so
bundles, byte-range members and compression are handled) and stores a
snippet under the same rules the upload path applies:

- live rows only (deleted_at IS NULL) that actually have chunk rows (a row
  without chunks cannot be downloaded),
- size cap (default 64 KiB, matching the upload-time _content_text_max_bytes),
- strict UTF-8 decode only — anything binary (or empty) stays NULL,
- when config.encryption.enabled is truthy the run is a no-op
  ({"status": "skipped_encrypted"}): we never decrypt-and-index encrypted
  backups' plaintext into the database, mirroring the upload-time gate.

FTS5 sync: files_fts is an external-content table (content='files'), so a
plain UPDATE of files.content_text never reaches the search index, and
per-row DELETEs cannot remove terms that were indexed under older row
values (drift left by legacy writes). After a batch that updated at least
one row we therefore run the supported FTS5 rebuild command —
INSERT INTO files_fts(files_fts) VALUES('rebuild') — which re-derives the
whole index from the current files table in one statement. (The Python
helper index.rebuild_fts() is deliberately NOT used: it currently raises
NameError — it calls executemany(), which index.py never imports.)

Safety: non-destructive by construction — the only write to the files table
is UPDATE ... SET content_text = ? WHERE id = ? AND content_text IS NULL
(re-checked inside the statement), the index is re-derived from the files
table, and no row or blob is ever deleted. One file's failure never aborts
the batch: it is counted in "errors" and logged with traceback.

Resumable: "remaining" counts the still-NULL live chunk-backed rows within
the size cap (scoped to --backend when given); re-run until it is 0. Note
that rows whose content is not text (binary/empty) legitimately stay NULL
and keep being counted — on repos with such content "remaining" settles at
the skipped_binary count rather than 0; the counter shows why.
"""
import shutil
import tempfile
from pathlib import Path

from backupme.core.database import execute, commit, rollback
from backupme.log import get_logger

log = get_logger("content_backfill")

DEFAULT_MAX_SIZE = 65536  # bytes; matches the upload-time content cap


def _scoped_where(backend: str | None, size_bound: int, above_cap: bool) -> tuple[str, tuple]:
    """Shared WHERE clause for candidate / remaining / too_large accounting.

    Every counted row is live, unindexed (content_text IS NULL), within (or,
    for too_large accounting, above) the size cap, and has at least one chunk
    row. With a backend filter, the row must also have at least one chunk
    copy stored on that backend (bundle members count through their parent
    blob's blob_storage rows — the join is over blob ids, not storage paths).
    """
    op = ">" if above_cap else "<="
    sql = ("FROM files f "
           "WHERE f.deleted_at IS NULL AND f.content_text IS NULL "
           f"AND f.size {op} ? "
           "AND EXISTS (SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id)")
    params: list = [size_bound]
    if backend:
        sql += (" AND EXISTS (SELECT 1 FROM file_chunks fc2 "
                "JOIN blob_storage bs ON bs.blob_id = fc2.blob_id "
                "WHERE fc2.file_id = f.id AND bs.backend_name = ?)")
        params.append(backend)
    return sql, tuple(params)


def _count_candidates(backend: str | None, max_size: int, above_cap: bool = False) -> int:
    where, params = _scoped_where(backend, max_size, above_cap)
    row = execute(f"SELECT COUNT(DISTINCT f.id) {where}", params).fetchone()
    return row[0] if row else 0


def backfill_content(config, limit: int = 500, max_size: int = DEFAULT_MAX_SIZE,
                     backend: str | None = None, dry_run: bool = False) -> dict:
    """Backfill content_text for existing rows. Returns a summary dict.

    dry_run=True performs candidates-only planning: the candidate query runs
    (read-only) but nothing is downloaded and nothing is updated — the cheap
    default, since a real download is the expensive part an operator wants
    to preview counts for, not repeat.
    """
    # Encrypted backups: never decrypt-and-index plaintext into the DB.
    enc_cfg = getattr(config, "encryption", None)
    if enc_cfg is not None and getattr(enc_cfg, "enabled", False):
        remaining = _count_candidates(backend, max_size)
        return {"status": "skipped_encrypted", "scanned": 0, "updated": 0,
                "skipped_binary": 0, "too_large": 0, "errors": 0,
                "remaining": remaining}

    limit = max(0, int(limit))
    too_large = _count_candidates(backend, max_size, above_cap=True)
    where, params = _scoped_where(backend, max_size, above_cap=False)
    if limit == 0:
        file_ids: list[int] = []
    else:
        rows = execute(
            f"SELECT DISTINCT f.id {where} ORDER BY f.id LIMIT ?",
            params + (limit,),
        ).fetchall()
        file_ids = [r[0] for r in rows]
    remaining = _count_candidates(backend, max_size)

    if dry_run:
        return {"status": "dry-run", "scanned": 0, "updated": 0,
                "skipped_binary": 0, "too_large": too_large, "errors": 0,
                "remaining": remaining, "candidates": len(file_ids)}

    from backupme.core.downloader import download_file  # lazy: CLI startup stays fast

    scanned = updated = skipped_binary = errors = 0
    tmp_dir = Path(tempfile.mkdtemp(prefix="backupme_content_backfill_"))
    try:
        for file_id in file_ids:
            scanned += 1
            dest = tmp_dir / f"content_{file_id}.bin"
            text: str | None = None
            try:
                dl = download_file(file_id, str(dest), backend_name=backend)
                if dl.get("status") != "ok":
                    errors += 1
                    log.warning("backfill id=%s: download status=%s message=%s",
                                file_id, dl.get("status"), dl.get("message"))
                else:
                    try:
                        raw = dest.read_bytes()
                        if not raw:
                            # Empty content is not indexable text (the upload
                            # path also leaves empty files with content_text NULL).
                            skipped_binary += 1
                        else:
                            text = raw.decode("utf-8")  # strict: binary stays NULL
                    except UnicodeDecodeError:
                        skipped_binary += 1
            except Exception:
                errors += 1
                log.exception("backfill id=%s: restore failed", file_id)
            finally:
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
            if text is None:
                continue
            try:
                cur = execute(
                    "UPDATE files SET content_text = ? "
                    "WHERE id = ? AND content_text IS NULL",
                    (text, file_id),
                )
                if cur.rowcount:
                    updated += 1
                commit()
            except Exception:
                errors += 1
                rollback()
                log.exception("backfill id=%s: index update failed", file_id)
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)

    if updated:
        # External-content FTS5 index: re-derive it from the files table so
        # the new snippets (and any pre-existing drift) are searchable.
        try:
            execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
            commit()
        except Exception:
            errors += 1
            rollback()
            log.exception("files_fts rebuild failed after backfill")

    remaining = _count_candidates(backend, max_size)
    return {"status": "ok", "scanned": scanned, "updated": updated,
            "skipped_binary": skipped_binary, "too_large": too_large,
            "errors": errors, "remaining": remaining}

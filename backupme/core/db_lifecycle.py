"""DB lifecycle: keep SQLite hot-store proportional to retention + grace by
exporting history rows to monthly Zstd-compressed Parquet partitions and
hard-purging what has already been exported.

Topology (locked, Plan 02):
- SQLite stays the sole writer (ingest, dedup, retention, op_log).
- ``cold_export_to_parquet`` copies ``files`` rows (by id watermark) into
  ``parquet/files_YYYY-MM.parquet`` (pyarrow, zstd level 3, format 2.6,
  dictionary-encoded string columns, statistics on).
- ``hard_purge_versions`` hard-deletes soft-deleted, grace-expired rows that
  are already covered by the export watermark.
- ``query_history_duckdb`` runs read-only DuckDB SQL over the partitions.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from backupme.log import get_logger

log = get_logger("db_lifecycle")

from backupme.core.database import execute, commit

_ROW_GROUP_TARGET = 128 * 1024 * 1024  # ~128MB row groups per the plan

_FILES_SCHEMA_FIELDS = [
    ("id", "int64"),
    ("source_root", "string"),
    ("relative_path", "string"),
    ("sha256", "string"),
    ("size", "int64"),
    ("mtime", "float64"),
    ("chunked", "bool"),
    ("redundancy", "int32"),
    ("version_group", "string"),
    ("version_seq", "int32"),
    ("content_text", "string"),
    ("migrated", "int8"),
    ("created_at", "string"),
    ("updated_at", "string"),
    ("deleted_at", "string"),
]

_DICTIONARY_COLUMNS = ["source_root", "relative_path", "sha256", "version_group"]


# ---------------------------------------------------------------------------
# Parquet partition + watermark manifest helpers
# ---------------------------------------------------------------------------

def parquet_dir_for(config=None) -> Path:
    """Resolve the parquet output dir from config (dataclass or dict)."""
    if config is None:
        from backupme.config import load_config
        config = load_config()
    default = "parquet"
    if isinstance(config, dict):
        lc = config.get("db_lifecycle") or {}
        if isinstance(lc, dict):
            return Path(lc.get("parquet_dir", default) or default)
        return Path(getattr(lc, "parquet_dir", default) or default)
    lc = getattr(config, "db_lifecycle", None)
    return Path(getattr(lc, "parquet_dir", default) or default)


def _lifecycle_cfg(config=None):
    if config is None:
        from backupme.config import load_config
        config = load_config()
    if isinstance(config, dict):
        return config.get("db_lifecycle") or {}
    return getattr(config, "db_lifecycle", None)


def _manifest_path(pdir: Path) -> Path:
    return pdir / "_manifest.json"


def load_watermark(pdir: Path) -> int:
    mp = _manifest_path(pdir)
    if not mp.exists():
        return 0
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("unreadable parquet manifest %s — treating watermark as 0", mp)
        return 0
    return int(data.get("watermark", 0) or 0)


def _save_manifest(pdir: Path, manifest: dict) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    tmp = pdir / "_manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(_manifest_path(pdir))


# ---------------------------------------------------------------------------
# Cold export
# ---------------------------------------------------------------------------

def cold_export_to_parquet(config=None) -> dict:
    """Export ``files`` rows past the id watermark to a monthly Zstd Parquet
    partition and advance the watermark. No-op (with warning) when pyarrow
    is missing."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        log.warning("pyarrow not installed — cold export skipped")
        return {"status": "skipped", "reason": "pyarrow not installed"}

    lc = _lifecycle_cfg(config)
    compression = getattr(lc, "parquet_compression", "zstd") if not isinstance(lc, dict) \
        else lc.get("parquet_compression", "zstd")
    compression_level = getattr(lc, "parquet_compression_level", 3) if not isinstance(lc, dict) \
        else lc.get("parquet_compression_level", 3)

    pdir = parquet_dir_for(config)
    pdir.mkdir(parents=True, exist_ok=True)
    watermark = load_watermark(pdir)

    rows = execute(
        "SELECT id, source_root, relative_path, sha256, size, mtime, chunked, "
        "redundancy, version_group, version_seq, content_text, migrated, "
        "created_at, updated_at, deleted_at "
        "FROM files WHERE id > ? ORDER BY id",
        (watermark,),
    ).fetchall()
    if not rows:
        return {"status": "ok", "partition": None, "rows": 0,
                "watermark": watermark, "message": "nothing to export"}

    schema = pa.schema([
        pa.field(n, getattr(pa, "bool_" if t == "bool" else t)())
        for n, t in _FILES_SCHEMA_FIELDS
    ])
    records = []
    for r in rows:
        records.append({
            "id": int(r["id"]),
            "source_root": r["source_root"],
            "relative_path": r["relative_path"],
            "sha256": r["sha256"],
            "size": int(r["size"]),
            "mtime": float(r["mtime"]) if r["mtime"] is not None else None,
            "chunked": bool(r["chunked"]),
            "redundancy": int(r["redundancy"] or 1),
            "version_group": r["version_group"],
            "version_seq": int(r["version_seq"] or 1),
            "content_text": r["content_text"],
            "migrated": int(r["migrated"] or 0),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "deleted_at": r["deleted_at"],
        })
    new_table = pa.Table.from_pylist(records, schema=schema)

    # New rows always have id > watermark, so a same-month partition can only
    # grow by concatenation — no duplicates possible.
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    partition_name = f"files_{month}.parquet"
    partition_path = pdir / partition_name
    if partition_path.exists():
        existing = pq.read_table(partition_path, schema=schema)
        table = pa.concat_tables([existing, new_table])
    else:
        table = new_table

    pq.write_table(
        table, partition_path,
        compression=compression,
        compression_level=compression_level,
        version="2.6",
        use_dictionary=_DICTIONARY_COLUMNS,
        write_statistics=True,
        row_group_size=max(1, _ROW_GROUP_TARGET // 256),  # conservative rows/group
    )

    new_watermark = records[-1]["id"]
    manifest = {"watermark": max(watermark, new_watermark), "partitions": {}}
    mp = _manifest_path(pdir)
    if mp.exists():
        try:
            manifest = json.loads(mp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
        manifest.setdefault("partitions", {})
    manifest["watermark"] = max(watermark, new_watermark)
    manifest["partitions"][partition_name] = {
        "max_id": new_watermark,
        "rows": table.num_rows,
        "codec": compression,
        "bytes": partition_path.stat().st_size,
    }
    _save_manifest(pdir, manifest)

    return {
        "status": "ok",
        "partition": partition_name,
        "rows": len(records),
        "total_rows": table.num_rows,
        "watermark": manifest["watermark"],
        "bytes": partition_path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# Hard purge
# ---------------------------------------------------------------------------

def hard_purge_versions(grace_days: int | None = None, config=None) -> dict:
    """Hard-delete soft-deleted rows that are past the grace window AND below
    the cold-export watermark (i.e. safely in Parquet history)."""
    from backupme.core import dedup as dedup_mod

    lc = _lifecycle_cfg(config)
    if grace_days is None:
        grace_days = getattr(lc, "purge_grace_days", 14) if not isinstance(lc, dict) \
            else lc.get("purge_grace_days", 14)
    grace_days = int(grace_days)

    pdir = parquet_dir_for(config)
    watermark = load_watermark(pdir)

    rows = execute(
        "SELECT id FROM files "
        "WHERE deleted_at IS NOT NULL "
        "AND deleted_at < datetime('now', ?) "
        "AND id <= ?",
        (f"-{grace_days} days", watermark),
    ).fetchall()

    purged = 0
    skipped = 0
    for (fid,) in rows:
        if fid > watermark:
            skipped += 1
            continue
        chunks = execute(
            "SELECT blob_id FROM file_chunks WHERE file_id = ?", (fid,)
        ).fetchall()
        execute("DELETE FROM file_chunks WHERE file_id = ?", (fid,))
        execute("DELETE FROM files_fts WHERE rowid = ?", (fid,))
        for (blob_id,) in chunks:
            dedup_mod.unref_blob(blob_id)
        execute("DELETE FROM files WHERE id = ?", (fid,))
        purged += 1
        if purged % 1000 == 0:
            commit()
    if purged:
        commit()

    return {"purged": purged, "skipped": skipped, "watermark": watermark,
            "grace_days": grace_days}


# ---------------------------------------------------------------------------
# DuckDB history queries
# ---------------------------------------------------------------------------

def query_history_duckdb(sql: str, config=None) -> list:
    """Run read-only DuckDB SQL over the parquet partitions.

    A view named ``files`` is registered over ``read_parquet(<glob>)`` so
    callers write plain SQL: ``SELECT * FROM files WHERE relative_path = ?``.
    Returns a list of dicts; a single-row error marker on failure.
    """
    try:
        import duckdb
    except ImportError:
        log.warning("duckdb not installed — history query unavailable")
        return [{"status": "unavailable", "message": "duckdb not installed"}]

    pdir = parquet_dir_for(config)
    partitions = sorted(str(p) for p in pdir.glob("files_*.parquet"))
    con = duckdb.connect()
    try:
        if partitions:
            glob_escaped = str(pdir / "files_*.parquet").replace("'", "''")
            con.execute(
                f"CREATE VIEW files AS SELECT * FROM read_parquet('{glob_escaped}', union_by_name=true)"
            )
        else:
            con.execute(
                "CREATE VIEW files AS SELECT "
                "NULL::BIGINT AS id, NULL::VARCHAR AS source_root, "
                "NULL::VARCHAR AS relative_path, NULL::VARCHAR AS sha256, "
                "NULL::BIGINT AS size, NULL::DOUBLE AS mtime, "
                "NULL::BOOLEAN AS chunked, NULL::INTEGER AS redundancy, "
                "NULL::VARCHAR AS version_group, NULL::INTEGER AS version_seq, "
                "NULL::VARCHAR AS content_text, NULL::TINYINT AS migrated, "
                "NULL::VARCHAR AS created_at, NULL::VARCHAR AS updated_at, "
                "NULL::VARCHAR AS deleted_at WHERE FALSE"
            )
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception as e:
        log.error("history query failed: %s", e, exc_info=True)
        return [{"status": "error", "message": str(e)}]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def vacuum_if_needed(config=None) -> dict:
    """VACUUM the hot SQLite database (safe: full rewrite, WAL mode)."""
    execute("VACUUM")
    commit()
    return {"vacuumed": True}


def prune_oplog(days: int = 30, config=None) -> dict:
    """Archive terminal op_log rows older than ``days`` to a monthly JSONL
    file, then remove them from the hot DB."""
    rows = execute(
        "SELECT * FROM op_log "
        "WHERE status IN ('committed', 'failed', 'rolled_back') "
        "AND completed_at IS NOT NULL "
        "AND completed_at < datetime('now', ?)",
        (f"-{int(days)} days",),
    ).fetchall()
    if not rows:
        return {"pruned": 0, "file": None}

    jsonl_dir = Path("jsonl")
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    out_path = jsonl_dir / f"op_log_{month}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(dict(r), default=str) + "\n")

    for r in rows:
        execute("DELETE FROM op_log WHERE id = ?", (r["id"],))
    commit()
    return {"pruned": len(rows), "file": str(out_path)}

"""Misc API routes (backlog B05): status, logs (list/export), report
(view/generate), reports history, search, encrypt init/status, activity,
stats trends, convert, recompress, hash lookup, replication status.
"""
import io
import json
import sqlite3
import time
from pathlib import Path

from fastapi import Body
from fastapi.responses import JSONResponse, StreamingResponse

from backupme.core import database as db
from backupme.core import crypto as crypto_mod
from backupme.core import compress as compress_mod
from backupme.core import dedup, index as index_mod, verify
from backupme.log import get_logger

from .common import (_get_backends, _fts_phrase, _search_files_like,
                     op_log_start, op_log_finish)

log = get_logger("web.routes.misc")


def register(app, ctx):

    @app.get("/api/status")
    async def api_status():
        total_files = index_mod.file_count()
        savings = dedup.total_dedup_savings()
        gen = db.get_generation()
        backends = _get_backends(ctx)

        # Calculate actual stored bytes per backend from DB
        backend_sizes = {}
        try:
            rows = db.execute(
                "SELECT backend_name, SUM(stored_size) as total_size FROM blob_storage GROUP BY backend_name"
            ).fetchall()
            for r in rows:
                backend_sizes[r["backend_name"]] = r["total_size"]
        except Exception as e:
            log.warning("stored-bytes query failed for /api/status: %s", e)

        be_full = []
        for b in backends:
            be_full.append({
                "name": b.name,
                "type": b.type,
                "connected": b.connected,
                "stored_bytes": backend_sizes.get(b.name, 0)
            })

        return JSONResponse({
            "total_files": total_files,
            "blob_count": savings.get("total_blobs", 0),
            "stored_bytes": savings.get("stored_bytes", 0),
            "dedup_ratio": savings.get("ratio", 1.0),
            "generation": gen,
            "backends": be_full,
        })

    @app.get("/api/logs")
    async def api_logs(status: str = "", limit: int = 100):
        if status:
            rows = db.execute(
                "SELECT * FROM op_log WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM op_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/report")
    async def api_report():
        report = verify.get_report()
        # B09: per-operation skip/failure counters live in each operation's
        # result payload; op_log is the one place they persist. Surface the
        # latest completed row per op type so callers see the last
        # recompress/rebuild/gc/... counters without re-running anything.
        counters = {}
        for op_type in ("recompress", "rebuild", "rebalance", "gc", "purge", "report"):
            try:
                row = db.execute(
                    "SELECT status, target_json, completed_at FROM op_log "
                    "WHERE op_type = ? AND completed_at IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1", (op_type,)).fetchone()
            except sqlite3.OperationalError as e:
                log.warning("op_log counter query failed for op_type=%s: %s", op_type, e)
                continue
            if row is None:
                continue
            try:
                payload = json.loads(row["target_json"]) if row["target_json"] else {}
            except ValueError as e:
                log.warning("malformed target_json in op_log for op_type=%s: %s", op_type, e)
                payload = {}
            counters[op_type] = {"status": row["status"],
                                 "completed_at": row["completed_at"], **payload}
        report["op_counters"] = counters
        return JSONResponse(report)

    @app.get("/api/search")
    async def api_search(q: str = "", scope: str = ""):
        if not q:
            return JSONResponse([])
        if scope == "content":
            rows = db.execute(
                "SELECT * FROM files WHERE content_text LIKE ? AND deleted_at IS NULL LIMIT 50",
                (f"%{q}%",),
            ).fetchall()
            return JSONResponse([dict(r) for r in rows])
        # Quoted-phrase MATCH + LIKE fallback: raw user input must never
        # surface as a 500 via an fts5 syntax error (qa defect D3).
        try:
            rows = db.execute(
                "SELECT f.* FROM files f INNER JOIN files_fts ON f.id = files_fts.rowid "
                "WHERE files_fts MATCH ? AND f.deleted_at IS NULL ORDER BY rank LIMIT 50",
                (_fts_phrase(q),),
            ).fetchall()
        except sqlite3.OperationalError:
            return JSONResponse(_search_files_like(q, limit=50))
        return JSONResponse([dict(r) for r in rows])

    @app.post("/api/report/generate")
    async def api_report_generate(backend: str = "", count: int = 10):
        report_key = f"report:{backend}:{int(time.time())}"
        op_id = op_log_start("report", report_key,
                             {"backend": backend, "count": count})
        try:
            check_type = "random"
            if backend:
                check_type = f"backend:{backend}"
            result = verify.run_integrity_check(check_type, count=count, backend_name=backend or None)
            op_log_finish(op_id, "completed", target_json={
                "checked_files": result.get("checked_files", 0),
                "failed_files": result.get("failed_files", 0)})
            return JSONResponse({"status": "ok", **result})
        except Exception as e:
            op_log_finish(op_id, "failed", last_error=str(e))
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/encrypt/init")
    async def api_encrypt_init():
        try:
            key = crypto_mod.generate_key()
            key_path = "backupme.key"
            crypto_mod.save_key(key, key_path)
            return JSONResponse({"status": "ok", "message": f"Encryption key saved to {key_path}"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.get("/api/activity")
    async def api_activity(limit: int = 20):
        rows = db.execute(
            "SELECT * FROM op_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        if not rows:
            files = index_mod.get_all_files(limit=limit)
            return JSONResponse([{
                "op_type": "upload",
                "target_json": json.dumps({"file": f["relative_path"], "size": f["size"]}),
                "status": "completed",
                "started_at": f["created_at"],
            } for f in files])
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/stats/trend")
    async def api_stats_trend(days: int = 7):
        try:
            rows = db.execute(
                "SELECT DATE(started_at) as date, COUNT(*) as count "
                "FROM op_log WHERE started_at IS NOT NULL "
                "GROUP BY DATE(started_at) ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
            result = [{"date": r["date"], "count": r["count"]} for r in rows]
            result.reverse()
            return JSONResponse(result)
        except Exception as e:
            log.warning("activity trend query failed for /api/stats/trend: %s", e)
            return JSONResponse([])

    @app.get("/api/reports")
    async def api_reports(limit: int = 20):
        try:
            rows = db.execute(
                "SELECT * FROM integrity_checks ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return JSONResponse([dict(r) for r in rows])
        except Exception as e:
            log.warning("integrity report list query failed for /api/reports: %s", e)
            return JSONResponse([])

    @app.get("/api/reports/{report_id}")
    async def api_report_detail(report_id: int):
        try:
            row = db.execute("SELECT * FROM integrity_checks WHERE id = ?", (report_id,)).fetchone()
            if not row:
                return JSONResponse({"error": "not found"}, status_code=404)
            r = dict(row)
            if r.get("details"):
                try:
                    import json as _json
                    r["details"] = _json.loads(r["details"])
                except Exception as e:
                    log.warning("malformed details JSON in integrity_checks row id=%s: %s", report_id, e)
            return JSONResponse(r)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/convert/{backend_name}")
    async def api_convert(backend_name: str, prefix: str = "", delete: bool = False, target: str = ""):
        try:
            from backupme.core.converter import convert_backend
            result = convert_backend(
                backend_name=backend_name,
                prefix=prefix,
                target_backend_name=target or None,
                delete=delete,
                resume=True,
            )
            return JSONResponse({"status": "ok", **result})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/recompress")
    async def api_recompress():
        # B06 rework: the per-blob inline loop is gone — core
        # compress.recompress_blobs owns the migration now (write-new →
        # hash-verify → commit → GC-old, encrypted blobs skipped and
        # counted). Defaults match the old inline behavior: zstd level 3,
        # every backend (the endpoint has no backend param today —
        # settings.html POSTs without a body).
        op_id = op_log_start("recompress", f"recompress:{int(time.time())}",
                             {"algo": "zstd", "level": 3})
        try:
            # Warm the backend instance cache: recompress_blobs resolves each
            # row's backend by name through it (same warm-up the CLI does).
            _get_backends(ctx)
            result = compress_mod.recompress_blobs()
            counters = {
                "recompressed": result.get("recompressed", 0),
                "total_scanned": result.get("total_scanned", 0),
                "skipped_encrypted": result.get("skipped_encrypted", 0),
                "failed": result.get("failed", 0),
            }
            op_log_finish(op_id, "completed", target_json=counters)
            # "converted" stays as the legacy alias — settings.html reads it.
            return JSONResponse({"status": "ok", **counters,
                                 "converted": counters["recompressed"]})
        except Exception as e:
            op_log_finish(op_id, "failed", last_error=str(e))
            return JSONResponse({"status": "error", "message": str(e)})

    @app.get("/api/encrypt/status")
    async def api_encrypt_status():
        key_path = Path("backupme.key")
        exists = key_path.exists()
        return JSONResponse({
            "key_exists": exists,
            "key_path": str(key_path),
            "key_size": key_path.stat().st_size if exists else 0,
            # B14: cipher/kdf/file-layout capabilities — lets callers tell the
            # AEAD tag-at-end layout from the legacy tag-after-nonce one.
            "capabilities": crypto_mod.capabilities(),
        })

    @app.get("/api/hash/{sha256}")
    async def api_hash_lookup(sha256: str):
        try:
            rows = db.execute(
                "SELECT f.* FROM files f WHERE f.sha256 = ? AND f.deleted_at IS NULL LIMIT 20",
                (sha256,),
            ).fetchall()
            if not rows:
                rows = db.execute(
                    "SELECT f.* FROM files f WHERE f.sha256 LIKE ? AND f.deleted_at IS NULL LIMIT 20",
                    (f"{sha256}%",),
                ).fetchall()
            return JSONResponse([dict(r) for r in rows])
        except Exception as e:
            log.warning("hash lookup query failed for /api/hash/%s: %s", sha256, e)
            return JSONResponse([])

    @app.get("/api/logs/export")
    async def api_logs_export(fmt: str = "json", limit: int = 500):
        rows = db.execute(
            "SELECT * FROM op_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        logs = [dict(r) for r in rows]
        if fmt == "csv":
            import csv
            buf = io.StringIO()
            if logs:
                writer = csv.DictWriter(buf, fieldnames=logs[0].keys())
                writer.writeheader()
                writer.writerows(logs)
            return StreamingResponse(
                io.BytesIO(buf.getvalue().encode("utf-8")),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="logs.csv"'},
            )
        return StreamingResponse(
            io.BytesIO(json.dumps(logs, indent=2, default=str).encode("utf-8")),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="logs.json"'},
        )

    @app.get("/api/stats/storage-trend")
    async def api_storage_trend(days: int = 30):
        try:
            rows = db.execute(
                "SELECT DATE(started_at) as date, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed, "
                "COUNT(*) as total FROM op_log WHERE started_at IS NOT NULL "
                "GROUP BY DATE(started_at) ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
            result = [{"date": r["date"], "completed": r["completed"], "total": r["total"]} for r in rows]
            result.reverse()
            return JSONResponse(result)
        except Exception as e:
            log.warning("storage trend query failed for /api/stats/storage-trend: %s", e)
            return JSONResponse([])

    @app.get("/api/replication/status")
    async def api_replication_status():
        try:
            rows = db.execute(
                "SELECT bs.blob_id, bs.backend_name, bs.stored_size, b.ref_count "
                "FROM blob_storage bs LEFT JOIN blobs b ON bs.blob_id = b.blob_id "
                "ORDER BY bs.blob_id"
            ).fetchall()
            blob_map = {}
            for r in rows:
                bid = r["blob_id"]
                if bid not in blob_map:
                    blob_map[bid] = {"blob_id": bid, "ref_count": r["ref_count"] or 0, "backends": [], "stored_size": r["stored_size"] or 0}
                blob_map[bid]["backends"].append(r["backend_name"])
            total_blobs = len(blob_map)
            fully_replicated = sum(1 for b in blob_map.values() if len(b["backends"]) >= 2)
            single_copy = sum(1 for b in blob_map.values() if len(b["backends"]) == 1)
            backend_counts = {}
            for b in blob_map.values():
                for be in b["backends"]:
                    backend_counts[be] = backend_counts.get(be, 0) + 1
            return JSONResponse({
                "total_blobs": total_blobs,
                "fully_replicated": fully_replicated,
                "single_copy": single_copy,
                "per_backend": backend_counts,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

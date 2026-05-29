import io
import json
import os
from pathlib import Path

from fastapi import Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from .. import config as cfgmod
from ..core import database as db
from ..core import index as index_mod
from ..core import dedup, op_log, state, verify, snapshot as snapmod
from ..core import uploader as uploader_mod
from ..core import downloader as downloader_mod
from ..core import crypto as crypto_mod
from ..core import recover as recover_mod
from ..core import manifest
from ..backends import get_backend, get_all_backends


def register_routes(app, config: cfgmod.Config):
    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def _get_cfg_dict():
        if hasattr(config, "data"):
            return config.data
        return {}

    def _get_backups():
        return [{"source": b.source, "redundancy": b.redundancy if hasattr(b, "redundancy") else 1}
                for b in getattr(config, "backups", [])]

    # ── Page routes ──────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        total_files = index_mod.file_count()
        savings = dedup.total_dedup_savings()
        rpt = verify.get_report()
        backends = get_all_backends(config)
        be_list = [{"name": b.name, "type": b.type, "connected": b.connected} for b in backends]
        last_cp = state.get_last_checkpoint()
        recent = index_mod.get_all_files(limit=10)
        gen = db.get_generation()
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "total_files": total_files,
            "blob_count": savings.get("total_blobs", 0),
            "stored_bytes": savings.get("stored_bytes", 0),
            "savings_bytes": savings.get("savings_bytes", 0),
            "ratio": savings.get("ratio", 1.0),
            "backends": be_list,
            "generation": gen,
            "last_checkpoint": last_cp,
            "recent_files": recent,
            "report": rpt,
        })

    @app.get("/files", response_class=HTMLResponse)
    async def files_page(request: Request, q: str = "", backend: str = "", offset: int = 0, limit: int = 50):
        if q:
            files = index_mod.search_files(q)
        else:
            files = index_mod.get_all_files(limit=limit, offset=offset)
        if backend:
            files = [f for f in files if f.get("source_root") == backend]
        total = index_mod.file_count()
        return templates.TemplateResponse("files.html", {
            "request": request,
            "files": files,
            "total": total,
            "q": q, "backend": backend,
            "offset": offset, "limit": limit,
        })

    @app.get("/backends", response_class=HTMLResponse)
    async def backends_page(request: Request):
        backends = get_all_backends(config)
        be_list = []
        for b in backends:
            free = None
            total = None
            try:
                free = b.free_space()
                total = b.total_space()
            except Exception:
                pass
            be_list.append({"name": b.name, "type": b.type, "connected": b.connected,
                           "free_space": free, "total_space": total})
        return templates.TemplateResponse("backends.html", {
            "request": request,
            "backends": be_list,
        })

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, op: str = "", limit: int = 100):
        if op:
            rows = db.execute(
                "SELECT * FROM op_log WHERE op_type = ? ORDER BY id DESC LIMIT ?",
                (op, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM op_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        op_types = db.execute(
            "SELECT DISTINCT op_type FROM op_log ORDER BY op_type"
        ).fetchall()
        return templates.TemplateResponse("logs.html", {
            "request": request,
            "logs": [dict(r) for r in rows],
            "op_types": [r["op_type"] for r in op_types if r["op_type"]],
            "current_op": op,
        })

    @app.get("/database", response_class=HTMLResponse)
    async def database_page(request: Request):
        gen = db.get_generation()
        instance_id = db.get_instance_id()
        integrity = db.check_integrity()
        try:
            wal_row = db.execute("PRAGMA journal_mode").fetchone()
            wal_mode = wal_row and wal_row[0].upper() == "WAL"
        except Exception:
            wal_mode = False
        page_count = db.execute("PRAGMA page_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        recovery = db.execute(
            "SELECT * FROM recovery_events ORDER BY id DESC LIMIT 20"
        ).fetchall()
        return templates.TemplateResponse("database.html", {
            "request": request,
            "db_info": {
                "generation": gen,
                "instance_id": instance_id,
                "size": page_count * page_size,
                "integrity_status": "ok" if not integrity else "corrupt",
                "integrity_errors": integrity,
                "wal_mode": wal_mode,
            },
            "recovery": [dict(r) for r in recovery],
        })

    @app.get("/report", response_class=HTMLResponse)
    async def report_page(request: Request):
        rpt = verify.get_report()
        return templates.TemplateResponse("report.html", {
            "request": request,
            "report": rpt,
        })

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        raw = _get_cfg_dict()
        config_items = []
        for section in ["chunking", "compression", "encryption", "verification", "retention", "database"]:
            items = []
            sec = raw.get(section, {}) if isinstance(raw, dict) else {}
            if isinstance(sec, dict):
                for k, v in sec.items():
                    if isinstance(v, (str, int, float, bool)):
                        items.append((k, str(v)))
                    elif isinstance(v, dict):
                        for sk, sv in v.items():
                            if isinstance(sv, (str, int, float, bool)):
                                items.append((f"{k}.{sk}", str(sv)))
            if items:
                config_items.append((section, items))
        sources = _get_backups()
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "config_items": config_items,
            "sources": sources,
        })

    # ── API: Status ──────────────────────────────────────────────

    @app.get("/api/status")
    async def api_status():
        total_files = index_mod.file_count()
        savings = dedup.total_dedup_savings()
        gen = db.get_generation()
        backends = get_all_backends(config)
        be_full = []
        for b in backends:
            be_full.append({"name": b.name, "type": b.type, "connected": b.connected})
        return JSONResponse({
            "total_files": total_files,
            "blob_count": savings.get("total_blobs", 0),
            "stored_bytes": savings.get("stored_bytes", 0),
            "dedup_ratio": savings.get("ratio", 1.0),
            "generation": gen,
            "backends": be_full,
        })

    @app.get("/api/backends")
    async def api_backends():
        backends = get_all_backends(config)
        result = []
        for b in backends:
            free = None
            total = None
            try:
                free = b.free_space()
                total = b.total_space()
            except Exception:
                pass
            result.append({
                "name": b.name, "type": b.type, "connected": b.connected,
                "free_space": free, "total_space": total,
            })
        return JSONResponse(result)

    @app.get("/api/files")
    async def api_files(q: str = "", backend: str = "", limit: int = 100, offset: int = 0):
        if q:
            all_files = index_mod.search_files(q)
        else:
            all_files = index_mod.get_all_files(limit=limit, offset=offset)
        if backend:
            all_files = [f for f in all_files if f.get("source_root") == backend]
        for f in all_files:
            for k in list(f.keys()):
                if isinstance(f[k], bytes):
                    f[k] = f[k].decode("utf-8", errors="replace")
        return JSONResponse(all_files)

    @app.get("/api/files/{file_id}")
    async def api_file_detail(file_id: int):
        rows = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchall()
        if not rows:
            return JSONResponse({"error": "not found"}, status_code=404)
        f = dict(rows[0])
        chunks = db.execute(
            "SELECT fc.*, bs.backend_name, bs.storage_path, bs.stored_size, bs.stored_sha256, "
            "bs.compression_algo, bs.encrypted "
            "FROM file_chunks fc "
            "LEFT JOIN blob_storage bs ON fc.blob_id = bs.blob_id "
            "WHERE fc.file_id = ? ORDER BY fc.seq",
            (file_id,),
        ).fetchall()
        f["chunks"] = [dict(c) for c in chunks]
        for k in list(f.keys()):
            if isinstance(f[k], bytes):
                f[k] = f[k].decode("utf-8", errors="replace")
        return JSONResponse(f)

    @app.get("/api/sources")
    async def api_sources():
        rows = db.execute(
            "SELECT DISTINCT source_root FROM files WHERE deleted_at IS NULL"
        ).fetchall()
        return JSONResponse([r["source_root"] for r in rows])

    @app.get("/api/logs")
    async def api_logs(level: str = "", limit: int = 100):
        if level:
            rows = db.execute(
                "SELECT * FROM op_log WHERE status = ? ORDER BY id DESC LIMIT ?",
                (level, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM op_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/db/info")
    async def api_db_info():
        gen = db.get_generation()
        instance_id = db.get_instance_id()
        integrity = db.check_integrity()
        page_count = db.execute("PRAGMA page_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        try:
            wal = db.execute("PRAGMA journal_mode").fetchone()
            wal_mode = wal[0].upper() == "WAL" if wal else False
        except Exception:
            wal_mode = False
        return JSONResponse({
            "generation": gen,
            "instance_id": instance_id,
            "size": page_count * page_size,
            "integrity_status": "ok" if not integrity else "corrupt",
            "integrity_errors": integrity,
            "wal_mode": wal_mode,
        })

    @app.get("/api/db/snapshots")
    async def api_db_snapshots():
        snaps = sorted(Path(".").glob("snapshot-*.db.zst"))
        results = []
        for p in snaps:
            results.append({"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
        return JSONResponse(results)

    @app.get("/api/db/recovery")
    async def api_db_recovery():
        rows = db.execute(
            "SELECT * FROM recovery_events ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/report")
    async def api_report():
        return JSONResponse(verify.get_report())

    @app.get("/api/search")
    async def api_search(q: str = "", scope: str = ""):
        if not q:
            return JSONResponse([])
        if scope == "content":
            rows = db.execute(
                "SELECT * FROM files WHERE content_text LIKE ? AND deleted_at IS NULL LIMIT 50",
                (f"%{q}%",),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT f.* FROM files f INNER JOIN files_fts ON f.id = files_fts.rowid "
                "WHERE files_fts MATCH ? AND f.deleted_at IS NULL ORDER BY rank LIMIT 50",
                (q,),
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])

    # ── API: Post actions ────────────────────────────────────────

    @app.post("/api/backup")
    async def api_trigger_backup():
        results = []
        for entry in _get_backups():
            source = entry.get("source", "")
            if not source or not os.path.exists(source):
                results.append({"source": source, "status": "skipped"})
                continue
            backends = get_all_backends(config)
            try:
                if os.path.isfile(source):
                    r = uploader_mod.upload_file(
                        source_root=str(Path(source).parent),
                        relative_path=os.path.basename(source),
                        local_path=source,
                        backends=backends,
                        config=config,
                    )
                    results.append({"source": source, **r})
                elif os.path.isdir(source):
                    count = 0
                    for root, _dirs, files in os.walk(source):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            rel = os.path.relpath(fp, source)
                            uploader_mod.upload_file(
                                source_root=source,
                                relative_path=rel,
                                local_path=fp,
                                backends=backends,
                                config=config,
                            )
                            count += 1
                    results.append({"source": source, "status": "uploaded", "file_count": count})
            except Exception as e:
                results.append({"source": source, "status": "error", "error": str(e)})
        return JSONResponse({"status": "completed", "results": results})

    @app.post("/api/download/{file_id}")
    async def api_download(file_id: int):
        import tempfile
        tmp = Path(tempfile.mkdtemp()) / f"dl_{file_id}"
        try:
            result = downloader_mod.download_file(file_id, str(tmp))
            if result["status"] != "ok":
                return JSONResponse({"error": result.get("message", "download failed")}, status_code=500)
            data = tmp.read_bytes()
            filename = f"file_{file_id}"
            return StreamingResponse(
                io.BytesIO(data),
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            tmp.unlink(missing_ok=True)

    @app.post("/api/db/check")
    async def api_db_check():
        errors = db.check_integrity()
        if errors:
            return JSONResponse({"status": "error", "errors": errors})
        return JSONResponse({"status": "ok"})

    @app.post("/api/db/snapshot")
    async def api_db_snapshot():
        try:
            result = snapmod.create_snapshot()
            return JSONResponse({"status": "ok", **result})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/db/replicate")
    async def api_db_replicate():
        try:
            from ..core.replicator import Replicator
            repl = Replicator(config)
            repl.create_and_upload_snapshot()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/report/generate")
    async def api_report_generate():
        try:
            result = verify.run_integrity_check("random", count=10)
            return JSONResponse({"status": "ok", **result})
        except Exception as e:
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

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})
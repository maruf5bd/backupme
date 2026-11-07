"""HTML page routes (backlog B05).

GET / HTML pages: dashboard, files, backends, ingest, logs, database,
report, settings. JSON APIs live in the sibling domain modules.
"""
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from backupme.core import database as db
from backupme.core import dedup, state, verify
from backupme.core import index as index_mod

from .common import _get_backends, _get_backups, _get_cfg_dict, _search_files_safe


def register(app, ctx):

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        total_files = index_mod.file_count()
        savings = dedup.total_dedup_savings()
        rpt = verify.get_report()
        report_data = rpt.get("last_integrity_check") if rpt else None
        backends = _get_backends(ctx)
        be_list = [{"name": b.name, "type": b.type, "connected": b.connected} for b in backends]
        last_cp = state.get_last_checkpoint()
        recent = index_mod.get_all_files(limit=10)
        gen = db.get_generation()
        try:
            cps = db.execute(
                "SELECT * FROM op_log ORDER BY id DESC LIMIT 5"
            ).fetchall()
            checkpoints = [dict(c) for c in cps]
        except Exception:
            checkpoints = []
        return ctx.templates.TemplateResponse("dashboard.html", {
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
            "report": report_data,
            "checkpoints": checkpoints,
        })

    @app.get("/files", response_class=HTMLResponse)
    async def files_page(request: Request, q: str = "", backend: str = "", source: str = "",
                         offset: int = 0, limit: int = 50, mode: str = ""):
        if q:
            if mode == "content":
                rows = db.execute(
                    "SELECT * FROM files WHERE content_text LIKE ? AND deleted_at IS NULL LIMIT 50",
                    (f"%{q}%",),
                ).fetchall()
                files = [dict(r) for r in rows]
            elif mode == "hash":
                rows = db.execute(
                    "SELECT * FROM files WHERE sha256 = ? AND deleted_at IS NULL LIMIT 20",
                    (q,),
                ).fetchall()
                if not rows:
                    rows = db.execute(
                        "SELECT * FROM files WHERE sha256 LIKE ? AND deleted_at IS NULL LIMIT 20",
                        (f"{q}%",),
                    ).fetchall()
                files = [dict(r) for r in rows]
            else:
                files = _search_files_safe(q, limit=limit)
        else:
            files = index_mod.get_all_files(limit=limit, offset=offset, backend=backend or None)
        if backend and q:
            backend_file_ids = set()
            rows = db.execute(
                "SELECT DISTINCT fc.file_id FROM file_chunks fc "
                "INNER JOIN blob_storage bs ON fc.blob_id = bs.blob_id "
                "WHERE bs.backend_name = ?", (backend,)
            ).fetchall()
            backend_file_ids = {r["file_id"] for r in rows}
            files = [f for f in files if f.get("id") in backend_file_ids]
        if source:
            files = [f for f in files if (f.get("source_root") or "").startswith(source)]
        sources = [r["source_root"] for r in db.execute(
            "SELECT DISTINCT source_root FROM files WHERE deleted_at IS NULL "
            "ORDER BY source_root LIMIT 50").fetchall()]
        total = index_mod.file_count()
        backends = _get_backends(ctx)
        be_list = [{"name": b.name} for b in backends]
        return ctx.templates.TemplateResponse("files.html", {
            "request": request,
            "files": files,
            "total": total,
            "q": q, "backend": backend, "source": source, "mode": mode,
            "offset": offset, "limit": limit,
            "backends": be_list,
            "sources": sources,
        })

    @app.get("/backends", response_class=HTMLResponse)
    async def backends_page(request: Request):
        backends = _get_backends(ctx)
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
                           "free_space": free, "total_space": total,
                           "priority": b.config.get("priority", 50),
                           "quota": b.config.get("quota", None)})
        return ctx.templates.TemplateResponse("backends.html", {
            "request": request,
            "backends": be_list,
        })

    @app.get("/ingest", response_class=HTMLResponse)
    async def ingest_page(request: Request):
        ci = getattr(ctx.config, "cpanel_ingest", None)
        ci_dict = {
            "enabled": getattr(ci, "enabled", False),
            "drop_root": getattr(ci, "drop_root", ""),
            "staging_dir": getattr(ci, "staging_dir", "staging"),
            "staging_budget_gb": getattr(ci, "staging_budget_gb", 10.0),
            "keep_raw": getattr(ci, "keep_raw", 0),
        }
        return ctx.templates.TemplateResponse("ingest.html", {
            "request": request,
            "ci": ci_dict,
        })

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, op: str = "", status: str = "", limit: int = 100):
        conditions = []
        params = []
        if op:
            conditions.append("op_type = ?")
            params.append(op)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        rows = db.execute(
            f"SELECT * FROM op_log {where} ORDER BY id DESC LIMIT ?", (*params, limit)
        ).fetchall()
        op_types = db.execute(
            "SELECT DISTINCT op_type FROM op_log ORDER BY op_type"
        ).fetchall()
        return ctx.templates.TemplateResponse("logs.html", {
            "request": request,
            "logs": [dict(r) for r in rows],
            "op_types": [r["op_type"] for r in op_types if r["op_type"]],
            "current_op": op,
            "current_status": status,
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
        snaps = sorted(Path(".").glob("snapshot-*.db.zst"))
        snapshots = [{"name": p.name, "size": p.stat().st_size} for p in snaps]
        return ctx.templates.TemplateResponse("database.html", {
            "request": request,
            "db_info": {
                "generation": gen,
                "instance_id": instance_id,
                "size": page_count * page_size,
                "integrity_status": "ok" if not integrity else "corrupt",
                "integrity_errors": integrity,
                "wal_mode": wal_mode,
                "page_count": page_count,
                "page_size": page_size,
            },
            "snapshots": snapshots,
            "recovery": [dict(r) for r in recovery],
        })

    @app.get("/report", response_class=HTMLResponse)
    async def report_page(request: Request):
        rpt = verify.get_report()
        report_data = rpt.get("last_integrity_check") if rpt else None
        return ctx.templates.TemplateResponse("report.html", {
            "request": request,
            "report": report_data,
        })

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        raw = _get_cfg_dict(ctx)
        config_items = []
        for section in ["chunking", "compression", "encryption", "verification", "retention", "database",
                        "db_lifecycle", "scheduler", "cpanel_ingest"]:
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
        sources = _get_backups(ctx)
        return ctx.templates.TemplateResponse("settings.html", {
            "request": request,
            "config_items": config_items,
            "sources": sources,
        })

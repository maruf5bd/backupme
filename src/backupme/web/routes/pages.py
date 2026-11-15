"""HTML page routes (backlog B05).

GET / HTML pages: dashboard, files, backends, ingest, logs, database,
report, settings. JSON APIs live in the sibling domain modules.
"""
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from backupme.core import database as db
from backupme.core import state, verify
from backupme.core import index as index_mod
from backupme.log import get_logger

from .common import (_get_backups, _get_cfg_dict,
                     _search_files_safe, _list_backend_configs,
                     _cached_dedup_savings)
# Compatibility monkeypatch surface (same pattern as routes/__init__.py's
# recover_mod re-export): no page handler below calls _get_backends anymore
# (Phase C perf A1 moved /files and /backends onto the config-only listing),
# but tests/test_web_perf_pages.py patches this module attribute to prove
# the pages never resolve backends on the request path — the attribute must
# keep existing for the patch to arm.
from .common import _get_backends

log = get_logger("web.routes.pages")


# ── Presentation filters (UI polish only — no route or API shape changes) ──
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def _filesize_filter(value):
    """Human-readable byte size ('1.2 GB'). Falsy values render as '0 B';
    non-numeric input passes through unchanged so a bad value can never
    crash the page or render 'None B'."""
    if value in (None, ""):
        return "0 B"
    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value)
    if size <= 0:
        return "0 B"
    unit_idx = 0
    scaled = size
    while scaled >= 1024.0 and unit_idx < len(_SIZE_UNITS) - 1:
        scaled /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(scaled)} B"
    return f"{scaled:.1f} {_SIZE_UNITS[unit_idx]}"


def _dtime_filter(value):
    """Compact human timestamp ('May 30, 09:44'); the year is included when
    it differs from the current year. Empty input renders as '' (callers
    keep their own 'never'/'unknown' fallbacks); unparseable text passes
    through unchanged."""
    if not value:
        return ""
    if isinstance(value, datetime):
        stamp = value
    else:
        text = str(value).strip()
        try:
            stamp = datetime.fromisoformat(text.replace("T", " "))
        except ValueError:
            return text
    now = datetime.now()
    if stamp.year != now.year:
        return stamp.strftime("%b %d, %Y %H:%M")
    return stamp.strftime("%b %d, %H:%M")


def _nfmt_filter(value):
    """Thousands-separated number for server-rendered stat values.
    Mirrors App.formatNumber so the pre-poll paint matches the polled one."""
    if value is None or value == "":
        return "0"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        try:
            return f"{float(value):,.0f}"
        except (TypeError, ValueError):
            return str(value)


def register(app, ctx):
    # Attach the presentation filters to the shared Jinja environment.
    # pages.register runs first in routes.register_routes, so every template
    # rendered later can use them; setdefault keeps any existing filter.
    env = ctx.templates.env
    env.filters.setdefault("filesize", _filesize_filter)
    env.filters.setdefault("dtime", _dtime_filter)
    env.filters.setdefault("nfmt", _nfmt_filter)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        """Dashboard page — cheap-only request path (Phase A page-perf).

        Nothing here connects a backend or runs a heavy aggregate: backend
        cards render as skeletons from the config-only listing and fill
        asynchronously from /api/backends/status; the dedup-savings
        aggregate is served from a TTL cache on a worker thread; the
        integrity card reads the stored integrity_checks row instead of
        verify.get_report() (whose unrecoverable-files LEFT JOIN over
        file_chunks is minutes-class on the live store)."""
        total_files = index_mod.file_count()
        savings = await _cached_dedup_savings()
        # The integrity card only needs the newest integrity_checks row —
        # same dict shape verify.get_report() exposed as
        # last_integrity_check (status/completed_at/started_at), without
        # the report's full-table scans.
        try:
            row = db.execute(
                "SELECT * FROM integrity_checks ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()
            report_data = dict(row) if row else None
        except Exception as e:
            log.warning("last integrity_checks row query failed for dashboard page: %s", e)
            report_data = None
        # Config-only listing: name/type for the skeleton cards, no connects.
        be_list = _list_backend_configs(ctx)
        last_cp = state.get_last_checkpoint()
        # Recent files by id DESC: the rowid index answers instantly even on
        # millions of rows (created_at has no index — an ORDER BY created_at
        # sort measured 46 ms at 300k rows vs ~0 ms by id).
        recent = [dict(r) for r in db.execute(
            "SELECT * FROM files WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 10"
        ).fetchall()]
        gen = db.get_generation()
        try:
            cps = db.execute(
                "SELECT * FROM op_log ORDER BY id DESC LIMIT 5"
            ).fetchall()
            checkpoints = [dict(c) for c in cps]
        except Exception as e:
            log.warning("recent checkpoints query failed for dashboard page: %s", e)
            checkpoints = []
        return ctx.templates.TemplateResponse(request, "dashboard.html", {
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
        # Config-only listing (Phase C perf A1): the files page only needs
        # backend NAMES for its filter/upload dropdowns — no connects, no
        # probes on the request path.
        be_list = [{"name": b["name"]} for b in _list_backend_configs(ctx)]
        return ctx.templates.TemplateResponse(request, "files.html", {
            "files": files,
            "total": total,
            "q": q, "backend": backend, "source": source, "mode": mode,
            "offset": offset, "limit": limit,
            "backends": be_list,
            "sources": sources,
        })

    @app.get("/backends", response_class=HTMLResponse)
    async def backends_page(request: Request):
        """Backends page — cheap-only request path (Phase C perf A1).

        Cards render as config-only skeletons (name/type/priority/quota +
        the speed-test opt-out); connection state and free/total capacity
        fill asynchronously from GET /api/backends/status via JS, exactly
        like the dashboard's backend cards since Phase A. A dead SFTP
        backend can no longer hold this page render hostage."""
        be_list = _list_backend_configs(ctx)
        return ctx.templates.TemplateResponse(request, "backends.html", {
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
        return ctx.templates.TemplateResponse(request, "ingest.html", {
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
        return ctx.templates.TemplateResponse(request, "logs.html", {
            "logs": [dict(r) for r in rows],
            "op_types": [r["op_type"] for r in op_types if r["op_type"]],
            "current_op": op,
            "current_status": status,
        })

    def _collect_database_page_info() -> dict:
        """Cheap-only snapshot behind the /database page (Phase A page-perf).

        The full PRAGMA integrity_check (29s+ on the live DB, >120s wall on
        the whole page) left the request path: it now runs as a background
        op (POST /api/db/check/start) that persists its result as a 'pragma'
        row in integrity_checks. The page renders from the cheap PRAGMAs
        (generation / instance_id / page_count*page_size / journal_mode),
        the newest integrity_checks row as "last integrity check", the
        recovery-events tail and the snapshot directory glob+stat. This
        worker thread always closes its own SQLite handle so no pooled
        connection outlives the request."""
        try:
            gen = db.get_generation()
            instance_id = db.get_instance_id()
            try:
                wal_row = db.execute("PRAGMA journal_mode").fetchone()
                wal_mode = bool(wal_row and wal_row[0].upper() == "WAL")
            except Exception as e:
                log.warning("journal_mode PRAGMA read failed for database page: %s", e)
                wal_mode = False
            page_count = db.execute("PRAGMA page_count").fetchone()[0]
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            try:
                row = db.execute(
                    "SELECT * FROM integrity_checks ORDER BY started_at DESC, id DESC LIMIT 1"
                ).fetchone()
                last_check = dict(row) if row else None
            except Exception as e:
                log.warning("last integrity_checks row query failed for database page: %s", e)
                last_check = None
            recovery = db.execute(
                "SELECT * FROM recovery_events ORDER BY id DESC LIMIT 20"
            ).fetchall()
            snaps = sorted(Path(".").glob("snapshot-*.db.zst"))
            snapshots = [{"name": p.name, "size": p.stat().st_size} for p in snaps]
            return {
                "db_info": {
                    "generation": gen,
                    "instance_id": instance_id,
                    "size": page_count * page_size,
                    "wal_mode": wal_mode,
                    "page_count": page_count,
                    "page_size": page_size,
                },
                "last_check": last_check,
                "snapshots": snapshots,
                "recovery": [dict(r) for r in recovery],
            }
        finally:
            db.close_connection()

    @app.get("/database", response_class=HTMLResponse)
    async def database_page(request: Request):
        info = await asyncio.to_thread(_collect_database_page_info)
        return ctx.templates.TemplateResponse(request, "database.html", {
            **info,
        })

    @app.get("/report", response_class=HTMLResponse)
    async def report_page(request: Request):
        rpt = verify.get_report()
        report_data = rpt.get("last_integrity_check") if rpt else None
        return ctx.templates.TemplateResponse(request, "report.html", {
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
        return ctx.templates.TemplateResponse(request, "settings.html", {
            "config_items": config_items,
            "sources": sources,
        })

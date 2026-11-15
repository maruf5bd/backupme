"""cPanel ingest routes (backlog B05): status, scan, pause/resume, SSE stream,
generations, history templates, ingest trend, restore-generation.

The ingest worker writes progress into the shared _ingest_state dict from
routes/common.py. The pause Event is the single source of truth for "paused"
and is injected into every SSE snapshot (never stored in the state dict —
a stale copy would mislead consumers mid-pause).
"""
import json
import os
import time
from pathlib import Path

from fastapi import Body
from fastapi.responses import JSONResponse

from backupme.core import database as db
from backupme.core import state
from backupme.log import get_logger

from .common import (_get_backends, _config_as_dict,
                     _ingest_state, _ingest_lock, _ingest_paused, _reset_ingest,
                     mark_op_started,
                     make_sse_stream, start_daemon_thread)

log = get_logger("web.routes.ingest")


def register(app, ctx):

    def _ingest_progress_cb(member, done, total):
        with _ingest_lock:
            _ingest_state["current_member"] = member
            _ingest_state["members_done"] = done
            _ingest_state["members_total"] = total

    def _ingest_worker(drop_root: str):
        try:
            from backupme.core import cpanel_ingest as ci_mod
            cfg_dict = _config_as_dict(ctx)
            with _ingest_lock:
                _ingest_state["running"] = True
                _ingest_state["started_at"] = time.time()
                _ingest_state["drop_root"] = drop_root
            backends = _get_backends(ctx)
            discovered = ci_mod.discover(drop_root)
            archives = [d for d in discovered if d["type"] == "archive"]
            with _ingest_lock:
                _ingest_state["archives_total"] = len(archives)
            results = []
            for item in archives:
                if _ingest_paused.is_set():
                    with _ingest_lock:
                        _ingest_state["results"].append({
                            "archive": item["path"], "status": "deferred",
                            "reason": "paused"})
                        # Deferred archives count as handled so the queue
                        # counters reconcile (done == total when a pause
                        # deferred the tail of the queue).
                        _ingest_state["archives_done"] += 1
                    continue
                with _ingest_lock:
                    _ingest_state["current_archive"] = item["path"]
                try:
                    est = ci_mod.estimate_archive(item["path"])
                    with _ingest_lock:
                        _ingest_state["members_total"] = est["members"]
                        _ingest_state["members_done"] = 0
                except Exception as e:
                    log.warning("archive size estimate failed for %s: %s", item["path"], e)
                try:
                    r = ci_mod.ingest_archive(
                        item["path"], item["account"], backends, cfg_dict,
                        progress_cb=_ingest_progress_cb,
                        drop_root=drop_root)
                except Exception as e:
                    r = {"status": "failed", "error": str(e),
                         "archive": item["path"], "account": item["account"]}
                with _ingest_lock:
                    _ingest_state["results"].append(r)
                    _ingest_state["archives_done"] += 1
                    if r.get("status") == "failed":
                        _ingest_state["errors"].append(
                            f"{item['path']}: {r.get('error', 'failed')}")
        except Exception as e:
            with _ingest_lock:
                _ingest_state["errors"].append(str(e))
        finally:
            with _ingest_lock:
                _ingest_state["running"] = False
                _ingest_state["completed"] = True
                _ingest_state["current_archive"] = ""
                _ingest_state["current_member"] = ""
            # Release this worker's SQLite handle before the thread returns.
            # The handle is per-thread (threading.local), so it stays open
            # after the worker finishes and keeps the DB file locked — on
            # Windows that is a real lock (WinError 32) and breaks teardown
            # of any test that reopens the same DB file.
            db.close_connection()

    @app.get("/api/ingest/status")
    async def api_ingest_status():
        ci = getattr(ctx.config, "cpanel_ingest", None)
        staging_dir = Path(getattr(ci, "staging_dir", "staging") or "staging")
        staging_bytes = 0
        if staging_dir.exists():
            for dirpath, _dirs, filenames in os.walk(staging_dir):
                for fn in filenames:
                    try:
                        staging_bytes += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        # Expected control flow, not a failure: staging files
                        # can vanish between the walk and the stat (staging
                        # cleanup runs concurrently), skipping them is normal.
                        pass
        db_path = Path(str(db.DB_PATH))
        db_size = db_path.stat().st_size if db_path.exists() else 0

        from backupme.core import db_lifecycle as lc
        pdir = lc.parquet_dir_for(_config_as_dict(ctx))
        partitions = []
        for p in sorted(pdir.glob("files_*.parquet")):
            partitions.append({"file": p.name, "bytes": p.stat().st_size})
        accounts = [dict(r) for r in db.execute(
            "SELECT source_root, COUNT(*) AS files, SUM(size) AS bytes "
            "FROM files WHERE source_root LIKE 'cpanel/%' AND deleted_at IS NULL "
            "GROUP BY source_root ORDER BY source_root").fetchall()]
        last_cp = state.get_last_checkpoint()
        with _ingest_lock:
            ingest_snap = dict(_ingest_state)
        return JSONResponse({
            "enabled": getattr(ci, "enabled", False),
            "drop_root": getattr(ci, "drop_root", ""),
            "keep_raw": getattr(ci, "keep_raw", 0),
            "staging": {"dir": str(staging_dir), "bytes": staging_bytes,
                        "budget_gb": getattr(ci, "staging_budget_gb", 10.0)},
            "sqlite": {"size": db_size,
                       "files": db.execute("SELECT COUNT(*) FROM files WHERE deleted_at IS NULL").fetchone()[0],
                       "chunks": db.execute("SELECT COUNT(*) FROM file_chunks").fetchone()[0]},
            "parquet": {"dir": str(pdir), "partitions": partitions},
            "accounts": accounts,
            "checkpoint": last_cp,
            "running": ingest_snap["running"],
            "paused": _ingest_paused.is_set(),
            "queue": {"archives_total": ingest_snap["archives_total"],
                      "archives_done": ingest_snap["archives_done"],
                      "members_done": ingest_snap["members_done"],
                      "members_total": ingest_snap["members_total"],
                      "current_archive": ingest_snap["current_archive"],
                      "current_member": ingest_snap["current_member"]},
            "errors": ingest_snap["errors"][-10:],
        })

    @app.post("/api/ingest/scan")
    async def api_ingest_scan(data: dict = Body(default={})):
        ci = getattr(ctx.config, "cpanel_ingest", None)
        drop_root = (data.get("drop_root") or getattr(ci, "drop_root", "") or "").strip()
        if not drop_root:
            return JSONResponse({"error": "drop_root not configured"}, status_code=400)
        if not os.path.isdir(drop_root):
            return JSONResponse({"error": f"drop root not found: {drop_root}"}, status_code=404)
        with _ingest_lock:
            if _ingest_state["running"]:
                return JSONResponse({"error": "an ingest scan is already running"}, status_code=409)
        if _ingest_paused.is_set():
            # A pause is explicit operator consent to NOT ingest; a scan must
            # not silently clear it (bandwidth/egress protection control).
            return JSONResponse(
                {"error": "ingest is paused — POST /api/ingest/resume to allow scans"},
                status_code=409)
        _reset_ingest()
        # Op actually launched: its SSE stream polls to completion instead
        # of closing after one idle snapshot.
        mark_op_started(_ingest_state)
        start_daemon_thread(_ingest_worker, drop_root)
        return JSONResponse({"status": "started", "drop_root": drop_root})

    @app.post("/api/ingest/pause")
    async def api_ingest_pause():
        _ingest_paused.set()
        return JSONResponse({"status": "paused"})

    @app.post("/api/ingest/resume")
    async def api_ingest_resume():
        _ingest_paused.clear()
        return JSONResponse({"status": "resumed"})

    @app.get("/api/ingest/stream")
    async def api_ingest_stream():
        # The pause Event is the single source of truth; the state dict never
        # stores it (a stale copy would mislead SSE consumers into reading
        # "paused": false mid-pause). Injected into every SSE snapshot.
        def _ingest_snapshot():
            with _ingest_lock:
                snap = dict(_ingest_state)
            snap["paused"] = _ingest_paused.is_set()
            return json.dumps(snap)

        return make_sse_stream(_ingest_state, _ingest_lock,
                               snapshot_fn=_ingest_snapshot)

    @app.get("/api/ingest/generations")
    async def api_ingest_generations(account: str = ""):
        if not account:
            return JSONResponse({"error": "account required"}, status_code=400)
        rows = db.execute(
            "SELECT version_seq AS generation, COUNT(*) AS members, SUM(size) AS bytes, "
            "MIN(created_at) AS created, MAX(updated_at) AS updated "
            "FROM files WHERE source_root = ? AND deleted_at IS NULL "
            "GROUP BY version_seq ORDER BY version_seq DESC LIMIT 100",
            (f"cpanel/{account}",),
        ).fetchall()
        return JSONResponse({"account": account,
                             "generations": [dict(r) for r in rows]})

    @app.get("/api/ingest/history")
    async def api_ingest_history(template: str = "member_history",
                                 account: str = "", limit: int = 100):
        from backupme.core import db_lifecycle as lc
        limit = max(1, min(int(limit), 500))
        templates_sql = {
            "member_history": (
                "SELECT relative_path, version_seq, size, sha256, created_at, deleted_at "
                "FROM files WHERE source_root LIKE ? ORDER BY relative_path, version_seq DESC "
                f"LIMIT {limit}"),
            "account_bytes": (
                "SELECT substr(created_at, 1, 7) AS month, source_root, "
                "COUNT(*) AS files, SUM(size) AS bytes FROM files "
                "WHERE source_root LIKE ? GROUP BY month, source_root ORDER BY month DESC, source_root "
                f"LIMIT {limit}"),
            "recent": (
                "SELECT source_root, relative_path, version_seq, size, created_at "
                "FROM files ORDER BY created_at DESC, id DESC "
                f"LIMIT {limit}"),
        }
        sql = templates_sql.get(template)
        if sql is None:
            return JSONResponse({"error": f"unknown template: {template}"}, status_code=400)
        params = []
        if template == "member_history":
            params.append(f"cpanel/{account}%" if account else "cpanel/%")
        elif template == "account_bytes":
            params.append(f"cpanel/{account}%" if account else "cpanel/%")
        if params:
            rows = lc.query_history_duckdb(sql, params)
        else:
            rows = lc.query_history_duckdb(sql)
        if rows and rows[0].get("status") in ("error", "unavailable"):
            return JSONResponse(rows[0], status_code=503)
        return JSONResponse({"template": template, "rows": rows, "count": len(rows)})

    @app.get("/api/stats/ingest-trend")
    async def api_stats_ingest_trend(days: int = 30):
        try:
            rows = db.execute(
                "SELECT DATE(started_at) AS date, COUNT(*) AS archives, "
                "SUM(CAST(json_extract(target_json, '$.sha256') IS NOT NULL AS INTEGER)) AS archived "
                "FROM op_log WHERE op_type = 'cpanel_ingest' AND started_at IS NOT NULL "
                "AND started_at >= datetime('now', ?) "
                "GROUP BY DATE(started_at) ORDER BY date",
                (f"-{int(days)} days",),
            ).fetchall()
            return JSONResponse([dict(r) for r in rows])
        except Exception as e:
            log.warning("ingest trend query failed for /api/stats/ingest-trend: %s", e)
            return JSONResponse([])

    @app.post("/api/ingest/restore-generation")
    async def api_ingest_restore_generation(data: dict = Body(...)):
        account = data.get("account", "")
        try:
            generation = int(data.get("generation", 0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "generation must be an integer"}, status_code=400)
        if not account or generation <= 0:
            return JSONResponse({"error": "account and generation required"}, status_code=400)
        out = data.get("out") or f"restore_out/{account}"
        member_filter = data.get("member_filter") or None
        retar = data.get("retar") or None
        try:
            from backupme.core import cpanel_ingest as ci_mod
            result = ci_mod.restore_cpanel(
                account, generation, member_filter=member_filter, out=out,
                retar=retar,
                backends=_get_backends(ctx), config=_config_as_dict(ctx))
            if result.get("status") == "error":
                return JSONResponse(result, status_code=404)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

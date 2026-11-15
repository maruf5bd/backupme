"""Database operation routes (backlog B05): info, snapshots (list/download/
delete/prune with confirm gates), recovery events, check (blocking +
background start/status/stream), snapshot, replicate, restore/rebuild
(confirm gates), checkpoint.

Snapshot delete/prune and db restore/rebuild are destructive and require
``confirm: true`` in the body (backlog B03).

Phase A page-perf: the PRAGMA integrity_check moved out of every
synchronous path — /api/db/info reads the newest integrity_checks row
instead of re-running the PRAGMA, and the full check runs as a background
op (POST /api/db/check/start -> /api/db/check/status | /api/db/check/stream)
that persists its result as a 'pragma' row in integrity_checks plus an
op_log audit entry. The old blocking POST /api/db/check keeps its exact
response shapes (pinned by tests/test_web_d18b.py).
"""
import asyncio
import json
import time
from pathlib import Path

from fastapi import Body
from fastapi.responses import JSONResponse, FileResponse

from backupme.core import database as db
from backupme.core import recover as recover_mod
from backupme.core import snapshot as snapmod
from backupme.log import get_logger

from .common import (_config_as_dict, _get_backends, op_log_start, op_log_finish,
                     _db_check_state, _db_check_lock, mark_op_started,
                     make_sse_stream, start_daemon_thread)

log = get_logger("web.routes.dbops")


def _last_integrity_row() -> dict | None:
    """Newest integrity_checks row (any check_type) with a parsed errors
    list — the shared "last integrity check" source for /api/db/info and
    the /database page."""
    row = db.execute(
        "SELECT * FROM integrity_checks ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    details = result.get("details")
    parsed: list = []
    if details:
        try:
            loaded = json.loads(details)
            if isinstance(loaded, list):
                parsed = loaded
            else:
                parsed = [str(loaded)]
        except (TypeError, ValueError):
            parsed = [str(details)]
    # verify-type rows store per-file dicts; pragma rows store plain strings.
    result["errors"] = [e if isinstance(e, str) else json.dumps(e) for e in parsed]
    return result


def register(app, ctx):

    @app.get("/api/db/info")
    async def api_db_info():
        def _run_info():
            # Phase A page-perf: the PRAGMA integrity_check (29s class on the
            # live DB, qa defect D18) left this endpoint — integrity now
            # comes from the newest integrity_checks row (written by the
            # background check op / verify runs). The executor thread always
            # drops its own SQLite handle so no pooled connection outlives
            # the request.
            try:
                gen = db.get_generation()
                instance_id = db.get_instance_id()
                last_check = _last_integrity_row()
                page_count = db.execute("PRAGMA page_count").fetchone()[0]
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                try:
                    wal = db.execute("PRAGMA journal_mode").fetchone()
                    wal_mode = wal[0].upper() == "WAL" if wal else False
                except Exception as e:
                    log.warning("journal_mode PRAGMA read failed for /api/db/info: %s", e)
                    wal_mode = False
                if last_check is None:
                    integrity_status = "unknown"
                    integrity_errors = []
                    last_check_at = None
                else:
                    integrity_status = ("ok" if last_check.get("status") == "completed"
                                        else "corrupt")
                    integrity_errors = last_check.get("errors", [])
                    last_check_at = last_check.get("completed_at") or last_check.get("started_at")
                return {
                    "generation": gen,
                    "instance_id": instance_id,
                    "size": page_count * page_size,
                    "page_count": page_count,
                    "page_size": page_size,
                    "integrity_status": integrity_status,
                    "integrity_errors": integrity_errors,
                    "last_check_at": last_check_at,
                    "wal_mode": wal_mode,
                }
            finally:
                db.close_connection()

        return JSONResponse(await asyncio.to_thread(_run_info))

    @app.get("/api/db/snapshots")
    async def api_db_snapshots():
        snaps = sorted(Path(".").glob("snapshot-*.db.zst"))
        results = []
        for p in snaps:
            st = p.stat()
            results.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
        return JSONResponse(results)

    @app.get("/api/db/recovery")
    async def api_db_recovery():
        rows = db.execute(
            "SELECT * FROM recovery_events ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return JSONResponse([dict(r) for r in rows])

    @app.post("/api/db/check")
    async def api_db_check():
        def _run_check():
            # PRAGMA integrity_check is the 29-second blocking class on the
            # live DB — keep it off the event loop (qa defect D18). The
            # executor thread always drops its own SQLite handle before
            # returning so no pooled connection outlives the request.
            try:
                return db.check_integrity()
            finally:
                db.close_connection()

        errors = await asyncio.to_thread(_run_check)
        if errors:
            return JSONResponse({"status": "error", "errors": errors})
        return JSONResponse({"status": "ok"})

    # ── Background integrity check (Phase A page-perf) ───────────
    # The dashboard page launches the PRAGMA check here and follows the
    # progress on the SSE stream; the result is persisted as a 'pragma' row
    # in integrity_checks (so the page can show "last integrity check")
    # and audited in op_log like every other background op.

    def _db_check_worker(op_id: int | None):
        try:
            def _run_check():
                try:
                    return db.check_integrity()
                finally:
                    db.close_connection()

            errors = _run_check()
            try:
                db.execute(
                    "INSERT INTO integrity_checks (check_type, status, total_files, "
                    "checked_files, failed_files, completed_at, details) "
                    "VALUES ('pragma', ?, 0, 0, ?, datetime('now'), ?)",
                    ("completed" if not errors else "failed", len(errors),
                     json.dumps(errors)),
                )
                db.commit()
            except Exception:
                log.exception("failed to persist the pragma integrity_check result")
            final_status = "ok" if not errors else "corrupt"
            with _db_check_lock:
                _db_check_state.update({
                    "status": final_status,
                    "errors": errors,
                    "error": "",
                    "finished_at": time.time(),
                })
            op_log_finish(op_id, "completed",
                          target_json={"status": final_status, "errors": errors})
        except Exception as e:
            with _db_check_lock:
                _db_check_state.update({
                    "status": "error",
                    "error": str(e),
                    "finished_at": time.time(),
                })
            op_log_finish(op_id, "failed", last_error=str(e))
        finally:
            with _db_check_lock:
                _db_check_state["running"] = False
                _db_check_state["completed"] = True

    @app.post("/api/db/check/start")
    async def api_db_check_start():
        with _db_check_lock:
            if _db_check_state["running"]:
                return JSONResponse(
                    {"status": "busy",
                     "message": "an integrity check is already running"},
                    status_code=409)
            _db_check_state.update({
                "running": True, "started_at": time.time(), "finished_at": 0.0,
                "status": "", "errors": [], "error": "", "completed": False,
            })
        mark_op_started(_db_check_state)
        op_id = op_log_start("db_check", f"db_check:{int(time.time())}", {})
        start_daemon_thread(_db_check_worker, op_id)
        return JSONResponse({"status": "started"})

    @app.get("/api/db/check/status")
    async def api_db_check_status():
        with _db_check_lock:
            snapshot = dict(_db_check_state)
        snapshot["errors"] = snapshot.get("errors", [])[-10:]
        return JSONResponse(snapshot)

    @app.get("/api/db/check/stream")
    async def api_db_check_stream():
        return make_sse_stream(_db_check_state, _db_check_lock)

    @app.post("/api/db/snapshot")
    async def api_db_snapshot():
        import time
        snap_key = f"snapshot:{int(time.time())}"
        op_id = op_log_start("snapshot", snap_key, {})
        try:
            def _run_snapshot():
                # The sqlite backup (VACUUM INTO) + zstd compression take
                # seconds on a large DB — run the snapshot off the event
                # loop (qa defect D18). The op_log writes stay inline (fast
                # single inserts) and the executor thread always drops its
                # own SQLite handle before returning.
                try:
                    return snapmod.create_snapshot()
                finally:
                    db.close_connection()

            result = await asyncio.to_thread(_run_snapshot)
            op_log_finish(op_id, "completed", target_json=result)
            return JSONResponse({"status": "ok", **result})
        except Exception as e:
            op_log_finish(op_id, "failed", last_error=str(e))
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/db/replicate")
    async def api_db_replicate():
        try:
            from backupme.core.replicator import Replicator
            repl = Replicator(_config_as_dict(ctx))
            repl.create_and_upload_snapshot()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/db/restore")
    async def api_db_restore(data: dict = Body(default={})):
        if data.get("confirm") is not True:
            return JSONResponse(
                {"error": "confirm must be exactly true"}, status_code=400)
        try:
            result = recover_mod.recover_database(_get_backends(ctx))
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/db/rebuild")
    async def api_db_rebuild(data: dict = Body(default={})):
        if data.get("confirm") is not True:
            return JSONResponse(
                {"error": "confirm must be exactly true"}, status_code=400)
        try:
            result = recover_mod.rebuild_from_manifests()
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/db/checkpoint")
    async def api_db_checkpoint():
        try:
            db.wal_checkpoint()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.get("/api/db/snapshots/{name}/download")
    async def api_snapshot_download(name: str):
        p = Path(name)
        if not p.exists() or not p.name.startswith("snapshot-"):
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(
            path=str(p),
            filename=p.name,
            media_type="application/octet-stream",
        )

    @app.delete("/api/db/snapshots/{name}")
    async def api_snapshot_delete(name: str, data: dict = Body(default={})):
        if data.get("confirm") is not True:
            return JSONResponse(
                {"error": "confirm must be exactly true"}, status_code=400)
        try:
            p = Path(name)
            if not p.exists() or not p.name.startswith("snapshot-"):
                return JSONResponse({"status": "error", "message": "Snapshot not found"})
            p.unlink(missing_ok=True)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/db/snapshots/prune")
    async def api_snapshot_prune(keep: int = 5, data: dict = Body(default={})):
        if data.get("confirm") is not True:
            return JSONResponse(
                {"error": "confirm must be exactly true"}, status_code=400)
        try:
            snaps = sorted(Path(".").glob("snapshot-*.db.zst"), key=lambda p: p.stat().st_mtime, reverse=True)
            deleted = 0
            for p in snaps[keep:]:
                p.unlink(missing_ok=True)
                deleted += 1
            return JSONResponse({"status": "ok", "deleted": deleted, "kept": min(len(snaps), keep)})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

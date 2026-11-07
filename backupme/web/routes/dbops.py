"""Database operation routes (backlog B05): info, snapshots (list/download/
delete/prune with confirm gates), recovery events, check, snapshot, replicate,
restore/rebuild (confirm gates), checkpoint.

Snapshot delete/prune and db restore/rebuild are destructive and require
``confirm: true`` in the body (backlog B03).
"""
from pathlib import Path

from fastapi import Body
from fastapi.responses import JSONResponse, FileResponse

from backupme.core import database as db
from backupme.core import recover as recover_mod
from backupme.core import snapshot as snapmod
from backupme.log import get_logger

from .common import _config_as_dict, _get_backends, op_log_start, op_log_finish

log = get_logger("web.routes.dbops")


def register(app, ctx):

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
        except Exception as e:
            log.warning("journal_mode PRAGMA read failed for /api/db/info: %s", e)
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
        errors = db.check_integrity()
        if errors:
            return JSONResponse({"status": "error", "errors": errors})
        return JSONResponse({"status": "ok"})

    @app.post("/api/db/snapshot")
    async def api_db_snapshot():
        import time
        snap_key = f"snapshot:{int(time.time())}"
        op_id = op_log_start("snapshot", snap_key, {})
        try:
            result = snapmod.create_snapshot()
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

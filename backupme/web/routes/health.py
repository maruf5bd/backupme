"""Data-health routes (backlog B05): the /health dashboard page, the
/api/health/* JSON family, the physical reachability scan (start + SSE) and
the confirm-gated purge.

The scan worker writes into the shared _health_scan_state dict from
routes/common.py; per-backend errors are reported inside their result dicts,
worker-level errors in the state's errors list.
"""
import asyncio
import json
import os
import sys
import threading
import time

from fastapi import Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from backupme.core import data_health
from backupme.core import database as db
from backupme.log import get_logger

from .common import (_get_backends, _soft_delete_file,
                     _health_scan_state, _health_scan_lock, _health_scan_event,
                     _safe_stat, mark_op_started, _op_ever_started,
                     op_log_start, op_log_finish)

log = get_logger("web.routes.health")


def register(app, ctx):

    @app.get("/health", response_class=HTMLResponse)
    async def health_page(request: Request):
        """Data Health dashboard. Replaced the old JSON ping — the only
        consumers (restart pollers in dashboard/settings) just check r.ok,
        and JSON access to the same data is /api/health/data."""
        def _run_summary():
            # summary() is a multi-query scan over the live tables — keep it
            # off the event loop (qa defect D18). The executor thread always
            # drops its own SQLite handle so no pooled connection outlives
            # the request. The SSE scan stream below was already handled.
            try:
                return data_health.summary(ctx.config)
            finally:
                db.close_connection()

        summary = await asyncio.to_thread(_run_summary)
        return ctx.templates.TemplateResponse("health.html", {
            "request": request,
            "summary": summary,
        })

    # DEPRECATED (B04): use /api/health/data — kept as alias for old callers.
    # The old bespoke payload (db integrity/daemon/replication snapshot) is
    # retired; this endpoint now returns exactly the canonical JSON of
    # /api/health/data (summary + last scan snapshot).
    @app.get("/api/health/detailed")
    async def api_health_detailed():
        return await api_health_data()

    @app.get("/api/health/data")
    async def api_health_data():
        with _health_scan_lock:
            scan_snapshot = json.loads(json.dumps(_health_scan_state))
            finished = _health_scan_state["finished_at"]
            running = _health_scan_state["running"]
        scan_age = max(0.0, time.time() - finished) if finished else None

        def _run_summary():
            # data_health.summary() runs the multi-query damage scan —
            # blocking work kept off the event loop (qa defect D18 class).
            try:
                return data_health.summary(ctx.config)
            finally:
                db.close_connection()

        summary = await asyncio.to_thread(_run_summary)
        return JSONResponse({
            "summary": summary,
            "scan": scan_snapshot,
            "scan_running": running,
            "scan_age_seconds": scan_age,
        })

    @app.get("/api/health/issues")
    async def api_health_issues(reason: str = "no_chunks", limit: int = 50, offset: int = 0):
        try:
            data = data_health.issues(reason, limit=limit, offset=offset)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(data)

    @app.get("/api/health/same-name")
    async def api_health_same_name(limit: int = 50, offset: int = 0):
        return JSONResponse(data_health.same_name_groups(limit=limit, offset=offset))

    @app.get("/api/health/purge-candidates")
    async def api_health_purge_candidates(limit: int = 50, offset: int = 0):
        return JSONResponse(data_health.purge_candidates(limit=limit, offset=offset))

    @app.post("/api/health/scan")
    async def api_health_scan():
        with _health_scan_lock:
            if _health_scan_state["running"]:
                return JSONResponse(
                    {"error": "A health scan is already running"}, status_code=409)
            _health_scan_state.update({
                "running": True, "started_at": time.time(), "finished_at": 0.0,
                "completed": False, "results": {}, "remote": [], "errors": [],
            })
        # Op actually launched: its SSE stream polls to completion instead
        # of closing after one idle snapshot (same contract as
        # make_sse_stream's ops).
        mark_op_started(_health_scan_state)
        threading.Thread(target=_health_scan_worker, daemon=True).start()
        return JSONResponse({"status": "started"})

    @app.get("/api/health/scan/stream")
    async def api_health_scan_stream():
        async def event_generator():
            last_snapshot = ""
            emitted_any = False
            while True:
                with _health_scan_lock:
                    snapshot = json.dumps(_health_scan_state)
                if snapshot != last_snapshot:
                    yield f"data: {snapshot}\n\n"
                    last_snapshot = snapshot
                    emitted_any = True
                with _health_scan_lock:
                    done = _health_scan_state["completed"] and not _health_scan_state["running"]
                if done:
                    yield f"data: {snapshot}\n\n"
                    break
                if emitted_any and not _op_ever_started(_health_scan_state):
                    # Never-started op: emit the one idle snapshot and close
                    # (pre-first-op termination, matches make_sse_stream).
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/health/purge")
    async def api_health_purge(data: dict = Body(default={})):
        if data.get("confirm") is not True:
            return JSONResponse(
                {"error": "confirm must be exactly true"}, status_code=400)
        file_ids = data.get("file_ids")
        if (not isinstance(file_ids, list) or not file_ids
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in file_ids)):
            return JSONResponse(
                {"error": "file_ids must be a non-empty list of integers"},
                status_code=400)
        # Re-verify EVERY id at purge time; reject the whole request otherwise.
        candidates = data_health.filter_purge_candidate_ids(file_ids)
        rejected = sorted({i for i in file_ids if i not in candidates})
        if rejected:
            return JSONResponse({
                "error": ("not purge candidates, nothing deleted: "
                          + ", ".join(str(i) for i in rejected)),
                "rejected_ids": rejected,
            }, status_code=400)
        results = []
        # B12: every purge is audited to op_log (bookkeeping must never fail
        # the operation — the helpers log their own failures, B07).
        op_id = op_log_start("purge", f"health_purge:{int(time.time())}",
                             {"file_ids": list(file_ids)})
        try:
            for fid in file_ids:
                outcome = _soft_delete_file(fid)
                results.append({
                    "file_id": fid,
                    "status": outcome["status"],
                    "message": outcome["message"],
                })
        except Exception as e:
            log.exception("health purge crashed mid-run")
            op_log_finish(op_id, "failed", last_error=str(e))
            raise
        op_log_finish(op_id, "completed", target_json={
            "requested": len(file_ids),
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "skipped": sum(1 for r in results if r["status"] != "ok"),
        })
        return JSONResponse({"status": "ok", "results": results})

    # ── Data Health: physical reachability scan worker ──────────

    def _safe_backend_join(root: str, rel_path: str):
        """Join a storage_path onto the backend root, refusing traversal.

        Returns None for empty/traversal/absolute paths — never a path that
        escapes the backend root.
        """
        parts = [p for p in rel_path.replace("\\", "/").split("/")
                 if p not in ("", ".", "..")]
        if not parts or rel_path.replace("\\", "/").startswith("/"):
            return None
        if sys.platform == "win32" and (len(parts[0]) == 2 and parts[0][1] == ":"):
            return None
        return os.path.join(root, *parts)

    def _health_scan_backend(name: str, root: str, plan: dict) -> dict:
        """Stat every plain object and bundle parent at a local backend root,
        then resolve members through their parent bundle file."""
        result = {
            "backend": name, "root": root or "",
            "objects_checked": 0, "objects_ok": 0,
            "objects_missing": [], "objects_size_mismatch": [],
            "bundles_checked": 0, "bundles_ok": 0, "bundles_missing": [],
            "bundles_size_mismatch": [],
            "members_checked": 0, "members_ok": 0, "members_unreachable": [],
            "errors": [],
        }
        cap = 500
        if not root:
            result["errors"].append("backend root path is not configured")
            return result
        parent_status: dict[str, tuple] = {}
        for entry in plan.get("plain", []):
            full = _safe_backend_join(root, entry["path"])
            st = None
            if full is not None:
                try:
                    st = _safe_stat(full)
                except Exception as e:
                    result["errors"].append(f"{entry['path']}: {str(e)[:120]}")
                    st = None
            exists = st is not None
            actual_size = st.st_size if exists else None
            bucket = "bundles" if entry["is_bundle_parent"] else "objects"
            result[f"{bucket}_checked"] += 1
            if not exists:
                result[f"{bucket}_missing"].append(entry["path"])
            else:
                result[f"{bucket}_ok"] += 1
                expected = entry["stored_size"]
                if expected and actual_size != expected:
                    result[f"{bucket}_size_mismatch"].append(entry["path"])
            if entry["is_bundle_parent"]:
                parent_status[entry["path"]] = (exists, actual_size,
                                                int(entry.get("pt_size") or 0))
        for m in plan.get("members", []):
            result["members_checked"] += 1
            parent = m["parent"]
            info = parent_status.get(parent)
            if info is None:
                result["members_unreachable"].append({
                    "blob_id": m["blob_id"], "parent": parent,
                    "reason": "parent bundle has no storage row",
                })
                continue
            exists, size, pt_size = info
            if not exists:
                result["members_unreachable"].append({
                    "blob_id": m["blob_id"], "parent": parent,
                    "reason": "parent bundle missing",
                })
            elif pt_size and pt_size < m["offset"] + m["length"]:
                # Member ranges are offsets into the parent bundle's PLAINTEXT
                # stream, so the reachability check must use blobs.pt_size —
                # never the compressed on-disk size, which is typically smaller
                # and produced false data-loss alarms for every compressed
                # bundle (qa defect D2).
                result["members_unreachable"].append({
                    "blob_id": m["blob_id"], "parent": parent,
                    "reason": (f"parent bundle plaintext size {pt_size} smaller than "
                               f"offset+length {m['offset'] + m['length']}"),
                })
            else:
                result["members_ok"] += 1
        for key in ("objects_missing", "objects_size_mismatch",
                    "bundles_missing", "members_unreachable", "errors"):
            result[key] = result[key][:cap]
        return result

    def _health_scan_worker():
        try:
            backends = _get_backends(ctx)
            local = [b for b in backends if b.type == "local"]
            remote = []
            for b in [x for x in backends if x.type != "local"]:
                # Sampled existence check: catches phantom storage rows (D6)
                # without a full remote walk.
                try:
                    remote.append(data_health.sample_remote_backend(b, n=25))
                except Exception as e:
                    remote.append({"backend": b.name, "name": b.name,
                                   "type": getattr(b, "type", "?"),
                                   "note": "error", "checked": 0, "missing": [],
                                   "errors": [str(e)[:200]]})
            plan = data_health.local_scan_plan([b.name for b in local])
            results = {}
            for b in local:
                root = getattr(b, "base_path", "") or (b.config or {}).get("path", "")
                try:
                    results[b.name] = _health_scan_backend(
                        b.name, root, plan.get(b.name, {}))
                except Exception as e:
                    results[b.name] = {
                        "backend": b.name, "root": root,
                        "errors": [str(e)[:200]],
                    }
            with _health_scan_lock:
                _health_scan_state.update({
                    "results": results, "remote": remote, "errors": [],
                    "running": False, "completed": True,
                    "finished_at": time.time(),
                })
        except Exception as e:
            with _health_scan_lock:
                _health_scan_state.update({
                    "running": False, "completed": True,
                    "finished_at": time.time(),
                    "errors": [str(e)[:200]],
                })
        _health_scan_event.set()

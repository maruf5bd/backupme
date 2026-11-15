"""Backend routes (backlog B05): list/add/remove/test/detail/update, usage,
status, verify, rescan, onboard, rebalance, drain + the rebalance SSE stream.

ORDER CRITICAL inside this module: GET /api/backends/usage and GET
/api/backends/status are registered BEFORE GET /api/backends/{name} so
"usage"/"status" are not captured by the path parameter. The
verify/rescan/onboard/rebalance/drain group keeps the exact relative order
the monolith had.
"""
import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import Body
from fastapi.responses import JSONResponse

from backupme.core import database as db
from backupme.core import verify
from backupme.log import get_logger

from .common import (_get_backend, _get_backends, _config_as_dict, _config_dict,
                     _backend_fingerprint, _backend_status_cache,
                     _list_backend_configs,
                     _rebalance_state, _rebalance_lock, _reset_rebalance,
                     _process_state, _process_lock, _reset_process,
                     _speedtest_state, _speedtest_lock, mark_op_started,
                     op_log_start, op_log_finish,
                     make_sse_stream, start_daemon_thread)

log = get_logger("web.routes.backends")

# Backend types in the same fixed order _get_backends walks the config.
_BACKEND_TYPE_ORDER = ("local", "ftp", "sftp", "smb", "ssh", "rclone", "ninedrive")

# Speed-test payload size (Phase C): a few KB is enough to measure a real
# round trip through every backend type without loading the link.
_SPEEDTEST_SIZE_BYTES = 8 * 1024


# ── Cached per-backend status probe (Phase A page-perf) ──────────────────
# Lives at module level so GET /api/backends/status (below) and GET
# /api/status (misc.py, Phase C perf A2) share one implementation; the
# resolution goes through THIS module's _get_backends global, which is the
# monkeypatch surface tests/test_web_perf_pages.py pins.
def _status_probe_row(be, name: str) -> dict:
    status = {
        "name": name,
        "type": getattr(be, "type", ""),
        "connected": False,
        "free_space": None,
        "total_space": None,
        "priority": be.config.get("priority", 50),
        "quota": be.config.get("quota", None),
        "error": None,
    }
    try:
        connected = bool(be.connected) or bool(be.connect())
    except Exception as e:
        status["error"] = f"connect failed: {e}"
        return status
    status["connected"] = connected
    if connected:
        try:
            status["free_space"] = be.free_space()
            status["total_space"] = be.total_space()
        except Exception as e:
            status["error"] = f"space probe failed: {e}"
    return status


def _status_probe_all(ctx) -> list[dict]:
    """Worker-thread body behind GET /api/backends/status (run it via
    asyncio.to_thread — never on the event loop). Connect + free/total only —
    never walk(). Per-backend rows cache by config fingerprint; the pool is
    resolved only when at least one backend needs a probe."""
    cfg = _config_dict(ctx)
    ordered = []  # [fingerprint, name, row-or-None]
    needs_probe = False
    for btype in _BACKEND_TYPE_ORDER:
        for entry in cfg.get(btype, []):
            name = entry.get("name", "")
            if not name:
                continue
            fingerprint = _backend_fingerprint(entry)
            cached = _backend_status_cache.get(fingerprint)
            ordered.append([fingerprint, name, cached])
            if cached is None:
                needs_probe = True
    if needs_probe:
        by_name = {b.name: b for b in _get_backends(ctx)}
        for slot in ordered:
            if slot[2] is not None:
                continue
            fingerprint, name = slot[0], slot[1]
            be = by_name.get(name)
            row = _status_probe_row(be, name) if be is not None else {
                "name": name, "type": "", "connected": False,
                "free_space": None, "total_space": None,
                "priority": 50, "quota": None,
                "error": "backend could not be resolved",
            }
            _backend_status_cache.set(fingerprint, row)
            slot[2] = row
    return [row for _, _, row in ordered if row is not None]


async def _status_rows(ctx) -> list[dict]:
    """Cached per-backend status rows for any JSON consumer.

    Shared by GET /api/backends/status and GET /api/status (Phase C perf
    A2: the dashboard's 10 s /api/status poll must not probe every backend
    on every call — it rides the same ~30 s TTL cache). Never raises to the
    caller: a failing probe path degrades to honest offline rows
    (connected:false plus an error string)."""
    try:
        return await asyncio.to_thread(_status_probe_all, ctx)
    except Exception as e:
        log.exception("backend status probe failed; serving offline rows")
        offline = [{"name": b["name"], "type": b["type"], "connected": False,
                    "free_space": None, "total_space": None,
                    "priority": b["priority"], "quota": b["quota"],
                    "error": f"status probe failed: {e}"}
                   for b in _list_backend_configs(ctx)]
        return offline


def register(app, ctx):

    @app.get("/api/backends")
    async def api_backends():
        backends = _get_backends(ctx)
        result = []
        for b in backends:
            free = None
            total = None
            try:
                free = b.free_space()
                total = b.total_space()
            except Exception as e:
                log.warning("free/total space probe failed for backend %s: %s", b.name, e)
            result.append({
                "name": b.name, "type": b.type, "connected": b.connected,
                "free_space": free, "total_space": total,
                "priority": b.config.get("priority", 50),
                "quota": b.config.get("quota", None),
            })
        return JSONResponse(result)

    @app.post("/api/backends/add")
    async def api_backend_add(data: dict = Body(...)):
        try:
            btype = data.get("type", "")
            name = data.get("name", "")
            if not btype or not name:
                return JSONResponse({"status": "error", "message": "type and name required"})
            use_env = data.get("use_env", "false") in ("true", True)
            pw = data.get("password", "")
            payload = {k: v for k, v in data.items() if k != "use_env"}
            if pw and use_env:
                env_key = f"BACKUP_{name.upper()}_PASS"
                payload["password"] = "{{ " + env_key + " }}"
            # Convert numeric fields. Non-numeric form input is left as-is
            # on purpose (expected control flow, not a failure): the backend
            # factory re-validates these fields when the config is loaded.
            if "priority" in payload:
                try: payload["priority"] = int(payload["priority"])
                except (ValueError, TypeError): pass
            if "port" in payload:
                try: payload["port"] = int(payload["port"])
                except (ValueError, TypeError): pass

            yaml_path = Path("config.yaml")
            if yaml_path.exists():
                import yaml
                raw = yaml.safe_load(yaml_path.read_text()) or {}
            else:
                raw = {}
            raw.setdefault("backends", {}).setdefault(btype, [])
            existing = [d for d in raw["backends"][btype] if d.get("name") == name]
            if existing:
                existing[0].update({k: v for k, v in payload.items() if v})
            else:
                raw["backends"][btype].append(payload)

            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, indent=2)

            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.delete("/api/backends/remove/{name}")
    async def api_backend_remove(name: str, data: dict = Body(default={})):
        if data.get("confirm") is not True:
            return JSONResponse(
                {"error": "confirm must be exactly true"}, status_code=400)
        try:
            yaml_path = Path("config.yaml")
            if not yaml_path.exists():
                return JSONResponse({"status": "error", "message": "no config"})
            import yaml
            raw = yaml.safe_load(yaml_path.read_text()) or {}
            for btype in list(raw.get("backends", {})):
                raw["backends"][btype] = [d for d in raw["backends"][btype] if d.get("name") != name]
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, indent=2)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.get("/api/backends/test/{name}")
    async def api_backend_test(name: str):
        try:
            be = _get_backend(ctx, name)
            if not be:
                return JSONResponse({"connected": False, "error": "not found"})
            ok = be.connect()
            return JSONResponse({"connected": ok, "error": None if ok else "connection failed"})
        except Exception as e:
            return JSONResponse({"connected": False, "error": str(e)})

    # NOTE: registered before /api/backends/{name} so "usage" is not captured.
    @app.get("/api/backends/usage")
    async def api_backends_usage():
        from backupme.core import rebalance as rb
        usage = {u["backend_name"]: u for u in rb.backend_usage()}
        out = []
        for be in _get_backends(ctx):
            free = total = None
            try:
                free, total = be.free_space(), be.total_space()
            except Exception as e:
                log.warning("free/total space probe failed for backend %s: %s", be.name, e)
            u = usage.get(be.name, {})
            out.append({
                "name": be.name, "type": be.type, "connected": be.connected,
                "stored_bytes": u.get("stored_bytes", 0),
                "blobs": u.get("blobs", 0),
                "ingest_24h_bytes": u.get("ingest_24h_bytes", 0),
                "free_space": free, "total_space": total,
            })
        return JSONResponse({"backends": out})

    # NOTE: registered before /api/backends/{name} so "status" is not captured.
    @app.get("/api/backends/status")
    async def api_backends_status():
        """Cheap per-backend status for the dashboard cards (Phase A page-perf).

        Returns a JSON list, one row per configured backend:
            {"name", "type", "connected", "free_space", "total_space",
             "priority", "quota", "error"}          # error is null when fine

        Resolution + probes run on a worker thread (never the event loop)
        and results are cached per backend config fingerprint with a ~30 s
        TTL — a second call inside the TTL returns the cached rows without
        any probing. NEVER calls walk(): connect + free/total only. A
        failing backend degrades to connected:false plus an error string;
        the endpoint itself never raises to the client. The probe body
        lives at module level (_status_probe_all/_status_rows) and is
        shared with GET /api/status (Phase C perf A2).
        """
        return JSONResponse(await _status_rows(ctx))

    # ── Backend speed test (Phase C) ─────────────────────────────
    # Uploads a small unique test object to the backend, downloads it back,
    # hash-verifies the round trip, deletes the object, and measures
    # upload/download ms + object size. Single-flight like the db check
    # (POST while running -> 409). The delete runs in a finally clause: a
    # failed test must never leave litter on a backend. `speedtest: false`
    # in a backend's YAML entry opts it out (BackendDef.speedtest).

    def _speedtest_worker(op_id: int | None, backend_name: str):
        result = {"backend": backend_name, "verified": False, "deleted": False}
        status = "error"
        remote_rel = ""
        uploaded = False
        src_path = dl_path = None
        be = None
        try:
            be = _get_backend(ctx, backend_name)
            if be is None:
                raise RuntimeError(f"backend '{backend_name}' not found")
            payload = os.urandom(_SPEEDTEST_SIZE_BYTES)
            expected_sha = hashlib.sha256(payload).hexdigest()
            remote_rel = (f"speedtest/speedtest-{int(time.time())}-"
                          f"{uuid.uuid4().hex[:8]}.tmp")
            result["object"] = remote_rel
            result["size"] = len(payload)
            # The backend API takes a local file path for uploads, so the
            # payload goes through two temp files (uploaded + downloaded).
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp:
                tmp.write(payload)
                src_path = tmp.name
            dl_path = src_path + ".dl"
            t0 = time.perf_counter()
            be.upload(src_path, remote_rel)
            uploaded = True
            result["upload_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            t0 = time.perf_counter()
            be.download(remote_rel, dl_path)
            result["download_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            with open(dl_path, "rb") as f:
                got = f.read()
            result["verified"] = hashlib.sha256(got).hexdigest() == expected_sha
            if not result["verified"]:
                result["error"] = "round-trip hash mismatch"
            status = "ok" if result["verified"] else "error"
        except Exception as e:
            status = "error"
            result["error"] = str(e)
        finally:
            # The test object must never linger on the backend — even when
            # a step above raised. Only attempt the delete when the upload
            # actually happened (nothing to clean up otherwise); a failed
            # cleanup stays visible in the result instead of being silent.
            if uploaded and be is not None:
                try:
                    be.delete(remote_rel)
                    result["deleted"] = True
                except Exception as del_exc:
                    result["error"] = result.get("error") or \
                        f"cleanup delete failed: {del_exc}"
            # A failed cleanup downgrades the outcome: the test object is
            # still sitting on the backend — that is not an "ok" result.
            if status == "ok" and not result.get("deleted"):
                status = "error"
            for tmp_file in (src_path, dl_path):
                if tmp_file:
                    try:
                        Path(tmp_file).unlink(missing_ok=True)
                    except Exception:
                        pass
            finished_at = time.time()
            with _speedtest_lock:
                _speedtest_state.update({
                    "status": status,
                    "result": result,
                    "error": result.get("error", ""),
                    "finished_at": finished_at,
                })
            op_log_finish(op_id, "completed" if status == "ok" else "failed",
                          target_json=result)
            with _speedtest_lock:
                _speedtest_state["running"] = False
                _speedtest_state["completed"] = True
            db.close_connection()

    # NOTE: registered before /api/backends/{name} so a POST to a backend
    # named "speedtest" can never be captured by the detail/update routes.
    @app.post("/api/backends/{name}/speedtest")
    async def api_backend_speedtest(name: str):
        # Config-only opt-out lookup: no connects, no probes.
        cfg_rows = {r["name"]: r for r in _list_backend_configs(ctx)}
        row = cfg_rows.get(name)
        if row is None:
            return JSONResponse(
                {"status": "error", "message": f"backend '{name}' not found"},
                status_code=404)
        if not row.get("speedtest", True):
            return JSONResponse(
                {"status": "error",
                 "message": f"speed test is disabled for '{name}' "
                            "(speedtest: false in config)"},
                status_code=400)
        with _speedtest_lock:
            if _speedtest_state["running"]:
                return JSONResponse(
                    {"status": "busy",
                     "message": "a speed test is already running"},
                    status_code=409)
            _speedtest_state.update({
                "running": True, "backend": name,
                "started_at": time.time(), "finished_at": 0.0,
                "status": "", "error": "", "result": None, "completed": False,
            })
        mark_op_started(_speedtest_state)
        op_id = op_log_start("speedtest", f"speedtest:{name}:{int(time.time())}",
                             {"backend": name})
        start_daemon_thread(_speedtest_worker, op_id, name)
        return JSONResponse({"status": "started", "backend": name})

    # NOTE: registered before /api/backends/{name} so the literal segment
    # "speedtest" is not captured by the detail path parameter.
    @app.get("/api/backends/speedtest/status")
    async def api_backend_speedtest_status():
        """Live speed-test state plus the latest persisted outcome per
        backend (from op_log, op_type='speedtest') so the per-card result
        line survives page reloads and server restarts."""
        with _speedtest_lock:
            snapshot = dict(_speedtest_state)
        recent = []
        try:
            rows = db.execute(
                "SELECT id, status, started_at, completed_at, target_json, last_error "
                "FROM op_log WHERE op_type = 'speedtest' ORDER BY id DESC LIMIT 20"
            ).fetchall()
            seen = set()
            for r in rows:
                try:
                    target = json.loads(r["target_json"]) if r["target_json"] else {}
                except ValueError:
                    target = {}
                backend_name = target.get("backend", "")
                if backend_name in seen:
                    continue
                seen.add(backend_name)
                recent.append({
                    "backend": backend_name,
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "completed_at": r["completed_at"],
                    "upload_ms": target.get("upload_ms"),
                    "download_ms": target.get("download_ms"),
                    "size": target.get("size"),
                    "verified": target.get("verified"),
                    "error": target.get("error") or r["last_error"] or "",
                })
        except Exception as e:
            log.warning("speedtest recent-result lookup failed: %s", e)
        snapshot["recent"] = recent
        return JSONResponse(snapshot)

    @app.get("/api/backends/{name}")
    async def api_backend_detail(name: str):
        be = _get_backend(ctx, name)
        if not be:
            return JSONResponse({"error": "not found"}, status_code=404)
        free = total = None
        try:
            free = be.free_space()
            total = be.total_space()
        except Exception as e:
            log.warning("free/total space probe failed for backend %s: %s", be.name, e)
        return JSONResponse({
            "name": be.name,
            "type": be.type,
            "connected": be.connected,
            "free_space": free,
            "total_space": total,
            "priority": be.config.get("priority", 50),
            "quota": be.config.get("quota", None),
            "config": {k: v for k, v in be.config.items() if k != "password"},
        })

    @app.put("/api/backends/{name}")
    async def api_backend_update(name: str, data: dict = Body(...)):
        try:
            yaml_path = Path("config.yaml")
            if not yaml_path.exists():
                return JSONResponse({"status": "error", "message": "no config"})
            import yaml
            raw = yaml.safe_load(yaml_path.read_text()) or {}
            found = False
            for btype in raw.get("backends", {}):
                for be in raw["backends"][btype]:
                    if be.get("name") == name:
                        use_env = data.get("use_env", False) in ("true", True)
                        pw = data.get("password", "")
                        payload = {k: v for k, v in data.items() if k not in ("use_env",)}
                        if pw and use_env:
                            env_key = f"BACKUP_{name.upper()}_PASS"
                            payload["password"] = "{{ " + env_key + " }}"
                        for k, v in payload.items():
                            if v is not None and v != "":
                                be[k] = v
                        found = True
                        break
                if found:
                    break
            if not found:
                return JSONResponse({"status": "error", "message": f"Backend '{name}' not found"})
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, indent=2)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # ── API: Backend Verify ──────────────────────────────────────

    @app.post("/api/backends/{name}/verify")
    async def api_backend_verify(name: str, count: int = 10):
        try:
            def _run_check():
                # run_integrity_check downloads + hashes blobs over the
                # network — blocking work that must not sit on the event loop
                # (qa defect D18); the executor thread always drops its own
                # SQLite handle before returning.
                try:
                    return verify.run_integrity_check(
                        f"backend:{name}", count=count, backend_name=name)
                finally:
                    db.close_connection()

            result = await asyncio.to_thread(_run_check)
            return JSONResponse({"status": "ok", **result})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/backends/{name}/rescan")
    async def api_backend_rescan(name: str):
        be = _get_backend(ctx, name)
        if be is None:
            return JSONResponse({"error": f"backend '{name}' not found"}, status_code=404)
        try:
            connected = be.connected or be.connect()
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=502)
        if not connected:
            return JSONResponse(
                {"status": "error", "message": f"backend '{name}' connect failed"},
                status_code=502)
        try:
            from backupme.core.backend_scan import rescan_backend
            # rescan_backend walks the backend listing over the network —
            # keep it off the event loop (qa defect D18). It returns a
            # JSON-ready dict (counts + sorted lists).
            return JSONResponse(await asyncio.to_thread(rescan_backend, be))
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # ── Serial files-ingest (download -> pipeline -> store back) ─────

    def _process_progress_cb(done, skipped, errors, total):
        with _process_lock:
            _process_state["done"] = done
            _process_state["skipped"] = skipped
            _process_state["errors"] = errors
            _process_state["total"] = total

    def _process_worker(backend_name, prefix, target, delete):
        try:
            from backupme.core.files_ingest import ingest_files
            cfg = _config_as_dict(ctx)
            result = ingest_files(
                backend_name=backend_name,
                config=cfg,
                prefix=prefix,
                target_backend_name=target or None,
                delete=bool(delete),
                progress_cb=_process_progress_cb,
            )
            with _process_lock:
                _process_state["result"] = result
                if result.get("status") == "error":
                    _process_state["errors_list"].append(str(result.get("error")))
        except Exception as e:
            with _process_lock:
                _process_state["errors_list"].append(str(e))
        finally:
            with _process_lock:
                _process_state["running"] = False
                _process_state["completed"] = True
            db.close_connection()

    @app.post("/api/backends/{name}/process-files")
    async def api_backend_process_files(name: str, data: dict = Body(...)):
        if data.get("confirm") is not True:
            return JSONResponse(
                {"error": "confirm must be the literal boolean true"}, status_code=400)
        be = _get_backend(ctx, name)
        if be is None:
            return JSONResponse({"error": f"backend '{name}' not found"}, status_code=404)
        with _process_lock:
            if _process_state["running"]:
                return JSONResponse(
                    {"status": "busy",
                     "message": "another process-files job is already running"},
                    status_code=409)
            prefix = str(data.get("prefix") or "")
            target = str(data.get("target") or "") or None
            delete = bool(data.get("delete") or False)
            _reset_process(name, prefix, target, delete)
        start_daemon_thread(_process_worker, name, prefix, target, delete)
        return JSONResponse({"status": "started", "backend": name,
                             "prefix": prefix, "target": target or "",
                             "delete": delete})

    @app.get("/api/backends/{name}/process-files/status")
    async def api_backend_process_files_status(name: str):
        with _process_lock:
            snapshot = dict(_process_state)
        snapshot["errors_list"] = snapshot.get("errors_list", [])[-5:]
        return JSONResponse(snapshot)

    @app.get("/api/backends/{name}/process-files/stream")
    async def api_backend_process_files_stream(name: str):
        return make_sse_stream(_process_state, _process_lock)

    # ── Backends: onboard / rebalance / drain ────────────────────

    def _rebalance_progress_cb(blob_id, done, total):
        with _rebalance_lock:
            _rebalance_state["current_blob"] = blob_id
            _rebalance_state["done"] = done
            _rebalance_state["total"] = total

    def _rebalance_worker(kind, backend_name):
        try:
            from backupme.core import rebalance as rb
            cfg_dict = _config_as_dict(ctx)
            backends = _get_backends(ctx)
            if kind == "drain":
                result = rb.drain_backend(backend_name, backends, cfg_dict,
                                          progress_cb=_rebalance_progress_cb)
            else:
                result = rb.rebalance_to(backend_name, backends, cfg_dict,
                                         progress_cb=_rebalance_progress_cb)
            with _rebalance_lock:
                _rebalance_state["result"] = result
                if result.get("status") not in ("ok",):
                    _rebalance_state["errors"].append(str(result.get("error") or result.get("failed") or "partial"))
        except Exception as e:
            with _rebalance_lock:
                _rebalance_state["errors"].append(str(e))
        finally:
            with _rebalance_lock:
                _rebalance_state["running"] = False
                _rebalance_state["completed"] = True
            db.close_connection()

    @app.post("/api/backends/onboard")
    async def api_backends_onboard(data: dict = Body(...)):
        name = (data.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "backend name required"}, status_code=400)
        be = _get_backend(ctx, name)
        if be is None:
            return JSONResponse({"error": f"backend '{name}' not found or not connectable"}, status_code=404)
        free = total = None
        try:
            free, total = be.free_space(), be.total_space()
        except Exception as e:
            log.warning("free/total space probe failed for backend %s: %s", be.name, e)
        result = {"name": name, "type": be.type, "connected": be.connected,
                  "free_space": free, "total_space": total}
        if not data.get("rebalance"):
            return JSONResponse({"status": "ok", **result})
        with _rebalance_lock:
            if _rebalance_state["running"]:
                return JSONResponse({"error": "a rebalance operation is already running"}, status_code=409)
        from backupme.core import rebalance as rb
        result["plan"] = rb.plan_rebalance(name)
        _reset_rebalance("rebalance", name)
        start_daemon_thread(_rebalance_worker, "rebalance", name)
        return JSONResponse({"status": "started", **result})

    @app.post("/api/backends/{name}/rebalance")
    async def api_backend_rebalance(name: str, data: dict = Body(default={})):
        if _get_backend(ctx, name) is None:
            return JSONResponse({"error": f"backend '{name}' not found"}, status_code=404)
        with _rebalance_lock:
            if _rebalance_state["running"]:
                return JSONResponse({"error": "a rebalance operation is already running"}, status_code=409)
        limit = data.get("limit")
        _reset_rebalance("rebalance", name)
        start_daemon_thread(_rebalance_worker, "rebalance", name)
        return JSONResponse({"status": "started", "backend": name, "limit": limit})

    @app.post("/api/backends/{name}/drain")
    async def api_backend_drain(name: str):
        if _get_backend(ctx, name) is None:
            return JSONResponse({"error": f"backend '{name}' not found"}, status_code=404)
        with _rebalance_lock:
            if _rebalance_state["running"]:
                return JSONResponse({"error": "a rebalance operation is already running"}, status_code=409)
        _reset_rebalance("drain", name)
        start_daemon_thread(_rebalance_worker, "drain", name)
        return JSONResponse({"status": "started", "backend": name})

    @app.get("/api/backends/rebalance/stream")
    async def api_backends_rebalance_stream():
        return make_sse_stream(_rebalance_state, _rebalance_lock)

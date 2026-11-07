"""Backend routes (backlog B05): list/add/remove/test/detail/update, usage,
verify, rescan, onboard, rebalance, drain + the rebalance SSE stream.

ORDER CRITICAL inside this module: GET /api/backends/usage is registered
BEFORE GET /api/backends/{name} so "usage" is not captured by the path
parameter. The verify/rescan/onboard/rebalance/drain group keeps the exact
relative order the monolith had.
"""
from pathlib import Path

from fastapi import Body
from fastapi.responses import JSONResponse

from backupme.core import database as db
from backupme.core import verify
from backupme.log import get_logger

from .common import (_get_backend, _get_backends, _config_as_dict,
                     _rebalance_state, _rebalance_lock, _reset_rebalance,
                     make_sse_stream, start_daemon_thread)

log = get_logger("web.routes.backends")


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
            result = verify.run_integrity_check(f"backend:{name}", count=count, backend_name=name)
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
            # rescan_backend returns a JSON-ready dict (counts + sorted lists).
            return JSONResponse(rescan_backend(be))
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

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

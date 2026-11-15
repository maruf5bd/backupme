"""Scheduler/daemon routes (backlog B05): daemon start/stop/status, server
restart (confirm gate), scheduler config get/set.

The daemon handle lives on the RouteContext so a freshly built app always
starts with no scheduler running, exactly like the old per-register closure.
"""
import os
import sys
import time
from pathlib import Path

from fastapi import Body
from fastapi.responses import JSONResponse

from backupme.log import get_logger

log = get_logger("web.routes.scheduler")


def register(app, ctx):
    _scheduler = None

    @app.post("/api/daemon/start")
    async def api_daemon_start():
        nonlocal _scheduler
        try:
            from backupme.scheduler import BackupScheduler
            import dataclasses
            cfg = ctx.config
            if dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
                cfg = dataclasses.asdict(cfg)
            if _scheduler and _scheduler._thread and _scheduler._thread.is_alive():
                return JSONResponse({"status": "already_running"})
            _scheduler = BackupScheduler(cfg)
            _scheduler.start()
            return JSONResponse({"status": "ok", "message": "Daemon started"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/daemon/stop")
    async def api_daemon_stop():
        nonlocal _scheduler
        try:
            if not _scheduler:
                return JSONResponse({"status": "not_running"})
            _scheduler.stop()
            _scheduler = None
            return JSONResponse({"status": "ok", "message": "Daemon stopped"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.get("/api/daemon/status")
    async def api_daemon_status():
        running = _scheduler is not None and _scheduler._thread is not None and _scheduler._thread.is_alive()
        return JSONResponse({"running": running})

    @app.post("/api/server/restart")
    async def api_server_restart(data: dict = Body(default={})):
        if data.get("confirm") is not True:
            return JSONResponse(
                {"error": "confirm must be exactly true"}, status_code=400)

        import threading

        def _do_restart():
            time.sleep(1)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=True).start()
        return JSONResponse({"status": "restarting", "message": "Server restarting in 1 second..."})

    @app.get("/api/scheduler/config")
    async def api_scheduler_config():
        try:
            import yaml
            yaml_path = Path("config.yaml")
            if not yaml_path.exists():
                return JSONResponse({"interval": 3600, "enabled": False})
            raw = yaml.safe_load(yaml_path.read_text()) or {}
            sched = raw.get("scheduler", raw.get("daemon", {}))
            return JSONResponse({
                "interval": sched.get("interval", 3600),
                "enabled": sched.get("enabled", False),
            })
        except Exception as e:
            log.warning("scheduler config read failed for /api/scheduler/config, serving defaults: %s", e)
            return JSONResponse({"interval": 3600, "enabled": False})

    @app.post("/api/scheduler/config")
    async def api_scheduler_config_save(data: dict = Body(...)):
        try:
            import yaml
            yaml_path = Path("config.yaml")
            if yaml_path.exists():
                raw = yaml.safe_load(yaml_path.read_text()) or {}
            else:
                raw = {}
            raw.setdefault("scheduler", {})
            if "interval" in data:
                raw["scheduler"]["interval"] = int(data["interval"])
            if "enabled" in data:
                raw["scheduler"]["enabled"] = bool(data["enabled"])
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, indent=2)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

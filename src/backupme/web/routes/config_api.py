"""Config routes (backlog B05): sources (list/add/delete), config.yaml
export/import/save.

All handlers rewrite config.yaml via PyYAML and then reload the live Config
through the shared _reload_config helper.
"""
import io
from pathlib import Path

from fastapi import Body, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backupme.core import database as db
from backupme.log import get_logger

from .common import _reload_config

log = get_logger("web.routes.config_api")


def register(app, ctx):

    @app.get("/api/sources")
    async def api_sources():
        # Get configured sources from config.yaml
        cfg_sources = {b.source for b in getattr(ctx.config, "backups", [])}
        # Get existing source roots in metadata DB
        try:
            rows = db.execute(
                "SELECT DISTINCT source_root FROM files WHERE deleted_at IS NULL"
            ).fetchall()
            db_sources = {r["source_root"] for r in rows}
        except Exception as e:
            log.warning("source roots query failed for /api/sources: %s", e)
            db_sources = set()

        all_sources = sorted(list(cfg_sources.union(db_sources)))
        return JSONResponse(all_sources)

    @app.post("/api/config/sources")
    async def api_sources_add(payload: dict = Body(...)):
        try:
            source = payload.get("source", "").strip()
            if not source:
                return JSONResponse({"status": "error", "message": "Source path is required"}, status_code=400)

            import yaml
            yaml_path = Path("config.yaml")
            if yaml_path.exists():
                raw = yaml.safe_load(yaml_path.read_text()) or {}
            else:
                raw = {}

            backups = raw.setdefault("backups", [])
            for b in backups:
                if b.get("source") == source:
                    return JSONResponse({"status": "error", "message": "Source path already exists"}, status_code=400)

            new_src = {
                "source": source,
                "mirror": bool(payload.get("mirror", False)),
                "exclude": [p.strip() for p in payload.get("exclude", "").split(",") if p.strip()] if isinstance(payload.get("exclude"), str) else payload.get("exclude", [])
            }
            if payload.get("backend"):
                new_src["backend"] = payload["backend"]

            backups.append(new_src)

            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, indent=2)

            _reload_config(ctx)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    @app.delete("/api/config/sources")
    async def api_sources_delete(source: str = Query(...)):
        try:
            import yaml
            yaml_path = Path("config.yaml")
            if not yaml_path.exists():
                return JSONResponse({"status": "error", "message": "Config file not found"}, status_code=404)

            raw = yaml.safe_load(yaml_path.read_text()) or {}
            backups = raw.get("backups", [])

            original_len = len(backups)
            backups = [b for b in backups if b.get("source") != source]

            if len(backups) == original_len:
                return JSONResponse({"status": "error", "message": f"Source '{source}' not found"}, status_code=404)

            raw["backups"] = backups

            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, indent=2)

            _reload_config(ctx)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    @app.get("/api/config/export")
    async def api_config_export():
        yaml_path = Path("config.yaml")
        if not yaml_path.exists():
            return JSONResponse({"error": "config.yaml not found"}, status_code=404)
        content = yaml_path.read_text(encoding="utf-8")
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="text/yaml",
            headers={"Content-Disposition": 'attachment; filename="config.yaml"'},
        )

    @app.post("/api/config")
    async def api_config_save(data: dict = Body(...)):
        try:
            import yaml
            yaml_path = Path("config.yaml")
            if yaml_path.exists():
                raw = yaml.safe_load(yaml_path.read_text()) or {}
            else:
                raw = {}
            for section, values in data.items():
                if isinstance(values, dict):
                    raw.setdefault(section, {})
                    for k, v in values.items():
                        # Handle dot-separated nested keys (e.g. "auto_backup.enabled")
                        if "." in k:
                            parts = k.split(".")
                            target = raw[section]
                            for p in parts[:-1]:
                                target = target.setdefault(p, {})
                            target[parts[-1]] = v
                        else:
                            raw[section][k] = v
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, indent=2)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/config/import")
    async def api_config_import(request: Request):
        try:
            import yaml
            body = await request.body()
            raw = yaml.safe_load(body.decode("utf-8"))
            if not isinstance(raw, dict):
                return JSONResponse({"status": "error", "message": "Invalid YAML"})
            yaml_path = Path("config.yaml")
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, indent=2)
            return JSONResponse({"status": "ok", "message": "Config imported successfully"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

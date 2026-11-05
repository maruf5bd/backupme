import asyncio
import io
import json
import os
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path

from fastapi import Request, Query, Body, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from .. import config as cfgmod
from ..core import database as db
from ..core import index as index_mod
from ..core import dedup, op_log, state, verify, snapshot as snapmod
from ..core import uploader as uploader_mod
from ..core import downloader as downloader_mod
from ..core import hasher as hasher_mod
from ..core import crypto as crypto_mod
from ..core import recover as recover_mod
from ..core import manifest
from ..core import data_health
from ..backends import get_backend as _raw_get_backend, get_all_backends as _raw_get_all_backends, invalidate_cache
from ..backends.base import StorageBackend


def _be_to_dict(be: cfgmod.BackendDef) -> dict:
    return {k: getattr(be, k) for k in ("type", "name", "path", "host", "port", "username",
                                         "password", "key_file", "share", "remote", "bwlimit",
                                         "quota", "priority", "weight", "base_url", "api_key",
                                         "email", "refresh_token", "folder")}


def _fts_phrase(q: str) -> str:
    """Quote a user query as a single FTS5 phrase so punctuation is never
    parsed as query syntax (qa defect D3: 'delta.txt' used to raise
    'fts5: syntax error near "."'). Escaped double quotes keep the result a
    valid string token for any input."""
    return '"' + q.replace('"', '""') + '"'


def _search_files_like(q: str, limit: int = 100) -> list[dict]:
    """Plain LIKE fallback for queries FTS5 refuses to parse — user input
    must never turn into a 500."""
    pattern = f"%{q}%"
    rows = db.execute(
        "SELECT * FROM files WHERE deleted_at IS NULL "
        "AND (relative_path LIKE ? OR content_text LIKE ?) "
        "ORDER BY id LIMIT ?",
        (pattern, pattern, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _search_files_safe(q: str, limit: int = 100) -> list[dict]:
    """FTS search with a hard LIKE fallback on sqlite parse errors."""
    try:
        return index_mod.search_files(_fts_phrase(q))
    except sqlite3.OperationalError:
        return _search_files_like(q, limit=limit)


def register_routes(app, config: cfgmod.Config):
    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def _reload_config():
        yaml_path = Path("config.yaml")
        if not yaml_path.exists():
            return
        import yaml
        raw = yaml.safe_load(yaml_path.read_text()) or {}
        config.backends.clear()
        from ..config import _build_backend, BackupDef
        for btype, blist in raw.get("backends", {}).items():
            for item in blist:
                name = item.get("name", f"{btype}_{len(config.backends.get(btype, []))}")
                config.backends.setdefault(btype, []).append(_build_backend(name, btype, dict(item)))
        config.backups.clear()
        for bdef in raw.get("backups", []):
            config.backups.append(BackupDef(**bdef))

    def _config_dict():
        _reload_config()
        result = {}
        for btype, blist in config.backends.items():
            result.setdefault(btype, []).extend(_be_to_dict(b) for b in blist)
        return result

    def _get_backends():
        invalidate_cache()
        cfg = _config_dict()
        connected = _raw_get_all_backends(cfg)
        connected_names = {b.name for b in connected}
        all_backends = list(connected)
        for btype in ("local", "ftp", "sftp", "smb", "ssh", "rclone", "ninedrive"):
            for entry in cfg.get(btype, []):
                name = entry.get("name", "")
                if name and name not in connected_names:
                    from ..backends import BACKEND_MAP
                    cls = BACKEND_MAP.get(btype)
                    if cls:
                        inst = cls(name, entry)
                        all_backends.append(inst)
        return all_backends

    def _get_backend(name: str):
        for b in _get_backends():
            if b.name == name:
                return b
        return None

    def _invalidate():
        pass  # no cache to clear, everything reads fresh

    def _get_backups():
        return [{"source": b.source, "redundancy": 1 if not b.mirror else 2}
                for b in getattr(config, "backups", [])]

    def _get_cfg_dict():
        import dataclasses
        if dataclasses.is_dataclass(config) and not isinstance(config, type):
            return dataclasses.asdict(config)
        return vars(config)

    def _config_as_dict():
        """Convert Config dataclass to plain dict for modules that expect dict."""
        import dataclasses
        if dataclasses.is_dataclass(config) and not isinstance(config, type):
            return dataclasses.asdict(config)
        return config

    # ── Page routes ──────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        total_files = index_mod.file_count()
        savings = dedup.total_dedup_savings()
        rpt = verify.get_report()
        report_data = rpt.get("last_integrity_check") if rpt else None
        backends = _get_backends()
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
        return templates.TemplateResponse("dashboard.html", {
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
        backends = _get_backends()
        be_list = [{"name": b.name} for b in backends]
        return templates.TemplateResponse("files.html", {
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
        backends = _get_backends()
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
        return templates.TemplateResponse("backends.html", {
            "request": request,
            "backends": be_list,
        })

    @app.get("/ingest", response_class=HTMLResponse)
    async def ingest_page(request: Request):
        ci = getattr(config, "cpanel_ingest", None)
        ci_dict = {
            "enabled": getattr(ci, "enabled", False),
            "drop_root": getattr(ci, "drop_root", ""),
            "staging_dir": getattr(ci, "staging_dir", "staging"),
            "staging_budget_gb": getattr(ci, "staging_budget_gb", 10.0),
            "keep_raw": getattr(ci, "keep_raw", 0),
        }
        return templates.TemplateResponse("ingest.html", {
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
        return templates.TemplateResponse("logs.html", {
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
        return templates.TemplateResponse("database.html", {
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
        return templates.TemplateResponse("report.html", {
            "request": request,
            "report": report_data,
        })

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        raw = _get_cfg_dict()
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
        sources = _get_backups()
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "config_items": config_items,
            "sources": sources,
        })

    # ── API: Status ──────────────────────────────────────────────

    @app.get("/api/status")
    async def api_status():
        total_files = index_mod.file_count()
        savings = dedup.total_dedup_savings()
        gen = db.get_generation()
        backends = _get_backends()
        
        # Calculate actual stored bytes per backend from DB
        backend_sizes = {}
        try:
            rows = db.execute(
                "SELECT backend_name, SUM(stored_size) as total_size FROM blob_storage GROUP BY backend_name"
            ).fetchall()
            for r in rows:
                backend_sizes[r["backend_name"]] = r["total_size"]
        except Exception:
            pass
            
        be_full = []
        for b in backends:
            be_full.append({
                "name": b.name, 
                "type": b.type, 
                "connected": b.connected,
                "stored_bytes": backend_sizes.get(b.name, 0)
            })
            
        return JSONResponse({
            "total_files": total_files,
            "blob_count": savings.get("total_blobs", 0),
            "stored_bytes": savings.get("stored_bytes", 0),
            "dedup_ratio": savings.get("ratio", 1.0),
            "generation": gen,
            "backends": be_full,
        })


    @app.get("/api/backends")
    async def api_backends():
        backends = _get_backends()
        result = []
        for b in backends:
            free = None
            total = None
            try:
                free = b.free_space()
                total = b.total_space()
            except Exception:
                pass
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
            # Convert numeric fields
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

            _invalidate()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.delete("/api/backends/remove/{name}")
    async def api_backend_remove(name: str):
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
            _invalidate()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.get("/api/backends/test/{name}")
    async def api_backend_test(name: str):
        try:
            be = _get_backend(name)
            if not be:
                return JSONResponse({"connected": False, "error": "not found"})
            ok = be.connect()
            return JSONResponse({"connected": ok, "error": None if ok else "connection failed"})
        except Exception as e:
            return JSONResponse({"connected": False, "error": str(e)})

    @app.get("/api/files")
    async def api_files(q: str = "", backend: str = "", limit: int = 100, offset: int = 0):
        if q:
            all_files = _search_files_safe(q, limit=limit)
        else:
            all_files = index_mod.get_all_files(limit=limit, offset=offset, backend=backend or None)
        if backend and q:
            backend_file_ids = set()
            rows = db.execute(
                "SELECT DISTINCT fc.file_id FROM file_chunks fc "
                "INNER JOIN blob_storage bs ON fc.blob_id = bs.blob_id "
                "WHERE bs.backend_name = ?", (backend,)
            ).fetchall()
            backend_file_ids = {r["file_id"] for r in rows}
            all_files = [f for f in all_files if f.get("id") in backend_file_ids]
        for f in all_files:
            for k in list(f.keys()):
                if isinstance(f[k], bytes):
                    f[k] = f[k].decode("utf-8", errors="replace")
        return JSONResponse(all_files)

    @app.get("/api/browse")
    async def api_browse(root: str = "", path: str = ""):
        try:
            if not root:
                rows = db.execute(
                    "SELECT DISTINCT source_root FROM files WHERE deleted_at IS NULL ORDER BY source_root"
                ).fetchall()
                return JSONResponse({"root": "", "path": "", "drives": [r["source_root"] for r in rows], "dirs": [], "files": []})

            # Rows in the DB may use either separator: the scan worker stores
            # os.sep (backslash on Windows) while cPanel ingest normalizes to
            # forward slashes, so match both prefix styles (qa defect D4).
            base = path.replace("\\", "/").strip("/") if path else ""
            base_bs = base.replace("/", "\\") if base else ""
            like_fs = (base + "/%") if base else "%"
            like_bs = (base_bs + "\\%") if base else "%"

            # Find immediate subdirectories
            rows = db.execute(
                "SELECT DISTINCT relative_path FROM files WHERE source_root = ? "
                "AND (relative_path LIKE ? OR relative_path LIKE ?) AND deleted_at IS NULL",
                (root, like_fs, like_bs),
            ).fetchall()

            dirs_set = set()
            files_list = []
            for r in rows:
                rel = r["relative_path"]
                # Both prefixes have the same length, so slicing by the
                # normalized base works for either separator style.
                remainder = rel[len(base):].lstrip("\\/") if base else rel
                if "\\" in remainder or "/" in remainder:
                    # Has subdirectory
                    subdir = remainder.split("\\")[0].split("/")[0]
                    dirs_set.add(subdir)
                else:
                    # Direct file at this level: keep the LEAF, the full rel
                    # would be double-prefixed in the detail lookup below.
                    files_list.append(remainder)

            # Get file details for direct files (exact match in either style)
            file_details = []
            for fname in sorted(files_list):
                fpath_fs = (base + "/" + fname) if base else fname
                fpath_bs = (base_bs + "\\" + fname) if base else fname
                frow = db.execute(
                    "SELECT id, relative_path, sha256, size, mtime, created_at FROM files "
                    "WHERE source_root = ? AND relative_path IN (?, ?) AND deleted_at IS NULL "
                    "ORDER BY version_seq DESC LIMIT 1",
                    (root, fpath_fs, fpath_bs),
                ).fetchone()
                if frow:
                    file_details.append(dict(frow))

            return JSONResponse({
                "root": root,
                "path": path,
                "dirs": sorted(dirs_set),
                "files": file_details,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/tree")
    async def api_tree(root: str = "", depth: int = 0):
        """Return full tree structure with aggregated stats per folder."""
        try:
            if not root:
                rows = db.execute(
                    "SELECT DISTINCT source_root FROM files WHERE deleted_at IS NULL ORDER BY source_root"
                ).fetchall()
                sources = []
                for r in rows:
                    sr = r["source_root"]
                    stats = db.execute(
                        "SELECT COUNT(*) as cnt, COALESCE(SUM(size),0) as total FROM files WHERE source_root = ? AND deleted_at IS NULL",
                        (sr,)
                    ).fetchone()
                    sources.append({"name": sr, "files": stats["cnt"], "size": stats["total"]})
                return JSONResponse({"type": "roots", "items": sources})

            rows = db.execute(
                "SELECT id, relative_path, sha256, size, mtime, created_at FROM files WHERE source_root = ? AND deleted_at IS NULL ORDER BY relative_path",
                (root,)
            ).fetchall()

            # Build tree structure
            tree: dict = {}
            for r in rows:
                rel = r["relative_path"]
                parts = rel.replace("/", "\\").split("\\")
                node = tree
                for i, part in enumerate(parts[:-1]):
                    if part not in node:
                        node[part] = {"__files": [], "__stats": {"files": 0, "size": 0}}
                    node = node[part]
                fname = parts[-1]
                if "__files" not in node:
                    node["__files"] = []
                node["__files"].append({"id": r["id"], "name": fname, "size": r["size"], "sha256": r["sha256"], "created_at": r["created_at"]})

            def _build_tree(d, prefix=""):
                items = []
                for key in sorted(k for k in d.keys() if k not in ("__files", "__stats")):
                    child = d[key]
                    child_files = child.get("__files", [])
                    child_size = sum(f["size"] for f in child_files)
                    def _count_recursive(node):
                        cnt = 0
                        sz = 0
                        for f in node.get("__files", []):
                            cnt += 1
                            sz += f["size"]
                        for k2 in node:
                            if k2 not in ("__files", "__stats"):
                                c, s = _count_recursive(node[k2])
                                cnt += c
                                sz += s
                        return cnt, sz
                    sub_files_count, sub_size = _count_recursive(child)
                    path = prefix + "\\" + key if prefix else key
                    items.append({
                        "type": "folder",
                        "name": key,
                        "path": path,
                        "files": sub_files_count,
                        "size": sub_size,
                        "children": _build_tree(child, path),
                    })
                for f in d.get("__files", []):
                    items.append({
                        "type": "file",
                        "id": f["id"],
                        "name": f["name"],
                        "size": f["size"],
                        "sha256": f["sha256"],
                        "created_at": f["created_at"],
                    })
                return items

            result = _build_tree(tree)
            return JSONResponse({"type": "tree", "root": root, "items": result})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ── Restore All Files ────────────────────────────────────

    _restore_state = {
        "running": False, "source_root": "", "dest_path": "",
        "total": 0, "done": 0, "errors": 0, "current": "", "error_list": [], "completed": False,
    }
    _restore_lock = threading.Lock()
    _restore_event = threading.Event()

    def _emit_restore():
        _restore_event.set()
        _restore_event.clear()

    def _restore_worker(source_root: str, dest_path: str, path_filter: str):
        rows = db.execute(
            "SELECT id, relative_path, sha256, size FROM files WHERE source_root = ? AND deleted_at IS NULL ORDER BY relative_path",
            (source_root,),
        ).fetchall()

        # Filter to specific subfolder if given
        files = []
        sep = os.sep
        for r in rows:
            rel = r["relative_path"]
            if path_filter:
                # Match exact directory: "DCIM" should match "DCIM/photo.jpg" but not "DCIM2/photo.jpg"
                if not (rel == path_filter or rel.startswith(path_filter + sep) or rel.startswith(path_filter + "\\")):
                    continue
            files.append(dict(r))

        with _restore_lock:
            _restore_state["total"] = len(files)
            _restore_state["running"] = True
        _emit_restore()

        dest = Path(dest_path)
        for f in files:
            rel = f["relative_path"]
            # If path_filter, strip the prefix so files restore relative to the subfolder
            if path_filter:
                if rel.startswith(path_filter + sep) or rel.startswith(path_filter + "\\"):
                    rel = rel[len(path_filter)+1:].lstrip("\\/")
                elif rel == path_filter:
                    rel = os.path.basename(rel)
            # Defense in depth: the index is expected to hold clean relative
            # paths (scan + sanitize_member enforce this), but a poisoned or
            # legacy row must not write outside the restore destination.
            p = Path(rel)
            if ".." in p.parts or p.is_absolute() or (len(rel) >= 2 and rel[1] == ":"):
                with _restore_lock:
                    _restore_state["errors"] += 1
                    if len(_restore_state["error_list"]) < 500:
                        _restore_state["error_list"].append(f"{rel}: unsafe path skipped")
                    _restore_state["done"] += 1
                _emit_restore()
                continue
            out_path = dest / rel
            with _restore_lock:
                _restore_state["current"] = rel
            _emit_restore()
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                downloader_mod.download_file(f["id"], str(out_path))
                with _restore_lock:
                    _restore_state["done"] += 1
                _emit_restore()
            except Exception as e:
                with _restore_lock:
                    _restore_state["errors"] += 1
                    if len(_restore_state["error_list"]) < 500:
                        _restore_state["error_list"].append(f"{rel}: {str(e)[:100]}")
                    _restore_state["done"] += 1
                _emit_restore()

        with _restore_lock:
            _restore_state["running"] = False
            _restore_state["completed"] = True
            _restore_state["current"] = ""
        _emit_restore()

    @app.post("/api/restore/all")
    async def api_restore_all(data: dict = Body(...)):
        source_root = data.get("source_root", "").strip()
        dest_path = data.get("dest_path", "").strip()
        path_filter = data.get("path", "").strip()
        if not source_root or not dest_path:
            return JSONResponse({"error": "source_root and dest_path required"}, status_code=400)

        with _restore_lock:
            if _restore_state["running"]:
                return JSONResponse({"error": "A restore is already running"}, status_code=409)

        # Reset state
        with _restore_lock:
            _restore_state.update({
                "running": False, "source_root": source_root, "dest_path": dest_path,
                "total": 0, "done": 0, "errors": 0, "current": "", "error_list": [], "completed": False,
            })

        t = threading.Thread(target=_restore_worker, args=(source_root, dest_path, path_filter), daemon=True)
        t.start()
        return JSONResponse({"status": "started", "source_root": source_root, "dest_path": dest_path})

    @app.get("/api/restore/stream")
    async def api_restore_stream():
        async def event_generator():
            last = ""
            while True:
                with _restore_lock:
                    snap = json.dumps(_restore_state)
                if snap != last:
                    yield f"data: {snap}\n\n"
                    last = snap
                with _restore_lock:
                    if _restore_state["completed"] and not _restore_state["running"]:
                        yield f"data: {snap}\n\n"
                        break
                await asyncio.sleep(0.5)
        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/restore/status")
    async def api_restore_status():
        with _restore_lock:
            return JSONResponse(_restore_state)

    @app.get("/api/files/{file_id}")
    async def api_file_detail(file_id: int):
        rows = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchall()
        if not rows:
            return JSONResponse({"error": "not found"}, status_code=404)
        f = dict(rows[0])
        chunks = db.execute(
            "SELECT fc.*, bs.backend_name, bs.storage_path, bs.stored_size, bs.stored_sha256, "
            "bs.compression_algo, bs.encrypted "
            "FROM file_chunks fc "
            "LEFT JOIN blob_storage bs ON fc.blob_id = bs.blob_id "
            "WHERE fc.file_id = ? ORDER BY fc.seq",
            (file_id,),
        ).fetchall()
        f["chunks"] = [dict(c) for c in chunks]
        for k in list(f.keys()):
            if isinstance(f[k], bytes):
                f[k] = f[k].decode("utf-8", errors="replace")
        return JSONResponse(f)

    @app.get("/api/sources")
    async def api_sources():
        # Get configured sources from config.yaml
        cfg_sources = {b.source for b in getattr(config, "backups", [])}
        # Get existing source roots in metadata DB
        try:
            rows = db.execute(
                "SELECT DISTINCT source_root FROM files WHERE deleted_at IS NULL"
            ).fetchall()
            db_sources = {r["source_root"] for r in rows}
        except Exception:
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
                
            _reload_config()
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
                
            _reload_config()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


    @app.get("/api/logs")
    async def api_logs(status: str = "", limit: int = 100):
        if status:
            rows = db.execute(
                "SELECT * FROM op_log WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM op_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])

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
        except Exception:
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

    @app.get("/api/report")
    async def api_report():
        return JSONResponse(verify.get_report())

    @app.get("/api/search")
    async def api_search(q: str = "", scope: str = ""):
        if not q:
            return JSONResponse([])
        if scope == "content":
            rows = db.execute(
                "SELECT * FROM files WHERE content_text LIKE ? AND deleted_at IS NULL LIMIT 50",
                (f"%{q}%",),
            ).fetchall()
            return JSONResponse([dict(r) for r in rows])
        # Quoted-phrase MATCH + LIKE fallback: raw user input must never
        # surface as a 500 via an fts5 syntax error (qa defect D3).
        try:
            rows = db.execute(
                "SELECT f.* FROM files f INNER JOIN files_fts ON f.id = files_fts.rowid "
                "WHERE files_fts MATCH ? AND f.deleted_at IS NULL ORDER BY rank LIMIT 50",
                (_fts_phrase(q),),
            ).fetchall()
        except sqlite3.OperationalError:
            return JSONResponse(_search_files_like(q, limit=50))
        return JSONResponse([dict(r) for r in rows])

    # ── API: Post actions ────────────────────────────────────────

    @app.post("/api/backup")
    async def api_trigger_backup():
        backup_key = f"backup:all:{int(time.time())}"
        try:
            db.execute(
                "INSERT INTO op_log (idempotency_key, op_type, target_json, status, started_at) VALUES (?, ?, ?, ?, datetime('now'))",
                (backup_key, "backup", json.dumps({"source": "all"}), "running"),
            )
            db.commit()
            op_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        except Exception:
            op_id = None

        results = []
        from ..core.skip import should_skip, should_skip_dir
        cfg_dict = _config_as_dict()
        for entry in _get_backups():
            source = entry.get("source", "")
            if not source or not os.path.exists(source):
                results.append({"source": source, "status": "skipped"})
                continue
            backends = _get_backends()
            try:
                if os.path.isfile(source):
                    skip, reason = should_skip(source)
                    if skip:
                        results.append({"source": source, "status": "skipped", "reason": reason})
                        continue
                    r = uploader_mod.upload_file(
                        source_root=str(Path(source).parent),
                        relative_path=os.path.basename(source),
                        local_path=source,
                        backends=backends,
                        config=cfg_dict,
                    )
                    results.append({"source": source, **r})
                elif os.path.isdir(source):
                    count = 0
                    for root, dirs, files in os.walk(source):
                        dirs[:] = [d for d in dirs if not should_skip_dir(os.path.join(root, d))]
                        for fn in files:
                            fp = os.path.join(root, fn)
                            skip, reason = should_skip(fp)
                            if skip:
                                continue
                            rel = os.path.relpath(fp, source)
                            uploader_mod.upload_file(
                                source_root=source,
                                relative_path=rel,
                                local_path=fp,
                                backends=backends,
                                config=cfg_dict,
                            )
                            count += 1
                    results.append({"source": source, "status": "uploaded", "file_count": count})
            except Exception as e:
                results.append({"source": source, "status": "error", "error": str(e)})

        if op_id:
            try:
                has_error = any(r.get("status") == "error" for r in results)
                status_str = "failed" if has_error and all(r.get("status") == "error" for r in results) else ("completed" if not has_error else "partial")
                db.execute(
                    "UPDATE op_log SET status = ?, completed_at = datetime('now'), target_json = ? WHERE id = ?",
                    (status_str, json.dumps({"results": results}), op_id),
                )
                db.commit()
            except Exception:
                pass

        return JSONResponse({"status": "completed", "results": results})


    @app.get("/api/download/{file_id}")
    async def api_download(file_id: int):
        import tempfile
        tmp = Path(tempfile.mkdtemp()) / f"dl_{file_id}"
        # Unknown / soft-deleted id: 404 with a JSON error body, never a 500
        # (qa defect D5). Other download failures still map to 500 below.
        row = db.execute(
            "SELECT id FROM files WHERE id = ? AND deleted_at IS NULL", (file_id,)
        ).fetchone()
        if row is None:
            tmp.unlink(missing_ok=True)
            return JSONResponse(
                {"error": f"File id={file_id} not found (or soft-deleted)"},
                status_code=404,
            )
        try:
            result = downloader_mod.download_file(file_id, str(tmp))
            if result["status"] != "ok":
                tmp.unlink(missing_ok=True)
                return JSONResponse({"error": result.get("message", "download failed")}, status_code=500)
            filename = Path(result.get("path", f"file_{file_id}")).name
            return FileResponse(
                path=str(tmp),
                filename=filename,
                media_type="application/octet-stream",
                # A bare lambda is not awaitable: starlette responses require
                # a BackgroundTask (raised TypeError on every download before).
                background=BackgroundTask(tmp.unlink, missing_ok=True),
            )
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/files/{file_id}/preview")
    async def api_file_preview(file_id: int):
        import tempfile
        tmp = Path(tempfile.mkdtemp()) / f"prev_{file_id}"
        # Unknown / soft-deleted id: 404 with a JSON error body, never a 500
        # (qa defect D5). Other read failures still map to 500 below.
        row = db.execute(
            "SELECT id FROM files WHERE id = ? AND deleted_at IS NULL", (file_id,)
        ).fetchone()
        if row is None:
            tmp.unlink(missing_ok=True)
            return JSONResponse(
                {"status": "error", "message": f"File id={file_id} not found (or soft-deleted)"},
                status_code=404,
            )
        try:
            result = downloader_mod.download_file(file_id, str(tmp))
            if result["status"] != "ok":
                tmp.unlink(missing_ok=True)
                return JSONResponse({"error": result.get("message", "failed to read file")}, status_code=500)
            
            size = tmp.stat().st_size
            is_binary = False
            try:
                with open(tmp, "rb") as f:
                    chunk = f.read(1024)
                    if b"\x00" in chunk:
                        is_binary = True
            except Exception:
                is_binary = True
                
            if is_binary:
                tmp.unlink(missing_ok=True)
                return JSONResponse({"status": "binary", "message": "Binary preview not supported (download to view)", "size": size})
                
            try:
                with open(tmp, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(102400) # limit to 100KB
                truncated = size > 102400
                tmp.unlink(missing_ok=True)
                return JSONResponse({
                    "status": "ok",
                    "content": content,
                    "truncated": truncated,
                    "size": size
                })
            except Exception as e:
                tmp.unlink(missing_ok=True)
                return JSONResponse({"status": "error", "message": f"Failed to decode text: {str(e)}"})
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    @app.post("/api/upload/file")
    async def api_upload_file(
        file: UploadFile = File(...),
        backend: str = Form(""),
        redundancy: int = Form(1)
    ):
        import tempfile
        upload_key = f"upload:{file.filename}:{int(time.time())}"
        try:
            db.execute(
                "INSERT INTO op_log (idempotency_key, op_type, target_json, status, started_at) VALUES (?, ?, ?, ?, datetime('now'))",
                (upload_key, "upload", json.dumps({"filename": file.filename, "backend": backend}), "running"),
            )
            db.commit()
            op_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        except Exception:
            op_id = None

        tmp_path = None
        try:
            suffix = Path(file.filename).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                content = await file.read()
                tmp.write(content)
                tmp_path = tmp.name
                
            backends = _get_backends()
            if backend:
                backends = [b for b in backends if b.name == backend]
            
            cfg_dict = _config_as_dict()
            r = uploader_mod.upload_file(
                source_root="web_upload",
                relative_path=file.filename,
                local_path=tmp_path,
                backends=backends,
                config=cfg_dict,
            )
            
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            
            if op_id:
                try:
                    db.execute(
                        "UPDATE op_log SET status = 'completed', completed_at = datetime('now'), target_json = ? WHERE id = ?",
                        (json.dumps({"filename": file.filename, "result": "success", "size": len(content)}), op_id),
                    )
                    db.commit()
                except Exception:
                    pass
            return JSONResponse({"status": "ok", "filename": file.filename, **r})
        except Exception as e:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            if op_id:
                try:
                    db.execute(
                        "UPDATE op_log SET status = 'failed', completed_at = datetime('now'), last_error = ? WHERE id = ?",
                        (str(e), op_id),
                    )
                    db.commit()
                except Exception:
                    pass
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


    @app.post("/api/db/check")
    async def api_db_check():
        errors = db.check_integrity()
        if errors:
            return JSONResponse({"status": "error", "errors": errors})
        return JSONResponse({"status": "ok"})

    @app.post("/api/db/snapshot")
    async def api_db_snapshot():
        snap_key = f"snapshot:{int(time.time())}"
        try:
            db.execute(
                "INSERT INTO op_log (idempotency_key, op_type, target_json, status, started_at) VALUES (?, ?, ?, ?, datetime('now'))",
                (snap_key, "snapshot", json.dumps({}), "running"),
            )
            db.commit()
            op_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        except Exception:
            op_id = None
            
        try:
            result = snapmod.create_snapshot()
            if op_id:
                try:
                    db.execute(
                        "UPDATE op_log SET status = 'completed', completed_at = datetime('now'), target_json = ? WHERE id = ?",
                        (json.dumps(result), op_id),
                    )
                    db.commit()
                except Exception:
                    pass
            return JSONResponse({"status": "ok", **result})
        except Exception as e:
            if op_id:
                try:
                    db.execute(
                        "UPDATE op_log SET status = 'failed', completed_at = datetime('now'), last_error = ? WHERE id = ?",
                        (str(e), op_id),
                    )
                    db.commit()
                except Exception:
                    pass
            return JSONResponse({"status": "error", "message": str(e)})


    @app.post("/api/db/replicate")
    async def api_db_replicate():
        try:
            from ..core.replicator import Replicator
            repl = Replicator(_config_as_dict())
            repl.create_and_upload_snapshot()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/report/generate")
    async def api_report_generate(backend: str = "", count: int = 10):
        report_key = f"report:{backend}:{int(time.time())}"
        try:
            db.execute(
                "INSERT INTO op_log (idempotency_key, op_type, target_json, status, started_at) VALUES (?, ?, ?, ?, datetime('now'))",
                (report_key, "report", json.dumps({"backend": backend, "count": count}), "running"),
            )
            db.commit()
            op_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        except Exception:
            op_id = None
            
        try:
            check_type = "random"
            if backend:
                check_type = f"backend:{backend}"
            result = verify.run_integrity_check(check_type, count=count, backend_name=backend or None)
            if op_id:
                try:
                    db.execute(
                        "UPDATE op_log SET status = 'completed', completed_at = datetime('now'), target_json = ? WHERE id = ?",
                        (json.dumps({"checked_files": result.get("checked_files", 0), "failed_files": result.get("failed_files", 0)}), op_id),
                    )
                    db.commit()
                except Exception:
                    pass
            return JSONResponse({"status": "ok", **result})
        except Exception as e:
            if op_id:
                try:
                    db.execute(
                        "UPDATE op_log SET status = 'failed', completed_at = datetime('now'), last_error = ? WHERE id = ?",
                        (str(e), op_id),
                    )
                    db.commit()
                except Exception:
                    pass
            return JSONResponse({"status": "error", "message": str(e)})


    @app.post("/api/encrypt/init")
    async def api_encrypt_init():
        try:
            key = crypto_mod.generate_key()
            key_path = "backupme.key"
            crypto_mod.save_key(key, key_path)
            return JSONResponse({"status": "ok", "message": f"Encryption key saved to {key_path}"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.get("/health", response_class=HTMLResponse)
    async def health_page(request: Request):
        """Data Health dashboard. Replaced the old JSON ping — the only
        consumers (restart pollers in dashboard/settings) just check r.ok,
        and JSON access to the same data is /api/health/data."""
        return templates.TemplateResponse("health.html", {
            "request": request,
            "summary": data_health.summary(config),
        })

    # ── API: New endpoints ─────────────────────────────────────

    @app.get("/api/activity")
    async def api_activity(limit: int = 20):
        rows = db.execute(
            "SELECT * FROM op_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        if not rows:
            files = index_mod.get_all_files(limit=limit)
            return JSONResponse([{
                "op_type": "upload",
                "target_json": json.dumps({"file": f["relative_path"], "size": f["size"]}),
                "status": "completed",
                "started_at": f["created_at"],
            } for f in files])
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/stats/trend")
    async def api_stats_trend(days: int = 7):
        try:
            rows = db.execute(
                "SELECT DATE(started_at) as date, COUNT(*) as count "
                "FROM op_log WHERE started_at IS NOT NULL "
                "GROUP BY DATE(started_at) ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
            result = [{"date": r["date"], "count": r["count"]} for r in rows]
            result.reverse()
            return JSONResponse(result)
        except Exception:
            return JSONResponse([])

    # NOTE: registered before /api/backends/{name} so "usage" is not captured.
    @app.get("/api/backends/usage")
    async def api_backends_usage():
        from ..core import rebalance as rb
        usage = {u["backend_name"]: u for u in rb.backend_usage()}
        out = []
        for be in _get_backends():
            free = total = None
            try:
                free, total = be.free_space(), be.total_space()
            except Exception:
                pass
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
        be = _get_backend(name)
        if not be:
            return JSONResponse({"error": "not found"}, status_code=404)
        free = total = None
        try:
            free = be.free_space()
            total = be.total_space()
        except Exception:
            pass
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

    @app.get("/api/health/detailed")
    async def api_health_detailed():
        try:
            # 1. Database details & integrity
            gen = db.get_instance_id()
            integrity = db.check_integrity()
            total_files = index_mod.file_count()
            savings = dedup.total_dedup_savings()
            db_status = "ok" if not integrity else "corrupt"
            
            # 2. Daemon status
            daemon_running = _scheduler is not None and _scheduler._thread is not None and _scheduler._thread.is_alive()
            
            # 3. Backends status
            backends_list = []
            backends_ok = True
            for be in _get_backends():
                conn_ok = False
                free = total = None
                try:
                    conn_ok = be.connected or be.connect()
                    if conn_ok:
                        free = be.free_space()
                        total = be.total_space()
                except Exception:
                    pass
                if not conn_ok:
                    backends_ok = False
                backends_list.append({
                    "name": be.name,
                    "type": be.type,
                    "connected": conn_ok,
                    "free_space": free,
                    "total_space": total,
                    "priority": be.config.get("priority", 50),
                })
            
            # 4. Replication summary
            total_blobs = 0
            fully_replicated = 0
            single_copy = 0
            try:
                rows = db.execute(
                    "SELECT bs.blob_id, bs.backend_name FROM blob_storage bs"
                ).fetchall()
                blob_map = {}
                for r in rows:
                    bid = r["blob_id"]
                    blob_map.setdefault(bid, []).append(r["backend_name"])
                total_blobs = len(blob_map)
                fully_replicated = sum(1 for b in blob_map.values() if len(b) >= 2)
                single_copy = sum(1 for b in blob_map.values() if len(b) == 1)
            except Exception:
                pass
            
            # 5. Active scan status
            with _scan_lock:
                scan_active = _scan_state["running"]
                scan_errors_count = len(_scan_state["errors"])
                scan_progress = {
                    "running": scan_active,
                    "files_total": _scan_state["files_total"],
                    "files_done": _scan_state["files_done"],
                    "files_error": _scan_state["files_error"],
                }
            
            # 6. Database snapshot replication check
            db_repl_status = {}
            for be in _get_backends():
                try:
                    snaps = be.db_list("snapshots")
                    db_repl_status[be.name] = {
                        "snapshots_count": len(snaps) if snaps else 0,
                        "replicated": bool(snaps),
                    }
                except Exception as e:
                    db_repl_status[be.name] = {
                        "snapshots_count": 0,
                        "replicated": False,
                        "error": str(e),
                    }
            
            # Compile warnings/issues
            issues = []
            if integrity:
                issues.append(f"Database integrity check failed: {', '.join(integrity)}")
            if not daemon_running:
                issues.append("Background scheduler daemon is not running")
            if not backends_ok:
                issues.append("One or more storage backends are disconnected")
            for be_name, status in db_repl_status.items():
                if not status["replicated"]:
                    issues.append(f"Database snapshot not replicated to backend '{be_name}'")
            
            overall = "perfect" if not issues else "warning" if len(issues) < 3 else "danger"
            
            return JSONResponse({
                "status": "ok" if overall == "perfect" else overall,
                "overall_health": overall,
                "issues": issues,
                "generation": db.get_generation(),
                "instance_id": gen,
                "integrity": db_status,
                "total_files": total_files,
                "stored_bytes": savings.get("stored_bytes", 0),
                "dedup_ratio": savings.get("ratio", 1.0),
                "daemon_running": daemon_running,
                "backends": backends_list,
                "replication_summary": {
                    "total_blobs": total_blobs,
                    "fully_replicated": fully_replicated,
                    "single_copy": single_copy,
                },
                "active_scan": scan_progress,
                "db_replication_check": db_repl_status,
            })
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    # ── API: Daemon ─────────────────────────────────────────────

    _scheduler = None

    @app.post("/api/daemon/start")
    async def api_daemon_start():
        nonlocal _scheduler
        try:
            from ..scheduler import BackupScheduler
            import dataclasses
            cfg = config
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

    # ── API: Server Restart ─────────────────────────────────────

    @app.post("/api/server/restart")
    async def api_server_restart():
        def _do_restart():
            time.sleep(1)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=True).start()
        return JSONResponse({"status": "restarting", "message": "Server restarting in 1 second..."})

    # ── API: DB Restore/Rebuild ─────────────────────────────────

    @app.post("/api/db/restore")
    async def api_db_restore():
        try:
            result = recover_mod.recover_database(_get_backends())
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/db/rebuild")
    async def api_db_rebuild():
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

    # ── API: File Delete ────────────────────────────────────────

    def _soft_delete_file(file_id: int) -> dict:
        """Soft-delete one file row and drop one blob ref per chunk.

        Shared by DELETE /api/files/{file_id} and POST /api/health/purge —
        this is the only soft-delete path; it never hard-deletes content
        (rows stay until the purge grace window and GC apply the ref drop).
        """
        row = db.execute(
            "SELECT id, relative_path FROM files WHERE id = ? AND deleted_at IS NULL",
            (file_id,),
        ).fetchone()
        if not row:
            return {"status": "error", "message": "File not found", "not_found": True}
        db.execute("UPDATE files SET deleted_at = datetime('now') WHERE id = ?", (file_id,))
        chunks = db.execute("SELECT blob_id FROM file_chunks WHERE file_id = ?", (file_id,)).fetchall()
        for c in chunks:
            db.execute("UPDATE blobs SET ref_count = ref_count - 1 WHERE blob_id = ? AND ref_count > 0", (c["blob_id"],))
        db.commit()
        return {"status": "ok", "message": f"Deleted {row['relative_path']}"}

    @app.delete("/api/files/{file_id}")
    async def api_file_delete(file_id: int):
        try:
            outcome = _soft_delete_file(file_id)
            if outcome.get("not_found"):
                return JSONResponse(
                    {"status": "error", "message": outcome["message"]}, status_code=404)
            return JSONResponse(outcome)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # ── API: File Versions ──────────────────────────────────────

    @app.get("/api/files/{file_id}/versions")
    async def api_file_versions(file_id: int):
        try:
            row = db.execute("SELECT version_group FROM files WHERE id = ?", (file_id,)).fetchone()
            if not row:
                return JSONResponse({"error": "not found"}, status_code=404)
            vg = row["version_group"]
            if not vg:
                return JSONResponse([])
            versions = db.execute(
                "SELECT id, relative_path, size, sha256, version_seq, created_at, deleted_at "
                "FROM files WHERE version_group = ? ORDER BY version_seq DESC",
                (vg,),
            ).fetchall()
            return JSONResponse([dict(v) for v in versions])
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ── API: File Verification ──────────────────────────────────

    @app.post("/api/files/{file_id}/verify")
    async def api_file_verify(file_id: int):
        try:
            result = await asyncio.to_thread(verify.verify_file_integrity, file_id)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    # ── API: Report History ─────────────────────────────────────

    @app.get("/api/reports")
    async def api_reports(limit: int = 20):
        try:
            rows = db.execute(
                "SELECT * FROM integrity_checks ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return JSONResponse([dict(r) for r in rows])
        except Exception as e:
            return JSONResponse([])

    @app.get("/api/reports/{report_id}")
    async def api_report_detail(report_id: int):
        try:
            row = db.execute("SELECT * FROM integrity_checks WHERE id = ?", (report_id,)).fetchone()
            if not row:
                return JSONResponse({"error": "not found"}, status_code=404)
            r = dict(row)
            if r.get("details"):
                try:
                    import json as _json
                    r["details"] = _json.loads(r["details"])
                except Exception:
                    pass
            return JSONResponse(r)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ── API: Config Edit ────────────────────────────────────────

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
            _invalidate()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # ── API: Convert/Recompress ─────────────────────────────────

    @app.post("/api/convert/{backend_name}")
    async def api_convert(backend_name: str, prefix: str = "", delete: bool = False, target: str = ""):
        try:
            from ..core.converter import convert_backend
            result = convert_backend(
                backend_name=backend_name,
                prefix=prefix,
                target_backend_name=target or None,
                delete=delete,
                resume=True,
            )
            return JSONResponse({"status": "ok", **result})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/recompress")
    async def api_recompress():
        try:
            from ..core import compress as compress_mod
            import tempfile
            rows = db.execute(
                "SELECT bs.blob_id, bs.storage_path, bs.backend_name, bs.stored_size, bs.compression_algo "
                "FROM blob_storage bs WHERE bs.compression_algo != 'zstd' LIMIT 100"
            ).fetchall()
            converted = 0
            for row in rows:
                tmp_dir = None
                try:
                    be = _get_backend(row["backend_name"])
                    if not be:
                        continue
                    tmp_dir = Path(tempfile.mkdtemp())
                    tmp = tmp_dir / "blob"
                    be.download(row["storage_path"], str(tmp))
                    if tmp.stat().st_size == 0:
                        continue
                    data = tmp.read_bytes()
                    new_data = compress_mod.compress(data)
                    del data
                    tmp.write_bytes(new_data)
                    new_hash = hasher_mod.sha256_file(str(tmp))
                    be.upload(str(tmp), row["storage_path"])
                    db.execute(
                        "UPDATE blob_storage SET compression_algo = 'zstd', stored_size = ?, stored_sha256 = ? WHERE blob_id = ? AND backend_name = ?",
                        (len(new_data), new_hash, row["blob_id"], row["backend_name"]),
                    )
                    converted += 1
                except Exception:
                    continue
                finally:
                    if tmp_dir:
                        import shutil
                        shutil.rmtree(str(tmp_dir), ignore_errors=True)
            db.commit()
            return JSONResponse({"status": "ok", "converted": converted})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # ── API: Encrypt Status ─────────────────────────────────────

    @app.get("/api/encrypt/status")
    async def api_encrypt_status():
        key_path = Path("backupme.key")
        exists = key_path.exists()
        return JSONResponse({
            "key_exists": exists,
            "key_path": str(key_path),
            "key_size": key_path.stat().st_size if exists else 0,
        })

    # ── API: Hash Lookup ────────────────────────────────────────

    @app.get("/api/hash/{sha256}")
    async def api_hash_lookup(sha256: str):
        try:
            rows = db.execute(
                "SELECT f.* FROM files f WHERE f.sha256 = ? AND f.deleted_at IS NULL LIMIT 20",
                (sha256,),
            ).fetchall()
            if not rows:
                rows = db.execute(
                    "SELECT f.* FROM files f WHERE f.sha256 LIKE ? AND f.deleted_at IS NULL LIMIT 20",
                    (f"{sha256}%",),
                ).fetchall()
            return JSONResponse([dict(r) for r in rows])
        except Exception as e:
            return JSONResponse([])

    # ── API: Backend Edit ────────────────────────────────────────

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
            _invalidate()
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # ── API: Per-source backup ───────────────────────────────────

    @app.post("/api/backup/source")
    async def api_backup_source(data: dict = Body(...)):
        source = data.get("source", "")
        if not source or not os.path.exists(source):
            return JSONResponse({"status": "error", "message": "Source not found"})
            
        backup_key = f"backup:{source}:{int(time.time())}"
        try:
            db.execute(
                "INSERT INTO op_log (idempotency_key, op_type, target_json, status, started_at) VALUES (?, ?, ?, ?, datetime('now'))",
                (backup_key, "backup", json.dumps({"source": source}), "running"),
            )
            db.commit()
            op_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        except Exception:
            op_id = None

        from ..core.skip import should_skip, should_skip_dir
        cfg_dict = _config_as_dict()
        backends = _get_backends()
        try:
            if os.path.isfile(source):
                r = uploader_mod.upload_file(
                    source_root=str(Path(source).parent),
                    relative_path=os.path.basename(source),
                    local_path=source,
                    backends=backends,
                    config=cfg_dict,
                )
                if op_id:
                    try:
                        db.execute(
                            "UPDATE op_log SET status = 'completed', completed_at = datetime('now'), target_json = ? WHERE id = ?",
                            (json.dumps({"source": source, "result": "success", "file_count": 1}), op_id),
                        )
                        db.commit()
                    except Exception:
                        pass
                return JSONResponse({"status": "ok", **r})
            elif os.path.isdir(source):
                count = 0
                for root, dirs, files in os.walk(source):
                    dirs[:] = [d for d in dirs if not should_skip_dir(os.path.join(root, d))]
                    for fn in files:
                        fp = os.path.join(root, fn)
                        skip, reason = should_skip(fp)
                        if skip:
                            continue
                        rel = os.path.relpath(fp, source)
                        uploader_mod.upload_file(
                            source_root=source,
                            relative_path=rel,
                            local_path=fp,
                            backends=backends,
                            config=cfg_dict,
                        )
                        count += 1
                if op_id:
                    try:
                        db.execute(
                            "UPDATE op_log SET status = 'completed', completed_at = datetime('now'), target_json = ? WHERE id = ?",
                            (json.dumps({"source": source, "result": "success", "file_count": count}), op_id),
                        )
                        db.commit()
                    except Exception:
                        pass
                return JSONResponse({"status": "ok", "file_count": count})
        except Exception as e:
            if op_id:
                try:
                    db.execute(
                        "UPDATE op_log SET status = 'failed', completed_at = datetime('now'), last_error = ? WHERE id = ?",
                        (str(e), op_id),
                    )
                    db.commit()
                except Exception:
                    pass
            return JSONResponse({"status": "error", "message": str(e)})


    # ── API: Snapshot management ─────────────────────────────────

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
    async def api_snapshot_delete(name: str):
        try:
            p = Path(name)
            if not p.exists() or not p.name.startswith("snapshot-"):
                return JSONResponse({"status": "error", "message": "Snapshot not found"})
            p.unlink(missing_ok=True)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    @app.post("/api/db/snapshots/prune")
    async def api_snapshot_prune(keep: int = 5):
        try:
            snaps = sorted(Path(".").glob("snapshot-*.db.zst"), key=lambda p: p.stat().st_mtime, reverse=True)
            deleted = 0
            for p in snaps[keep:]:
                p.unlink(missing_ok=True)
                deleted += 1
            return JSONResponse({"status": "ok", "deleted": deleted, "kept": min(len(snaps), keep)})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # ── API: Log export ──────────────────────────────────────────

    @app.get("/api/logs/export")
    async def api_logs_export(fmt: str = "json", limit: int = 500):
        rows = db.execute(
            "SELECT * FROM op_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        logs = [dict(r) for r in rows]
        if fmt == "csv":
            import csv
            buf = io.StringIO()
            if logs:
                writer = csv.DictWriter(buf, fieldnames=logs[0].keys())
                writer.writeheader()
                writer.writerows(logs)
            return StreamingResponse(
                io.BytesIO(buf.getvalue().encode("utf-8")),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="logs.csv"'},
            )
        return StreamingResponse(
            io.BytesIO(json.dumps(logs, indent=2, default=str).encode("utf-8")),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="logs.json"'},
        )

    # ── API: Storage growth trend ────────────────────────────────

    @app.get("/api/stats/storage-trend")
    async def api_storage_trend(days: int = 30):
        try:
            rows = db.execute(
                "SELECT DATE(started_at) as date, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed, "
                "COUNT(*) as total FROM op_log WHERE started_at IS NOT NULL "
                "GROUP BY DATE(started_at) ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
            result = [{"date": r["date"], "completed": r["completed"], "total": r["total"]} for r in rows]
            result.reverse()
            return JSONResponse(result)
        except Exception:
            return JSONResponse([])

    # ── API: Config Import ───────────────────────────────────────

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
            _invalidate()
            return JSONResponse({"status": "ok", "message": "Config imported successfully"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # ── API: Scheduler Config ────────────────────────────────────

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
        except Exception:
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
            _invalidate()
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

    # ── API: Replication Status ──────────────────────────────────

    @app.get("/api/replication/status")
    async def api_replication_status():
        try:
            rows = db.execute(
                "SELECT bs.blob_id, bs.backend_name, bs.stored_size, b.ref_count "
                "FROM blob_storage bs LEFT JOIN blobs b ON bs.blob_id = b.blob_id "
                "ORDER BY bs.blob_id"
            ).fetchall()
            blob_map = {}
            for r in rows:
                bid = r["blob_id"]
                if bid not in blob_map:
                    blob_map[bid] = {"blob_id": bid, "ref_count": r["ref_count"] or 0, "backends": [], "stored_size": r["stored_size"] or 0}
                blob_map[bid]["backends"].append(r["backend_name"])
            total_blobs = len(blob_map)
            fully_replicated = sum(1 for b in blob_map.values() if len(b["backends"]) >= 2)
            single_copy = sum(1 for b in blob_map.values() if len(b["backends"]) == 1)
            backend_counts = {}
            for b in blob_map.values():
                for be in b["backends"]:
                    backend_counts[be] = backend_counts.get(be, 0) + 1
            return JSONResponse({
                "total_blobs": total_blobs,
                "fully_replicated": fully_replicated,
                "single_copy": single_copy,
                "per_backend": backend_counts,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/files/{file_id}/replication")
    async def api_file_replication(file_id: int):
        try:
            chunks = db.execute(
                "SELECT fc.blob_id, fc.seq FROM file_chunks fc WHERE fc.file_id = ? ORDER BY fc.seq",
                (file_id,),
            ).fetchall()
            result = []
            for c in chunks:
                stores = db.execute(
                    "SELECT backend_name, stored_size, compression_algo, encrypted FROM blob_storage WHERE blob_id = ?",
                    (c["blob_id"],),
                ).fetchall()
                result.append({
                    "seq": c["seq"],
                    "blob_id": c["blob_id"],
                    "copies": [dict(s) for s in stores],
                    "copy_count": len(stores),
                })
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ── API: Download to Server Path ─────────────────────────────

    @app.post("/api/download/{file_id}/to-path")
    async def api_download_to_path(file_id: int, data: dict = Body(...)):
        dest = data.get("path", "")
        if not dest:
            return JSONResponse({"status": "error", "message": "Destination path required"})
        try:
            dest_path = Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            result = downloader_mod.download_file(file_id, str(dest_path))
            if result["status"] != "ok":
                return JSONResponse({"status": "error", "message": result.get("message", "download failed")})
            return JSONResponse({"status": "ok", "path": str(dest_path), "size": dest_path.stat().st_size})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # ── Scan Drive ───────────────────────────────────────────────

    _scan_state = {
        "running": False,
        "path": "",
        "started_at": 0,
        "files_total": 0,
        "files_done": 0,
        "files_skipped": 0,
        "files_error": 0,
        "bytes_total": 0,
        "bytes_done": 0,
        "current_file": "",
        "errors": [],
        "completed": False,
        "results": [],
    }
    _scan_lock = threading.Lock()
    _scan_event = threading.Event()

    def _reset_scan():
        with _scan_lock:
            _scan_state.update({
                "running": False, "path": "", "started_at": 0,
                "files_total": 0, "files_done": 0, "files_skipped": 0,
                "files_error": 0, "bytes_total": 0, "bytes_done": 0,
                "current_file": "", "errors": [], "completed": False,
                "results": [],
            })

    def _emit_scan():
        _scan_event.set()
        _scan_event.clear()

    def _safe_stat(p: str):
        """Stat a path, trying with UNC prefix on Windows for long/special paths."""
        try:
            return os.stat(p)
        except OSError:
            lp = _lp(p)
            try:
                return os.stat(lp)
            except OSError:
                return None

    def _lp(p: str) -> str:
        """Apply UNC long-path prefix on Windows."""
        if sys.platform == "win32" and not p.startswith("\\\\?\\"):
            return "\\\\?\\" + os.path.abspath(p)
        return p

    def _scan_worker(path: str, backend_name: str | None, redundancy: int,
                     excludes: list[str], delete_source: bool):
        from ..core.skip import should_skip, should_skip_dir
        import fnmatch
        cfg_dict = _config_as_dict()

        if backend_name:
            targets = [b for b in _get_backends() if b.name == backend_name]
        else:
            targets = _get_backends()

        if not targets:
            with _scan_lock:
                _scan_state["errors"].append("No backends available")
                _scan_state["running"] = False
                _scan_state["completed"] = True
            _emit_scan()
            return

        # Phase 1: Count files
        with _scan_lock:
            _scan_state["running"] = True
            _scan_state["path"] = path
            _scan_state["started_at"] = time.time()
        _emit_scan()

        scan_key = f"scan:{path}:{int(time.time())}"
        try:
            db.execute(
                "INSERT INTO op_log (idempotency_key, op_type, target_json, status, started_at) VALUES (?, ?, ?, ?, datetime('now'))",
                (scan_key, "scan", json.dumps({"path": path, "backend": backend_name}), "running"),
            )
            db.commit()
            scan_op_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        except Exception:
            scan_op_id = None

        all_files = []  # list of (display_path, fs_path) tuples
        abs_src = os.path.abspath(path)
        long_src = _lp(abs_src)

        if os.path.isfile(long_src):
            all_files.append((abs_src, long_src))
        elif os.path.isdir(long_src):
            for dirpath, dirnames, filenames in os.walk(long_src):
                # Convert walk dirpath back to display form for skip checks
                disp_dir = dirpath
                if dirpath.startswith("\\\\?\\"):
                    disp_dir = dirpath[4:]
                dirnames[:] = [d for d in dirnames
                               if not should_skip_dir(os.path.join(disp_dir, d))
                               and not any(fnmatch.fnmatch(d, pat) for pat in excludes)]
                for fn in filenames:
                    fs_path = os.path.join(dirpath, fn)
                    disp_path = os.path.join(disp_dir, fn)
                    all_files.append((disp_path, fs_path))

        total_bytes = 0
        for _, fsp in all_files:
            st = _safe_stat(fsp)
            if st:
                total_bytes += st.st_size

        with _scan_lock:
            _scan_state["files_total"] = len(all_files)
            _scan_state["bytes_total"] = total_bytes
        _emit_scan()

        # Phase 2: Upload files
        for disp_path, fs_path in all_files:
            # Compute relative path from original source
            if os.path.isdir(long_src):
                if disp_path.startswith(abs_src):
                    rel = disp_path[len(abs_src):].lstrip(os.sep)
                else:
                    rel = os.path.basename(disp_path)
            else:
                rel = os.path.basename(disp_path)

            # Check exists via stat (handles special chars)
            st = _safe_stat(fs_path)
            if not st or stat.S_ISDIR(st.st_mode):
                continue

            if excludes and any(
                fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(disp_path), pat)
                or fnmatch.fnmatch(rel.split(os.sep)[0], pat) or rel.startswith(pat + os.sep) or rel.startswith(pat + "/")
                for pat in excludes
            ):
                with _scan_lock:
                    _scan_state["files_skipped"] += 1
                    _scan_state["files_done"] += 1
                _emit_scan()
                continue

            skip, reason = should_skip(disp_path)
            if skip:
                with _scan_lock:
                    _scan_state["files_skipped"] += 1
                    _scan_state["files_done"] += 1
                _emit_scan()
                continue

            try:
                fsize = st.st_size
                with _scan_lock:
                    _scan_state["current_file"] = rel
                _emit_scan()

                uploader_mod.upload_file(
                    source_root=abs_src if os.path.isdir(long_src) else os.path.dirname(abs_src),
                    relative_path=rel,
                    local_path=fs_path,
                    backends=targets,
                    config=cfg_dict,
                    redundancy=redundancy,
                )

                if delete_source:
                    try:
                        os.remove(fs_path)
                    except OSError:
                        try:
                            os.remove(disp_path)
                        except OSError:
                            pass

                with _scan_lock:
                    _scan_state["files_done"] += 1
                    _scan_state["bytes_done"] += fsize
                    if len(_scan_state["results"]) < 5000:
                        _scan_state["results"].append({"file": rel, "size": fsize, "status": "ok"})
                _emit_scan()

            except Exception as e:
                with _scan_lock:
                    _scan_state["files_done"] += 1
                    _scan_state["files_error"] += 1
                    _scan_state["bytes_done"] += (st.st_size if st else 0)
                    if len(_scan_state["errors"]) < 500:
                        _scan_state["errors"].append(f"{rel}: {str(e)[:120]}")
                    if len(_scan_state["results"]) < 5000:
                        _scan_state["results"].append({"file": rel, "status": "error", "error": str(e)[:120]})
                _emit_scan()

        with _scan_lock:
            _scan_state["running"] = False
            _scan_state["completed"] = True
            _scan_state["current_file"] = ""

        done = _scan_state["files_done"]
        errs = _scan_state["files_error"]
        skip = _scan_state["files_skipped"]
        detail = f"{done - errs - skip} uploaded, {errs} errors, {skip} skipped"
        if scan_op_id:
            try:
                db.execute(
                    "UPDATE op_log SET status = ?, completed_at = datetime('now'), last_error = ? WHERE id = ?",
                    ("completed" if errs == 0 else "partial", detail, scan_op_id),
                )
                db.commit()
            except Exception:
                pass
        _emit_scan()

    @app.post("/api/backup/scan")
    async def api_backup_scan(data: dict = Body(...)):
        path = data.get("path", "").strip()
        if not path:
            return JSONResponse({"error": "Path is required"}, status_code=400)
        if not os.path.exists(path):
            return JSONResponse({"error": f"Path not found: {path}"}, status_code=404)

        with _scan_lock:
            if _scan_state["running"]:
                return JSONResponse({"error": "A scan is already running"}, status_code=409)

        backend_name = data.get("backend") or None
        redundancy = int(data.get("redundancy", 1))
        excludes = data.get("exclude", [])
        delete_source = bool(data.get("delete_source", False))

        _reset_scan()
        t = threading.Thread(
            target=_scan_worker,
            args=(path, backend_name, redundancy, excludes, delete_source),
            daemon=True,
        )
        t.start()
        return JSONResponse({"status": "started", "path": path})

    @app.get("/api/backup/scan/stream")
    async def api_backup_scan_stream():
        async def event_generator():
            last_snapshot = ""
            while True:
                with _scan_lock:
                    snapshot = json.dumps(_scan_state)
                if snapshot != last_snapshot:
                    yield f"data: {snapshot}\n\n"
                    last_snapshot = snapshot
                with _scan_lock:
                    if _scan_state["completed"] and not _scan_state["running"]:
                        yield f"data: {snapshot}\n\n"
                        break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/backup/scan/status")
    async def api_backup_scan_status():
        with _scan_lock:
            return JSONResponse(_scan_state)

    # ── cPanel Ingest ────────────────────────────────────────────

    _ingest_state = {
        "running": False,
        "drop_root": "",
        "started_at": 0,
        "current_archive": "",
        "current_member": "",
        "members_done": 0,
        "members_total": 0,
        "archives_done": 0,
        "archives_total": 0,
        "errors": [],
        "completed": False,
        "results": [],
    }
    _ingest_lock = threading.Lock()
    _ingest_paused = threading.Event()

    def _reset_ingest():
        with _ingest_lock:
            _ingest_state.update({
                "running": False, "drop_root": "", "started_at": 0,
                "current_archive": "", "current_member": "",
                "members_done": 0, "members_total": 0,
                "archives_done": 0, "archives_total": 0,
                "errors": [], "completed": False, "results": [],
            })

    def _ingest_progress_cb(member, done, total):
        with _ingest_lock:
            _ingest_state["current_member"] = member
            _ingest_state["members_done"] = done
            _ingest_state["members_total"] = total

    def _ingest_worker(drop_root: str):
        try:
            from ..core import cpanel_ingest as ci_mod
            cfg_dict = _config_as_dict()
            with _ingest_lock:
                _ingest_state["running"] = True
                _ingest_state["started_at"] = time.time()
                _ingest_state["drop_root"] = drop_root
            backends = _get_backends()
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
                except Exception:
                    pass
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
        ci = getattr(config, "cpanel_ingest", None)
        staging_dir = Path(getattr(ci, "staging_dir", "staging") or "staging")
        staging_bytes = 0
        if staging_dir.exists():
            for dirpath, _dirs, filenames in os.walk(staging_dir):
                for fn in filenames:
                    try:
                        staging_bytes += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
        db_path = Path(str(db.DB_PATH))
        db_size = db_path.stat().st_size if db_path.exists() else 0

        from ..core import db_lifecycle as lc
        pdir = lc.parquet_dir_for(_config_as_dict())
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
        ci = getattr(config, "cpanel_ingest", None)
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
        threading.Thread(target=_ingest_worker, args=(drop_root,), daemon=True).start()
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
        async def event_generator():
            last_snapshot = ""
            while True:
                with _ingest_lock:
                    snap = dict(_ingest_state)
                # The pause Event is the single source of truth; the state
                # dict never stores it (a stale copy would mislead SSE
                # consumers into reading "paused": false mid-pause).
                snap["paused"] = _ingest_paused.is_set()
                snapshot = json.dumps(snap)
                if snapshot != last_snapshot:
                    yield f"data: {snapshot}\n\n"
                    last_snapshot = snapshot
                with _ingest_lock:
                    done = _ingest_state["completed"] and not _ingest_state["running"]
                if done:
                    yield f"data: {snapshot}\n\n"
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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
        from ..core import db_lifecycle as lc
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
        except Exception:
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
            from ..core import cpanel_ingest as ci_mod
            result = ci_mod.restore_cpanel(
                account, generation, member_filter=member_filter, out=out,
                retar=retar,
                backends=_get_backends(), config=_config_as_dict())
            if result.get("status") == "error":
                return JSONResponse(result, status_code=404)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    # ── Backends: onboard / rebalance / drain ────────────────────

    _rebalance_state = {
        "running": False,
        "kind": "",          # rebalance | drain
        "backend": "",
        "done": 0,
        "total": 0,
        "current_blob": "",
        "errors": [],
        "completed": False,
        "result": None,
    }
    _rebalance_lock = threading.Lock()

    def _reset_rebalance(kind, backend):
        with _rebalance_lock:
            _rebalance_state.update({
                "running": True, "kind": kind, "backend": backend,
                "done": 0, "total": 0, "current_blob": "",
                "errors": [], "completed": False, "result": None,
            })

    def _rebalance_progress_cb(blob_id, done, total):
        with _rebalance_lock:
            _rebalance_state["current_blob"] = blob_id
            _rebalance_state["done"] = done
            _rebalance_state["total"] = total

    def _rebalance_worker(kind, backend_name):
        try:
            from ..core import rebalance as rb
            cfg_dict = _config_as_dict()
            backends = _get_backends()
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
        be = _get_backend(name)
        if be is None:
            return JSONResponse({"error": f"backend '{name}' not found or not connectable"}, status_code=404)
        free = total = None
        try:
            free, total = be.free_space(), be.total_space()
        except Exception:
            pass
        result = {"name": name, "type": be.type, "connected": be.connected,
                  "free_space": free, "total_space": total}
        if not data.get("rebalance"):
            return JSONResponse({"status": "ok", **result})
        with _rebalance_lock:
            if _rebalance_state["running"]:
                return JSONResponse({"error": "a rebalance operation is already running"}, status_code=409)
        from ..core import rebalance as rb
        result["plan"] = rb.plan_rebalance(name)
        _reset_rebalance("rebalance", name)
        threading.Thread(target=_rebalance_worker, args=("rebalance", name), daemon=True).start()
        return JSONResponse({"status": "started", **result})

    @app.post("/api/backends/{name}/rebalance")
    async def api_backend_rebalance(name: str, data: dict = Body(default={})):
        if _get_backend(name) is None:
            return JSONResponse({"error": f"backend '{name}' not found"}, status_code=404)
        with _rebalance_lock:
            if _rebalance_state["running"]:
                return JSONResponse({"error": "a rebalance operation is already running"}, status_code=409)
        limit = data.get("limit")
        _reset_rebalance("rebalance", name)
        threading.Thread(target=_rebalance_worker, args=("rebalance", name), daemon=True).start()
        return JSONResponse({"status": "started", "backend": name, "limit": limit})

    @app.post("/api/backends/{name}/drain")
    async def api_backend_drain(name: str):
        if _get_backend(name) is None:
            return JSONResponse({"error": f"backend '{name}' not found"}, status_code=404)
        with _rebalance_lock:
            if _rebalance_state["running"]:
                return JSONResponse({"error": "a rebalance operation is already running"}, status_code=409)
        _reset_rebalance("drain", name)
        threading.Thread(target=_rebalance_worker, args=("drain", name), daemon=True).start()
        return JSONResponse({"status": "started", "backend": name})

    @app.get("/api/backends/rebalance/stream")
    async def api_backends_rebalance_stream():
        async def event_generator():
            last_snapshot = ""
            while True:
                with _rebalance_lock:
                    snapshot = json.dumps(_rebalance_state)
                if snapshot != last_snapshot:
                    yield f"data: {snapshot}\n\n"
                    last_snapshot = snapshot
                with _rebalance_lock:
                    done = _rebalance_state["completed"] and not _rebalance_state["running"]
                if done:
                    yield f"data: {snapshot}\n\n"
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Data Health: live buckets + physical reachability scan ──

    _health_scan_state = {
        "running": False,
        "started_at": 0.0,
        "finished_at": 0.0,
        "completed": False,
        "results": {},   # per-backend dicts for local backends
        "remote": [],    # [{"name", "type", "note": "not scanned (remote)"}]
        "errors": [],    # worker-level errors; per-backend errors live in results
    }
    _health_scan_lock = threading.Lock()
    _health_scan_event = threading.Event()

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
            backends = _get_backends()
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

    @app.get("/api/health/data")
    async def api_health_data():
        with _health_scan_lock:
            scan_snapshot = json.loads(json.dumps(_health_scan_state))
            finished = _health_scan_state["finished_at"]
            running = _health_scan_state["running"]
        scan_age = max(0.0, time.time() - finished) if finished else None
        return JSONResponse({
            "summary": data_health.summary(config),
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
        threading.Thread(target=_health_scan_worker, daemon=True).start()
        return JSONResponse({"status": "started"})

    @app.get("/api/health/scan/stream")
    async def api_health_scan_stream():
        async def event_generator():
            last_snapshot = ""
            while True:
                with _health_scan_lock:
                    snapshot = json.dumps(_health_scan_state)
                if snapshot != last_snapshot:
                    yield f"data: {snapshot}\n\n"
                    last_snapshot = snapshot
                with _health_scan_lock:
                    done = _health_scan_state["completed"] and not _health_scan_state["running"]
                if done:
                    yield f"data: {snapshot}\n\n"
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
        for fid in file_ids:
            outcome = _soft_delete_file(fid)
            results.append({
                "file_id": fid,
                "status": outcome["status"],
                "message": outcome["message"],
            })
        return JSONResponse({"status": "ok", "results": results})
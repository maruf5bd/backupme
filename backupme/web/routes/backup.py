"""Backup trigger routes (backlog B05): full backup, per-source backup, and
the drive scan (start + SSE stream + status).

The scan worker writes progress into the shared _scan_state dict from
routes/common.py, which /api/backup/scan/status and the SSE stream read.
"""
import asyncio
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

from fastapi import Body
from fastapi.responses import JSONResponse, StreamingResponse

from backupme.core import database as db
from backupme.core import uploader as uploader_mod
from backupme.log import get_logger

from .common import (_get_backends, _get_backups, _config_as_dict,
                     _scan_state, _scan_lock, _reset_scan, _emit_scan,
                     _safe_stat, _lp)

log = get_logger("web.routes.backup")


def register(app, ctx):

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
        from backupme.core.skip import should_skip, should_skip_dir
        cfg_dict = _config_as_dict(ctx)
        for entry in _get_backups(ctx):
            source = entry.get("source", "")
            if not source or not os.path.exists(source):
                results.append({"source": source, "status": "skipped"})
                continue
            backends = _get_backends(ctx)
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

        from backupme.core.skip import should_skip, should_skip_dir
        cfg_dict = _config_as_dict(ctx)
        backends = _get_backends(ctx)
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

    # ── Scan Drive ───────────────────────────────────────────────

    def _scan_worker(path: str, backend_name: str | None, redundancy: int,
                     excludes: list[str], delete_source: bool):
        from backupme.core.skip import should_skip, should_skip_dir
        import fnmatch
        cfg_dict = _config_as_dict(ctx)

        if backend_name:
            targets = [b for b in _get_backends(ctx) if b.name == backend_name]
        else:
            targets = _get_backends(ctx)

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

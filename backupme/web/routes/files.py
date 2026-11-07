"""File routes (backlog B05): listing, browse, tree, restore-all,
restore-by-ids, download, preview, upload, delete, versions, verify,
replication detail.

The restore workers write their progress into the shared _restore_state
dict from routes/common.py, which /api/restore/status and the SSE stream read.
"""
import asyncio
import os
import time
from pathlib import Path

from fastapi import Body, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask

from backupme.core import database as db
from backupme.core import index as index_mod
from backupme.core import downloader as downloader_mod
from backupme.core import uploader as uploader_mod
from backupme.core import verify
from backupme.log import get_logger

from .common import (_search_files_safe, _soft_delete_file,
                     _get_backends, _config_as_dict,
                     _restore_state, _restore_lock, _emit_restore,
                     op_log_start, op_log_finish,
                     make_sse_stream, start_daemon_thread)

log = get_logger("web.routes.files")


def register(app, ctx):

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

    # ── Restore ──────────────────────────────────────────────

    def _select_restore_rows(source_root: str, path_filter: str):
        """Index rows under a source root (optionally one subfolder), with
        the subfolder prefix stripped so the worker restores relative to it."""
        rows = db.execute(
            "SELECT id, relative_path, sha256, size FROM files WHERE source_root = ? AND deleted_at IS NULL ORDER BY relative_path",
            (source_root,),
        ).fetchall()
        sep = os.sep
        files = []
        for r in rows:
            rel = r["relative_path"]
            if path_filter:
                # Match exact directory: "DCIM" should match "DCIM/photo.jpg" but not "DCIM2/photo.jpg"
                if not (rel == path_filter or rel.startswith(path_filter + sep) or rel.startswith(path_filter + "\\")):
                    continue
                if rel.startswith(path_filter + sep) or rel.startswith(path_filter + "\\"):
                    rel = rel[len(path_filter)+1:].lstrip("\\/")
                elif rel == path_filter:
                    rel = os.path.basename(rel)
            files.append({"id": r["id"], "relative_path": rel})
        return files

    def _start_restore(files: list, dest_path: str, source_label: str):
        with _restore_lock:
            _restore_state.update({
                "running": False, "source_root": source_label, "dest_path": dest_path,
                "total": len(files), "done": 0, "errors": 0, "current": "",
                "error_list": [], "completed": False,
            })
        start_daemon_thread(_restore_worker, files, dest_path)
        return {"status": "started", "source_root": source_label,
                "dest_path": dest_path, "restoring": len(files)}

    def _restore_worker(files: list, dest_path: str):
        with _restore_lock:
            _restore_state["total"] = len(files)
            _restore_state["running"] = True
        _emit_restore()

        dest = Path(dest_path)
        for f in files:
            rel = f["relative_path"]
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

        files = _select_restore_rows(source_root, path_filter)
        return JSONResponse(_start_restore(files, dest_path, source_root))

    @app.post("/api/restore/files")
    async def api_restore_files(data: dict = Body(...)):
        """Restore an explicit set of file ids (e.g. search results) to a
        server-side folder. Duplicate ids collapse; multiple versions of the
        same relative_path restore once with the latest version winning;
        unknown or soft-deleted ids are skipped and reported."""
        dest_path = data.get("dest_path", "").strip()
        raw_ids = data.get("file_ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return JSONResponse(
                {"error": "file_ids (non-empty list) and dest_path required"}, status_code=400)
        try:
            ids = sorted({int(x) for x in raw_ids})
        except (TypeError, ValueError):
            return JSONResponse({"error": "file_ids must be integers"}, status_code=400)
        if not dest_path:
            return JSONResponse({"error": "dest_path required"}, status_code=400)

        with _restore_lock:
            if _restore_state["running"]:
                return JSONResponse({"error": "A restore is already running"}, status_code=409)

        # Chunked IN() keeps the query under SQLite's variable limit for
        # arbitrary user-selected id sets; latest id per path wins.
        best = {}
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start:chunk_start + 500]
            marks = ",".join("?" for _ in chunk)
            rows = db.execute(
                f"SELECT id, relative_path FROM files WHERE id IN ({marks}) AND deleted_at IS NULL",
                chunk,
            ).fetchall()
            for r in rows:
                prev = best.get(r["relative_path"])
                if prev is None or r["id"] > prev["id"]:
                    best[r["relative_path"]] = {"id": r["id"], "relative_path": r["relative_path"]}
        files = sorted(best.values(), key=lambda d: d["relative_path"])
        if not files:
            return JSONResponse(
                {"error": f"none of the {len(ids)} id(s) are live files"}, status_code=400)
        payload = _start_restore(files, dest_path, f"{len(files)} file(s) by id")
        payload["skipped"] = len(ids) - len(files)
        return JSONResponse(payload)

    @app.get("/api/restore/stream")
    async def api_restore_stream():
        return make_sse_stream(_restore_state, _restore_lock)

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
            except Exception as e:
                log.warning("preview binary sniff failed for file id=%s: %s", file_id, e)
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
        op_id = op_log_start("upload", upload_key,
                             {"filename": file.filename, "backend": backend})

        tmp_path = None
        try:
            suffix = Path(file.filename).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                content = await file.read()
                tmp.write(content)
                tmp_path = tmp.name

            backends = _get_backends(ctx)
            if backend:
                backends = [b for b in backends if b.name == backend]

            cfg_dict = _config_as_dict(ctx)
            r = uploader_mod.upload_file(
                source_root="web_upload",
                relative_path=file.filename,
                local_path=tmp_path,
                backends=backends,
                config=cfg_dict,
            )

            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

            op_log_finish(op_id, "completed", target_json={
                "filename": file.filename, "result": "success", "size": len(content)})
            return JSONResponse({"status": "ok", "filename": file.filename, **r})
        except Exception as e:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            op_log_finish(op_id, "failed", last_error=str(e))
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

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

    @app.post("/api/files/{file_id}/verify")
    async def api_file_verify(file_id: int):
        try:
            result = await asyncio.to_thread(verify.verify_file_integrity, file_id)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

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

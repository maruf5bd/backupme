"""Archives dashboard routes (extract-first archives, Phase 2).

Surface over the virtual archive manifests recorded by core/archives.py:

- GET  /archives                    HTML page (archives table + restore form)
- GET  /api/archives                list (?limit&offset) with total
- GET  /api/archives/{archive_id}   one archive + its members (?limit&offset)
- POST /api/archives/restore        confirm-gated restore to a server path

Restore runs the EXISTING sha-verified core restore
(``archives.restore_archive``) via ``asyncio.to_thread`` — no blocking work
inline in async handlers (qa defect D18) — and the executor thread always
drops its SQLite handle (``db.close_connection()``) so no pooled connection
outlives the request.

Route order matters: the specific ``/api/archives/restore`` path is
registered BEFORE ``/api/archives/{archive_id}`` (same rule as
``/api/backends/usage`` before ``/api/backends/{name}``).
"""
import asyncio
from pathlib import Path

from fastapi import Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from backupme.core import archives as archives_mod
from backupme.core import database as db
from backupme.log import get_logger

log = get_logger("web.routes.archives")


def _unsafe_dest(dest: str) -> bool:
    """Server-side destination guard for archive restores.

    Mirrors the member-path guard the file-restore worker applies: traversal
    (``..``), empty paths, drive-relative forms (``C:``) and bare filesystem
    roots are refused. Absolute server paths (e.g. ``D:/restores``) stay
    legal — the dashboard restores onto the server machine by design.
    """
    if not dest or not dest.strip():
        return True
    if "\x00" in dest:
        return True
    p = Path(dest)
    if ".." in p.parts:
        return True
    stripped = dest.replace("\\", "/").rstrip("/")
    if not stripped:
        return True          # "/", "\\", "C:\..." all reduce to a bare root
    if stripped.endswith(":"):
        return True          # "C:" drive-relative / bare drive
    if len(dest) >= 2 and dest[1] == ":" and not p.is_absolute():
        return True          # "C:restores" — drive-relative, resolves oddly
    return False


def register(app, ctx):

    @app.get("/archives", response_class=HTMLResponse)
    async def archives_page(request: Request):
        rows = db.execute(
            "SELECT id, fingerprint, format, original_name, member_count, "
            "total_uncompressed, created_at FROM archives ORDER BY id DESC LIMIT 500"
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM archives").fetchone()[0]
        return ctx.templates.TemplateResponse("archives.html", {
            "request": request,
            "archives": [dict(r) for r in rows],
            "total": total,
        })

    @app.get("/api/archives")
    async def api_archives_list(limit: int = 50, offset: int = 0):
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        total = db.execute("SELECT COUNT(*) FROM archives").fetchone()[0]
        rows = db.execute(
            "SELECT id, fingerprint, format, original_name, member_count, "
            "total_uncompressed, created_at FROM archives "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return JSONResponse({
            "archives": [dict(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    # Specific path first: /api/archives/restore must never be captured by
    # the /{archive_id} route below (route-order rule).
    @app.post("/api/archives/restore")
    async def api_archives_restore(data: dict = Body(default={})):
        # Destructive-write gate, exactly like /api/db/restore: only the
        # literal boolean true passes ("true" the string, 1, "yes" all fail).
        if data.get("confirm") is not True:
            return JSONResponse(
                {"error": "confirm must be exactly true"}, status_code=400)
        fingerprint = str(data.get("fingerprint") or "").strip()
        dest_path = str(data.get("dest_path") or "").strip()
        if not fingerprint or not dest_path:
            return JSONResponse(
                {"error": "fingerprint and dest_path required"}, status_code=400)
        if _unsafe_dest(dest_path):
            return JSONResponse(
                {"error": "unsafe dest_path rejected"}, status_code=400)
        dest = Path(dest_path)
        if dest.exists() and not dest.is_dir():
            return JSONResponse(
                {"error": "dest_path exists and is not a directory"},
                status_code=400)

        def _run_restore():
            # restore_archive is the blocking sha-verified engine (downloads
            # chunks, writes files). It must run off the event loop (D18) and
            # this thread's pooled SQLite handle must not outlive the request.
            try:
                return archives_mod.restore_archive(fingerprint, dest_path)
            finally:
                db.close_connection()

        result = await asyncio.to_thread(_run_restore)
        if result.get("status") == "error":
            message = str(result.get("error", ""))
            if "no archive with fingerprint" in message:
                return JSONResponse(result, status_code=404)
            return JSONResponse(result, status_code=400)
        log.info("archive restore via dashboard: fp=%s dest=%s members=%s errors=%s",
                 result.get("fingerprint", "")[:16], dest_path,
                 result.get("members"), result.get("errors"))
        return JSONResponse(result)

    @app.get("/api/archives/{archive_id}")
    async def api_archives_detail(archive_id: int, limit: int = 100, offset: int = 0):
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        row = db.execute(
            "SELECT id, fingerprint, format, original_name, original_size, "
            "member_count, total_uncompressed, created_at FROM archives "
            "WHERE id = ?",
            (archive_id,),
        ).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        total = db.execute(
            "SELECT COUNT(*) FROM archive_members WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()[0]
        members = db.execute(
            "SELECT id, relative_path, file_id, member_sha256, member_size, "
            "mtime, nested_archive FROM archive_members WHERE archive_id = ? "
            "ORDER BY relative_path LIMIT ? OFFSET ?",
            (archive_id, limit, offset),
        ).fetchall()
        return JSONResponse({
            "archive": dict(row),
            "members": [dict(m) for m in members],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

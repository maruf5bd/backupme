"""Shared plumbing for the backupme.web.routes package (backlog B05/B06).

The old web/routes.py monolith carried every helper as a closure inside
register_routes. After the split (B05) each domain module exposes
register(app, ctx) and the cross-cutting pieces live here exactly once:

- RouteContext: the `ctx` handed to every register() — live Config + templates.
- config helpers: _reload_config/_config_dict/_config_as_dict/_get_cfg_dict/_get_backups
- backend helpers: _get_backends/_get_backend/_probe_space
- search helpers: _fts_phrase/_search_files_like/_search_files_safe
- file mutation: _soft_delete_file (the only soft-delete path)
- fs helpers: _safe_stat/_lp (long-path aware stat), _coerce_int
- background-task state dicts + locks + resets (restore/scan/ingest/rebalance/health scan)
- one task runner (B06): op_log_start/op_log_finish/make_sse_stream/start_daemon_thread

Route registration itself never lives here — see the domain modules.
"""
import asyncio
import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

from fastapi.responses import StreamingResponse

from backupme import config as cfgmod
from backupme.log import get_logger

from backupme.backends import invalidate_cache
from backupme.backends import get_all_backends as _raw_get_all_backends
from backupme.core import database as db
from backupme.core import index as index_mod

log = get_logger("web.routes")


class RouteContext:
    """Everything the domain route modules share: the live Config and the
    Jinja2 templates. Passed as `ctx` to each module's register(app, ctx)."""

    def __init__(self, app, config: cfgmod.Config, templates):
        self.app = app
        self.config = config
        self.templates = templates


def _be_to_dict(be: cfgmod.BackendDef) -> dict:
    return {k: getattr(be, k) for k in ("type", "name", "path", "host", "port", "username",
                                         "password", "key_file", "share", "remote", "bwlimit",
                                         "quota", "priority", "weight", "base_url", "api_key",
                                         "email", "refresh_token", "folder")}


# ── Search helpers ────────────────────────────────────────────

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
    """FTS search with a hard LIKE fallback on sqlite parse errors. The
    quoting + fallback live inside index.search_files since the qa P0 CLI
    fix — the raw user query goes in, never raises."""
    return index_mod.search_files(q, limit=limit)


# ── Config helpers ────────────────────────────────────────────

def _reload_config(ctx: RouteContext):
    config = ctx.config
    yaml_path = Path("config.yaml")
    if not yaml_path.exists():
        return
    import yaml
    raw = yaml.safe_load(yaml_path.read_text()) or {}
    config.backends.clear()
    from ...config import _build_backend, BackupDef
    for btype, blist in raw.get("backends", {}).items():
        for item in blist:
            name = item.get("name", f"{btype}_{len(config.backends.get(btype, []))}")
            config.backends.setdefault(btype, []).append(_build_backend(name, btype, dict(item)))
    config.backups.clear()
    for bdef in raw.get("backups", []):
        config.backups.append(BackupDef(**bdef))


def _config_dict(ctx: RouteContext) -> dict:
    _reload_config(ctx)
    result = {}
    for btype, blist in ctx.config.backends.items():
        result.setdefault(btype, []).extend(_be_to_dict(b) for b in blist)
    return result


def _get_backups(ctx: RouteContext):
    return [{"source": b.source, "redundancy": 1 if not b.mirror else 2}
            for b in getattr(ctx.config, "backups", [])]


def _get_cfg_dict(ctx: RouteContext):
    import dataclasses
    config = ctx.config
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        return dataclasses.asdict(config)
    return vars(config)


def _config_as_dict(ctx: RouteContext):
    """Convert Config dataclass to plain dict for modules that expect dict."""
    import dataclasses
    config = ctx.config
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        return dataclasses.asdict(config)
    return config


# ── Backend helpers ───────────────────────────────────────────

def _get_backends(ctx: RouteContext):
    invalidate_cache()
    cfg = _config_dict(ctx)
    connected = _raw_get_all_backends(cfg)
    connected_names = {b.name for b in connected}
    all_backends = list(connected)
    for btype in ("local", "ftp", "sftp", "smb", "ssh", "rclone", "ninedrive"):
        for entry in cfg.get(btype, []):
            name = entry.get("name", "")
            if name and name not in connected_names:
                from ...backends import BACKEND_MAP
                cls = BACKEND_MAP.get(btype)
                if cls:
                    inst = cls(name, entry)
                    all_backends.append(inst)
    return all_backends


def _get_backend(ctx: RouteContext, name: str):
    for b in _get_backends(ctx):
        if b.name == name:
            return b
    return None


def _probe_space(be) -> tuple:
    """free/total space probe shared by every backend listing (B07).

    Returns (free, total, errors): failures are logged and reported back
    instead of silently degrading to nulls."""
    free = total = None
    errors: list[str] = []
    try:
        free = be.free_space()
        total = be.total_space()
    except Exception as e:
        log.warning("space probe failed for backend %s: %s", be.name, e)
        errors.append(f"space probe failed: {e}")
    return free, total, errors


# ── File mutation ─────────────────────────────────────────────

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


# ── Filesystem helpers ────────────────────────────────────────

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


def _coerce_int(value, fallback):
    """int() coercion for form/json input that keeps the original value when
    it is not an integer (used by the backend add/update routes)."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return fallback


# ── Background-task state dicts (B05: moved here verbatim) ────

# Restore-all operation (files.py)
_restore_state = {
    "running": False, "source_root": "", "dest_path": "",
    "total": 0, "done": 0, "errors": 0, "current": "", "error_list": [], "completed": False,
}
_restore_lock = threading.Lock()
_restore_event = threading.Event()


def _emit_restore():
    _restore_event.set()
    _restore_event.clear()

# Drive scan / backup (backup.py)
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


def _emit_scan():
    _scan_event.set()
    _scan_event.clear()


def _reset_scan():
    with _scan_lock:
        _scan_state.update({
            "running": False, "path": "", "started_at": 0,
            "files_total": 0, "files_done": 0, "files_skipped": 0,
            "files_error": 0, "bytes_total": 0, "bytes_done": 0,
            "current_file": "", "errors": [], "completed": False,
            "results": [],
        })


# cPanel ingest (ingest.py)
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


# Rebalance / drain (backends.py)
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


# Data-health physical scan (health.py)
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


def reset_background_state():
    """Reset every background-task state dict to its idle value.

    register_routes calls this so each fresh app instance starts with clean
    task state: the old monolith scoped these dicts inside register_routes
    (fresh per create_app), and the tests pin that isolation ("no scan ran
    yet in this app instance"). Production runs one app per process, so the
    shared module-level storage is safe once reset at registration."""
    _reset_scan()
    _reset_ingest()
    with _restore_lock:
        _restore_state.update({
            "running": False, "source_root": "", "dest_path": "",
            "total": 0, "done": 0, "errors": 0, "current": "",
            "error_list": [], "completed": False,
        })
    with _rebalance_lock:
        _rebalance_state.update({
            "running": False, "kind": "", "backend": "",
            "done": 0, "total": 0, "current_blob": "",
            "errors": [], "completed": False, "result": None,
        })
    with _health_scan_lock:
        _health_scan_state.update({
            "running": False, "started_at": 0.0, "finished_at": 0.0,
            "completed": False, "results": {}, "remote": [], "errors": [],
        })
    _restore_event.set()
    _restore_event.clear()
    _scan_event.set()
    _scan_event.clear()
    _health_scan_event.set()
    _health_scan_event.clear()


# ── One task runner (B06) ─────────────────────────────────────

def op_log_start(op_type: str, idempotency_key: str, target: dict) -> int | None:
    """Insert a 'running' op_log row and return its id (None on DB failure).

    The op_log row is bookkeeping: losing it must not fail the operation
    itself, but the failure must be visible in the log (B07) — never a
    bare except-pass."""
    try:
        db.execute(
            "INSERT INTO op_log (idempotency_key, op_type, target_json, status, started_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (idempotency_key, op_type, json.dumps(target), "running"),
        )
        db.commit()
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception:
        log.exception("op_log start failed for op_type=%s key=%s", op_type, idempotency_key)
        return None


def op_log_finish(op_id: int | None, status: str, target_json: dict | None = None,
                  last_error: str | None = None) -> None:
    """Move a running op_log row to a terminal status. Never raises; a
    failed bookkeeping update is logged (B07), never silently dropped."""
    if not op_id:
        return
    try:
        if target_json is not None:
            db.execute(
                "UPDATE op_log SET status = ?, completed_at = datetime('now'), target_json = ? WHERE id = ?",
                (status, json.dumps(target_json), op_id),
            )
        else:
            db.execute(
                "UPDATE op_log SET status = ?, completed_at = datetime('now'), last_error = ? WHERE id = ?",
                (status, last_error, op_id),
            )
        db.commit()
    except Exception:
        log.exception("op_log finish failed for op_id=%s status=%s", op_id, status)


def start_daemon_thread(fn, *args) -> threading.Thread:
    """Start a daemon worker thread for a background operation (B06)."""
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


def make_sse_stream(state: dict, lock: threading.Lock,
                    snapshot_fn=None, done_fn=None):
    """Build the standard operation SSE StreamingResponse (B06).

    Every operation stream used to hand-roll the same loop: snapshot the
    state dict under its lock, yield a `data: <json>` frame whenever the
    snapshot changes, and terminate with one final frame once the op is
    completed and not running. `snapshot_fn` lets an op decorate its
    snapshot (ingest injects the live `paused` flag from its Event);
    `done_fn` overrides the completion test.
    """
    if snapshot_fn is None:
        def snapshot_fn():
            with lock:
                return json.dumps(state)
    if done_fn is None:
        def done_fn():
            with lock:
                return state["completed"] and not state["running"]

    async def event_generator():
        last_snapshot = ""
        while True:
            snapshot = snapshot_fn()
            if snapshot != last_snapshot:
                yield f"data: {snapshot}\n\n"
                last_snapshot = snapshot
            if done_fn():
                yield f"data: {snapshot}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

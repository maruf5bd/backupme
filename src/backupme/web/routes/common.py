"""Shared plumbing for the backupme.web.routes package (backlog B05/B06).

The old web/routes.py monolith carried every helper as a closure inside
register_routes. After the split (B05) each domain module exposes
register(app, ctx) and the cross-cutting pieces live here exactly once:

- RouteContext: the `ctx` handed to every register() — live Config + templates.
- config helpers: _reload_config/_config_dict/_config_as_dict/_get_cfg_dict/_get_backups
- backend helpers: _get_backends/_get_backend/_probe_space (thin wrappers over the
  ONE core resolver in backupme.backends — defect D30; the old per-app web pool is
  gone, the web layer shares the CLI's instance cache + cooldown)
- search helpers: _fts_phrase/_search_files_like/_search_files_safe
- file mutation: _soft_delete_file (the only soft-delete path)
- fs helpers: _safe_stat/_lp (long-path aware stat), _coerce_int
- background-task state dicts + locks + resets (restore/scan/ingest/rebalance/health scan/db check)
- one task runner (B06): op_log_start/op_log_finish/make_sse_stream/start_daemon_thread
- TTL caches (Phase A page-perf): per-backend status rows + dashboard dedup savings

Route registration itself never lives here — see the domain modules.
"""
import asyncio
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from fastapi.responses import StreamingResponse

from backupme import config as cfgmod
from backupme.log import get_logger

from backupme.core import database as db
from backupme.core import dedup
from backupme.core import index as index_mod

log = get_logger("web.routes")


class RouteContext:
    """Everything the domain route modules share: the live Config and the
    Jinja2 templates. Passed as `ctx` to each module's register(app, ctx)."""

    def __init__(self, app, config: cfgmod.Config, templates):
        self.app = app
        self.config = config
        self.templates = templates


# ── Secret-key classification (defect D29) ────────────────────

# The value the export writes in place of every credential (import refuses
# to write it back to config.yaml — see config_api.py).
REDACTED_MARKER = "***REDACTED***"

# Word parts that mark a key name as credential-bearing (defect D29). The
# old bare-substring check ("password" in key) missed common shapes:
# credential/passwd/webhook names, camelCase or dash variants, and
# two-part names like access_key. A key is a secret when one of its word
# parts EQUALS one of these (exact part match — so "author" or "tokenizer"
# never match) or when a two-part window forms a known compound. Parts are
# split on _ - . space and camelCase humps, with trailing digits stripped
# ("token2" matches). Covers every secret-shaped field the backend types
# in config.py actually use (password / api_key / refresh_token) and
# web.token.
_SECRET_WORD_PARTS = frozenset({
    "password", "passwd", "pwd", "secret", "secrets", "token", "tokens",
    "apikey", "credential", "credentials", "passphrase", "privatekey",
    "webhook",
})
_SECRET_KEY_PART_PAIRS = frozenset({
    ("api", "key"), ("private", "key"), ("access", "key"),
    ("secret", "key"), ("auth", "key"), ("signing", "key"),
})
_KEY_SEPARATOR_RE = re.compile(r"[_\-. \t]+")
_CAMEL_HUMP_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _key_word_parts(name) -> list[str]:
    """Split a key name into lowercase word parts ("refresh_token",
    "refreshToken" and "refresh-token" all become ["refresh", "token"])."""
    parts: list[str] = []
    for chunk in _KEY_SEPARATOR_RE.split(str(name)):
        if chunk:
            parts.extend(p.lower() for p in _CAMEL_HUMP_RE.split(chunk) if p)
    return parts


def _is_secret_key(name) -> bool:
    """True when a key name is credential-shaped (defect D29). Non-secret
    keys like "key_file" (a path), "api_version" or "author" stay clear."""
    parts = [p.rstrip("0123456789") or p for p in _key_word_parts(name)]
    if not parts:
        return False
    if any(part in _SECRET_WORD_PARTS for part in parts):
        return True
    # A bare "key" entry is credential-shaped; "key_file" is not.
    if len(parts) == 1:
        return parts[0] == "key"
    windows = {tuple(parts[i:i + 2]) for i in range(len(parts) - 1)}
    return bool(windows & _SECRET_KEY_PART_PAIRS)


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
    """Re-read config.yaml through config.load_config — the ONE template
    resolution point (defect D30).

    The old inline re-parse skipped `{{ ENV }}` resolution (the web layer
    authenticated with literal '{{ PASSWORD }}' strings) and crashed with
    TypeError on unknown `backups:` keys the CLI tolerates. load_config
    resolves templates and filters unknown keys for every layer, so the
    web reload now delegates to it and copies the fresh backends/backups
    into ctx.config in place (same object identity — callers hold it)."""
    yaml_path = Path("config.yaml")
    if not yaml_path.exists():
        return
    fresh = cfgmod.load_config(str(yaml_path))
    config = ctx.config
    config.backends.clear()
    config.backends.update(fresh.backends)
    config.backups.clear()
    config.backups.extend(fresh.backups)


def _config_dict(ctx: RouteContext) -> dict:
    """Backends as canonical entry dicts via the ONE serializer
    (backends._config_to_dict, defect D30): the web layer emits the same
    full field set the CLI sees, so rclone keeps config_path /
    extra_args / timeout_s on every web path. Secret masking stays at the
    serving edges (D29)."""
    _reload_config(ctx)
    from ...backends import _config_to_dict
    return _config_to_dict(ctx.config.backends)


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
# The old per-app `_backend_pool` is GONE (defect D30): it was a second
# instance registry next to the core one in backupme.backends — the same
# backend resolved into two objects (double SSH sessions to one box, the
# D10 churn pattern) and the two layers disagreed about which backends
# even existed. Every web route now resolves through the ONE resolver in
# backupme.backends (shared cache keyed by name+type+location, shared
# failed-connect cooldown, honest connected flag).


def _get_backends(ctx: RouteContext):
    """Resolve every configured backend through the ONE core resolver
    (defect D30) — the web layer and the CLI share the same instance cache,
    so the same config resolves to the same instance objects (one SSH
    session per backend, not two). Unreachable backends are listed too,
    with connected=False, so the dashboard shows the gap instead of
    silently dropping them; the resolver's failed-connect cooldown keeps
    an outage from re-dialing on every request."""
    from ...backends import resolve_all_backends
    return resolve_all_backends(_config_dict(ctx))


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


_STATUS_TTL_SECONDS = 30.0


def _list_backend_configs(ctx: RouteContext) -> list[dict]:
    """Config-only backend listing in the same order _get_backends uses —
    never connects, never probes, never touches the network.

    The dashboard page renders its backend cards from this (name + type
    skeleton) and fills the live status asynchronously from
    /api/backends/status, so a dead SFTP backend can no longer hold the
    page render hostage (Phase A page-perf)."""
    cfg_backends = getattr(ctx.config, "backends", {}) or {}
    out = []
    for btype in ("local", "ftp", "sftp", "smb", "ssh", "rclone", "ninedrive"):
        for entry in (cfg_backends.get(btype) or []):
            name = getattr(entry, "name", "") or ""
            if not name:
                continue
            out.append({
                "name": name,
                "type": btype,
                "priority": getattr(entry, "priority", 50),
                "quota": getattr(entry, "quota", None),
                # Dashboard speed-test opt-out (Phase C): `speedtest: false`
                # in the backend's YAML entry. The config-only listing carries
                # it so the backends page can render the button disabled
                # without probing anything.
                "speedtest": bool(getattr(entry, "speedtest", True)),
            })
    return out


def _backend_fingerprint(entry) -> str:
    """Stable cache key for one backend config entry — delegates to the
    resolver's canonical config fingerprint (defect D30: one serializer,
    one key definition). Any config change (password, host, path, ...)
    produces a new fingerprint, so a cached probe result can never leak
    across a reconfigured backend."""
    from ...backends import config_fingerprint
    return config_fingerprint(entry)


# ── TTL caches (Phase A page-perf) ────────────────────────────

class TTLCache:
    """Tiny thread-safe TTL cache on monotonic time.

    `get` returns None on miss/expiry (never raises), `set` overwrites,
    `clear` drops everything — reset_background_state calls clear() on the
    module-level caches at every registration so each fresh app instance
    re-probes from scratch (per-app test isolation, same contract as the
    background-task state dicts)."""

    def __init__(self, ttl_seconds: float = 30.0):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        with self._lock:
            hit = self._store.get(key)
            if hit is None:
                return None
            expires_at, value = hit
            if time.monotonic() >= expires_at:
                return None
            return value

    def set(self, key: str, value) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# Per-backend status rows for GET /api/backends/status (backends.py),
# keyed by backend config fingerprint (~30 s TTL: the second dashboard
# poll inside the window returns the cached probe without any I/O).
_backend_status_cache = TTLCache(_STATUS_TTL_SECONDS)

# dedup.total_dedup_savings() for the dashboard stat cards: a full
# COUNT/SUM aggregate over blob_storage JOIN blobs — 517 ms measured at
# 450k blob_storage rows, seconds-class at the live store's size, so the
# dashboard serves it from this cache and recomputes on a worker thread
# at most once per TTL window.
_dedup_savings_cache = TTLCache(_STATUS_TTL_SECONDS)
_dedup_compute_lock = threading.Lock()


def _compute_dedup_savings_once() -> dict:
    """Single-flight sync compute of the dedup-savings aggregate.

    If another thread is already computing, this waits for it instead of
    duplicating the scan — on the live store the first cold compute is
    minutes-class (cold pages on a multi-GB database), so concurrent first
    hits must share ONE scan. Always ends with the cache populated and the
    computing thread's own SQLite handle dropped."""
    cached = _dedup_savings_cache.get("savings")
    if cached is not None:
        return cached
    with _dedup_compute_lock:
        cached = _dedup_savings_cache.get("savings")
        if cached is not None:
            return cached

        def _compute():
            try:
                return dedup.total_dedup_savings()
            finally:
                db.close_connection()

        result = _compute()
        _dedup_savings_cache.set("savings", result)
        return result


def warm_dedup_savings_cache() -> None:
    """Pre-compute the dedup-savings aggregate off the request path.

    Started as a daemon thread at create_app time: the first live-store
    compute is minutes-class (124 s measured on the 2.6 GB index with cold
    pages), so it must never be paid by the first dashboard visitor. Skipped
    under pytest — fixtures swap dbmod.DB_PATH underneath a running thread,
    which could cache a wrong-DB aggregate."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    threading.Thread(target=_compute_dedup_savings_once, name="dedup-warm",
                     daemon=True).start()


async def _cached_dedup_savings() -> dict:
    """Dashboard/status dedup-savings aggregate served from a ~30 s TTL
    cache.

    dedup.total_dedup_savings() is a full COUNT/SUM over blob_storage JOIN
    blobs: trivial on the test fixture but seconds warm / minutes cold on
    the live store, so the number is computed on a worker thread at most
    once per TTL window, single-flighted across concurrent callers (Phase A
    page-perf; shared by the GET / page handler and GET /api/status since
    Phase C A2; daemon-warmed at create_app so the first visitor never pays
    the cold compute)."""
    cached = _dedup_savings_cache.get("savings")
    if cached is not None:
        return cached
    return await asyncio.to_thread(_compute_dedup_savings_once)


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

# State dicts of ops that actually LAUNCHED at least once in this app
# instance (keyed by dict identity — the op state dicts are module-level
# singletons updated in place, so id() is stable for the process lifetime;
# reset_background_state clears the set at every registration so each fresh
# app starts "never started"). SSE op streams terminate after ONE idle
# snapshot when their op has never been started: until the first op ran,
# `completed` stays False and scripted consumers waiting for EOF hung
# forever (pre-first-op termination defect). Browser EventSource
# auto-reconnects, so the live dashboard is unaffected.
_ops_ever_started: set[int] = set()


def mark_op_started(state: dict) -> None:
    """Record that `state`'s background operation actually launched. Every
    op-launch site calls this right after arming its state dict; SSE streams
    for never-started ops close after a single idle snapshot instead."""
    _ops_ever_started.add(id(state))


def _op_ever_started(state: dict) -> bool:
    return id(state) in _ops_ever_started


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
    # Only the launch sites call this — mark the op as actually started so
    # the rebalance SSE stream polls to completion instead of closing after
    # one idle snapshot.
    mark_op_started(_rebalance_state)


# Serial files-ingest (process-files, backends.py)
_process_state = {
    "running": False,
    "backend": "",
    "prefix": "",
    "target": "",
    "delete": False,
    "done": 0,
    "skipped": 0,
    "errors": 0,
    "total": 0,
    "current": "",
    "errors_list": [],
    "completed": False,
    "result": None,
}
_process_lock = threading.RLock()  # reentrant: launch sites hold it while calling _reset_process


def _reset_process(backend, prefix, target, delete):
    with _process_lock:
        _process_state.update({
            "running": True, "backend": backend, "prefix": prefix,
            "target": target or "", "delete": bool(delete),
            "done": 0, "skipped": 0, "errors": 0, "total": 0,
            "current": "", "errors_list": [], "completed": False,
            "result": None,
        })
    mark_op_started(_process_state)


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


# Database PRAGMA integrity check (dbops.py) — minutes-class on the live
# hot store (29s+ measured, >120s wall on the 2.6 GB DB), so it runs as a
# background op with its own status snapshot + SSE stream and persists the
# result as a 'pragma' row in integrity_checks (Phase A page-perf).
_db_check_state = {
    "running": False,
    "started_at": 0.0,
    "finished_at": 0.0,
    "status": "",      # "" while running; then "ok" | "corrupt" | "error"
    "errors": [],      # PRAGMA integrity_check rows != 'ok' (corrupt branch)
    "error": "",       # worker-level crash text when the op itself failed
    "completed": False,
}
_db_check_lock = threading.Lock()


# Backend speed test (backends.py, Phase C): uploads a small unique test
# object to one backend, downloads it back, hash-verifies, deletes it, and
# measures upload/download ms. Single-flight like the db check (a second
# start is rejected 409) — the measured timings are meaningless when two
# tests compete for the same link.
_speedtest_state = {
    "running": False,
    "backend": "",     # backend currently under test
    "started_at": 0.0,
    "finished_at": 0.0,
    "status": "",      # "" while running; then "ok" | "error"
    "error": "",       # worker-level failure text when status == "error"
    "result": None,    # {backend, object, size, upload_ms, download_ms, verified, deleted, ...}
    "completed": False,
}
_speedtest_lock = threading.Lock()


def reset_background_state():
    """Reset every background-task state dict to its idle value.

    register_routes calls this so each fresh app instance starts with clean
    task state: the old monolith scoped these dicts inside register_routes
    (fresh per create_app), and the tests pin that isolation ("no scan ran
    yet in this app instance"). Production runs one app per process, so the
    shared module-level storage is safe once reset at registration. The
    op-started markers are cleared too — a fresh app's SSE streams close
    after one idle snapshot until an op actually launches here."""
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
    with _db_check_lock:
        _db_check_state.update({
            "running": False, "started_at": 0.0, "finished_at": 0.0,
            "status": "", "errors": [], "error": "", "completed": False,
        })
    with _speedtest_lock:
        _speedtest_state.update({
            "running": False, "backend": "", "started_at": 0.0, "finished_at": 0.0,
            "status": "", "error": "", "result": None, "completed": False,
        })
    with _process_lock:
        _process_state.update({
            "running": False, "backend": "", "prefix": "", "target": "",
            "delete": False, "done": 0, "skipped": 0, "errors": 0,
            "total": 0, "current": "", "errors_list": [],
            "completed": False, "result": None,
        })
    _ops_ever_started.clear()
    _backend_status_cache.clear()
    _dedup_savings_cache.clear()
    _restore_event.set()
    _restore_event.clear()
    _scan_event.set()
    # Drop the shared resolver cache too (defect D30): a fresh app starts
    # with clean instances — same per-app isolation contract the old
    # per-app backend pool gave reset_background_state.
    from ...backends import invalidate_cache
    invalidate_cache()
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

    Termination has two cases:
    - the op completed (`done_fn`) — one final frame, then EOF;
    - the op was NEVER started in this app instance — one idle snapshot,
      then EOF (pre-first-op termination: without it, `completed` stays
      False forever on a fresh app and scripted consumers waiting for EOF
      hung). Browser EventSource auto-reconnects, so the live dashboard
      just re-polls as usual.
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
        emitted_any = False
        while True:
            snapshot = snapshot_fn()
            if snapshot != last_snapshot:
                yield f"data: {snapshot}\n\n"
                last_snapshot = snapshot
                emitted_any = True
            if done_fn():
                yield f"data: {snapshot}\n\n"
                break
            with lock:
                is_running = state.get("running", False)
            if emitted_any and not is_running and not _op_ever_started(state):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

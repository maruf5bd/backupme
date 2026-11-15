"""Phase A page-perf regression suite: the dashboard (/) and database
(/database) pages must render fast WITHOUT running backend connects or the
PRAGMA integrity_check on the request path.

What changed under test here:

- GET / renders from cheap indexed queries only. Backend cards are
  skeletons (data-backend-status markers) filled by JS from the cached
  GET /api/backends/status probe — a dead SFTP backend can no longer hold
  the page render hostage.
- GET /database renders from cheap PRAGMAs plus the newest integrity_checks
  row ("last integrity check"); the full PRAGMA integrity_check moved to a
  background operation (POST /api/db/check/start + /api/db/check/status +
  /api/db/check/stream) that persists a 'pragma' row and an op_log entry.
- GET /api/backends/status returns a documented JSON list
  ({name, type, connected, free_space, total_space, priority, quota, error})
  cached per backend config fingerprint with a ~30 s TTL.

Companion to tests/test_web_p2.py / tests/test_web_d18b.py — same isolated
env/backends fixtures, TestClient(raise_server_exceptions=False) black-box
pattern, gc finalizer for stray portal/executor sqlite handles. Everything
runs against scratch DBs only (never the live database).
"""
import gc
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute

PAGE_PERF_TIME_BUDGET_SECONDS = 5.0  # generous bound: scratch DB, cold caches


@pytest.fixture(autouse=True)
def _release_stray_db_locks():
    """TestClient portal threads / executor threads leave thread-local
    sqlite connections behind; gc them so the next test's fresh-DB init
    never hits a Windows file lock (same finalizer as test_web_p2)."""
    yield
    gc.collect()


@pytest.fixture()
def app(env, backends):
    from backupme.config import load_config
    return create_app(load_config())


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _upload(env, backends, rel: str, data: bytes) -> dict:
    from tests.helpers import backup_bytes
    return backup_bytes(Path(env), rel, data, backends)


def _wait_for(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# 1. Dashboard (/) — backends left the request path
# ---------------------------------------------------------------------------

def test_dashboard_renders_when_backend_resolution_raises(client, env, backends, monkeypatch):
    """The dashboard must not resolve/connect a single backend while
    rendering: with backend resolution rigged to explode, GET / still
    returns 200 and the body carries the skeleton markers the JS fills in.
    Both import surfaces are patched (common's original + pages' re-export)
    so ANY residual call in the handler detonates."""
    def _boom(ctx):
        raise RuntimeError("backend resolution must not run on the request path")
    monkeypatch.setattr("backupme.web.routes.common._get_backends", _boom)
    monkeypatch.setattr("backupme.web.routes.pages._get_backends", _boom)
    r = client.get("/")
    assert r.status_code == 200, r.text[:300]
    assert 'data-backend-status=' in r.text, \
        "dashboard must render data-backend-status skeleton cards"
    assert 'data-backend-badge' in r.text, "skeleton badge container missing"
    assert "checking" in r.text, "skeleton badge must render a checking state"


def test_dashboard_keeps_cheap_stats_and_recent_files(client, env, backends):
    """The perf rewrite must not drop what the page shows: seeded upload
    appears in recent activity, generation renders, dedup stat ids exist."""
    result = _upload(env, backends, "seed/perfdash.txt", b"dashboard perf seed")
    assert "file_id" in result, result
    from backupme.core import database as dbmod
    gen = dbmod.get_generation()
    r = client.get("/")
    assert r.status_code == 200, r.text[:300]
    assert "perfdash.txt" in r.text, "recent files list lost the seeded upload"
    assert f'id="gen-value">{gen}</strong>' in r.text, "generation not rendered"
    for stat_id in ("stat-files", "stat-stored", "stat-blobs", "stat-ratio"):
        assert f'id="{stat_id}"' in r.text, f"dedup stat card element {stat_id} missing"


def test_dashboard_and_database_pages_render_fast(client, env, backends):
    """Wall-clock evidence on the scratch env: both pages answer inside a
    5 s budget (the live problem was ~57 s for / and >120 s for /database).
    Generous bound on purpose — this guards against regressions to
    minutes-class work, not against CI jitter."""
    t0 = time.perf_counter()
    r_dash = client.get("/")
    dash_s = time.perf_counter() - t0
    assert r_dash.status_code == 200, r_dash.text[:300]
    t1 = time.perf_counter()
    r_db = client.get("/database")
    db_s = time.perf_counter() - t1
    assert r_db.status_code == 200, r_db.text[:300]
    assert dash_s < PAGE_PERF_TIME_BUDGET_SECONDS, \
        f"GET / took {dash_s:.2f}s (budget {PAGE_PERF_TIME_BUDGET_SECONDS}s)"
    assert db_s < PAGE_PERF_TIME_BUDGET_SECONDS, \
        f"GET /database took {db_s:.2f}s (budget {PAGE_PERF_TIME_BUDGET_SECONDS}s)"


# ---------------------------------------------------------------------------
# 2. Database page (/database) — the inline PRAGMA integrity_check is gone
# ---------------------------------------------------------------------------

def test_database_page_skips_integrity_check(client, env, backends, monkeypatch):
    """With db.check_integrity rigged to explode, GET /database must still
    return 200 — the full PRAGMA check left the request path — and the
    fresh scratch DB renders the honest 'no check recorded yet' state."""
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise RuntimeError("PRAGMA integrity_check must not run on the request path")

    from backupme.core import database as dbmod
    monkeypatch.setattr(dbmod, "check_integrity", _boom)
    r = client.get("/database")
    assert r.status_code == 200, r.text[:300]
    assert calls["n"] == 0, \
        f"/database ran check_integrity {calls['n']}x on the request path"
    assert "No integrity check recorded yet" in r.text, \
        "fresh scratch DB must render the 'no check recorded yet' last-check state"
    assert 'id="last-check-sub"' in r.text, "last-check display element missing"
    assert "last check: never" in r.text, "last-check fallback text missing"


def test_database_page_shows_last_recorded_check(client, env, backends):
    """The page's 'last integrity check' comes from the integrity_checks
    table: seed a failed pragma row and the page must show corrupt."""
    started = "2020-03-01 10:00:00"
    execute(
        "INSERT INTO integrity_checks (check_type, status, total_files, "
        "checked_files, failed_files, started_at, completed_at, details) "
        "VALUES ('pragma','failed',0,0,2,?,?,'[\"err a\",\"err b\"]')",
        (started, started),
    )
    execute("commit")
    r = client.get("/database")
    assert r.status_code == 200, r.text[:300]
    assert "Corrupt" in r.text, "failed last check must render the corrupt badge"
    assert "pragma" in r.text, "last-check text must name the check_type"


# ---------------------------------------------------------------------------
# 3. GET /api/backends/status — shape + ~30 s TTL cache
# ---------------------------------------------------------------------------

class _FakeBackend:
    """Minimal backend stand-in: the exact surface the status probe uses."""

    def __init__(self, name: str):
        self.name = name
        self.type = "local"
        self.connected = True
        self.config = {"priority": 42, "quota": None}

    def free_space(self):
        return 1234

    def total_space(self):
        return 4321


@pytest.fixture()
def probe_counter(monkeypatch):
    """Replace backend resolution in the status route with a counting fake
    (named after the env config's test_local so the probe resolves it).
    The count doubles as the probe counter: a call = a probe pass."""
    calls = {"n": 0}

    def fake_get_backends(ctx):
        calls["n"] += 1
        return [_FakeBackend("test_local")]

    monkeypatch.setattr("backupme.web.routes.backends._get_backends", fake_get_backends)
    return calls


def test_backends_status_documented_shape(client, probe_counter):
    r = client.get("/api/backends/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list), body
    assert len(body) == 1, body
    row = body[0]
    assert set(row.keys()) == {"name", "type", "connected", "free_space",
                               "total_space", "priority", "quota", "error"}, row
    assert row["name"] == "test_local", row
    assert row["connected"] is True, row
    assert row["free_space"] == 1234 and row["total_space"] == 4321, row
    assert row["priority"] == 42 and row["quota"] is None, row
    assert row["error"] is None, row
    assert probe_counter["n"] == 1, "first status call must probe exactly once"


def test_backends_status_second_call_within_ttl_does_not_reprobe(client, probe_counter):
    """The ~30 s TTL: the second call inside the window returns the cached
    rows without resolving or probing any backend."""
    first = client.get("/api/backends/status")
    assert first.status_code == 200, first.text
    second = client.get("/api/backends/status")
    assert second.status_code == 200, second.text
    assert second.json() == first.json(), "cached rows must be identical"
    assert probe_counter["n"] == 1, (
        f"second call within the TTL re-probed {probe_counter['n'] - 1}x — "
        "the status cache is not being consulted")


def test_backends_status_never_raises_to_client(client, monkeypatch):
    """A failing probe path degrades to honest offline rows (connected:false
    plus an error string) with HTTP 200 — never a 500."""
    def _boom(ctx):
        raise RuntimeError("storage box unreachable")

    monkeypatch.setattr("backupme.web.routes.backends._get_backends", _boom)
    r = client.get("/api/backends/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list), body
    assert body, "configured backends must still be listed"
    row = body[0]
    assert row["connected"] is False, row
    assert row["error"], "a failing backend must carry an error string"
    assert "storage box unreachable" in row["error"], row


# ---------------------------------------------------------------------------
# 4. Background integrity check — completes, persists, audits
# ---------------------------------------------------------------------------

def test_background_integrity_check_completes_with_op_log(client, env, backends):
    _upload(env, backends, "seed/checkseed.txt", b"integrity op seed")
    r = client.post("/api/db/check/start")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "started"}, r.json()

    def _finished():
        snap = client.get("/api/db/check/status").json()
        return snap.get("completed") and not snap.get("running")

    assert _wait_for(_finished, timeout=15.0), "background check never completed"
    snap = client.get("/api/db/check/status").json()
    assert snap["status"] == "ok", snap
    assert snap["errors"] == [], snap
    assert snap["error"] == "", snap

    # Result persisted as a 'pragma' row in integrity_checks.
    row = execute(
        "SELECT check_type, status, failed_files FROM integrity_checks "
        "ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "no integrity_checks row written"
    assert row["check_type"] == "pragma", dict(row)
    assert row["status"] == "completed", dict(row)
    assert row["failed_files"] == 0, dict(row)

    # op_log audit entry completed like every other background op.
    logs = execute(
        "SELECT op_type, status FROM op_log WHERE op_type = 'db_check' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert logs is not None, "no op_log entry for the db_check op"
    assert logs["status"] == "completed", dict(logs)


def test_background_integrity_check_reports_corrupt(client, monkeypatch):
    """The corrupt branch flows through the same background machinery and
    stays visible: status 'corrupt', errors recorded, op_log failed->? and
    the pragma row 'failed'."""
    from backupme.core import database as dbmod
    monkeypatch.setattr(dbmod, "check_integrity",
                        lambda: ["*** in database main *** frame 7 damage"])
    r = client.post("/api/db/check/start")
    assert r.status_code == 200, r.text

    def _finished():
        snap = client.get("/api/db/check/status").json()
        return snap.get("completed") and not snap.get("running")

    assert _wait_for(_finished, timeout=15.0), "background check never completed"
    snap = client.get("/api/db/check/status").json()
    assert snap["status"] == "corrupt", snap
    assert snap["errors"] == ["*** in database main *** frame 7 damage"], snap
    row = execute(
        "SELECT check_type, status, failed_files FROM integrity_checks "
        "ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and row["check_type"] == "pragma", dict(row)
    assert row["status"] == "failed" and row["failed_files"] == 1, dict(row)


def test_background_integrity_check_second_start_is_409_while_running(client, monkeypatch):
    """Single-flight: while a check runs, a second start is rejected 409
    instead of stacking a duplicate PRAGMA run."""
    gate = {"release": False}
    from backupme.core import database as dbmod

    def _slow_check():
        while not gate["release"]:
            time.sleep(0.02)
        return []

    monkeypatch.setattr(dbmod, "check_integrity", _slow_check)
    r1 = client.post("/api/db/check/start")
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/db/check/start")
    assert r2.status_code == 409, r2.text
    assert r2.json()["status"] == "busy", r2.json()
    gate["release"] = True
    assert _wait_for(
        lambda: client.get("/api/db/check/status").json().get("completed"),
        timeout=15.0), "gated check never completed"


# ---------------------------------------------------------------------------
# 5. SSE stream contract for the background check
# ---------------------------------------------------------------------------

def _drain_sse(client: TestClient, path: str) -> list[str]:
    with client.stream("GET", path) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream"), \
            f"{path} content-type: {resp.headers['content-type']}"
        raw = "".join(resp.iter_text())
    return [e for e in raw.split("\n\n") if e.strip()]


def test_db_check_stream_emits_and_terminates(client, env, backends):
    _upload(env, backends, "seed/streamseed.txt", b"stream seed")
    start = client.post("/api/db/check/start")
    assert start.status_code == 200, start.text
    events = _drain_sse(client, "/api/db/check/stream")
    assert events, "stream closed without emitting a single SSE event"
    for event in events:
        assert event.startswith("data: "), f"malformed SSE frame: {event[:80]!r}"
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is True, f"stream did not end completed: {last}"
    assert last["running"] is False, f"stream did not end idle: {last}"
    assert last["status"] == "ok", last


def test_db_check_stream_terminates_when_never_started(client):
    """Pre-first-op termination contract: on a fresh app the idle stream
    emits one snapshot and closes (same behavior as the other op streams)."""
    events = _drain_sse(client, "/api/db/check/stream")
    assert events, "idle stream closed without a snapshot"
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is False and last["running"] is False, last


# ---------------------------------------------------------------------------
# 6. /api/db/info — last recorded check instead of an inline PRAGMA run
# ---------------------------------------------------------------------------

def test_db_info_reports_last_recorded_check_without_running_it(client, env, backends,
                                                                monkeypatch):
    from backupme.core import database as dbmod
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise RuntimeError("must not run the PRAGMA check inline")

    monkeypatch.setattr(dbmod, "check_integrity", _boom)
    r = client.get("/api/db/info")
    assert r.status_code == 200, r.text
    body = r.json()
    assert calls["n"] == 0, "/api/db/info ran check_integrity inline"
    assert body["integrity_status"] == "unknown", body
    assert body["integrity_errors"] == [], body
    assert body["last_check_at"] is None, body

    started = "2020-04-01 09:00:00"
    execute(
        "INSERT INTO integrity_checks (check_type, status, total_files, "
        "checked_files, failed_files, started_at, completed_at, details) "
        "VALUES ('pragma','completed',0,0,0,?,?,'[]')",
        (started, started),
    )
    execute("commit")
    r2 = client.get("/api/db/info")
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["integrity_status"] == "ok", body2
    assert body2["last_check_at"] == started, body2

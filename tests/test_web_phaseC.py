"""Phase C feature suite: backend speed test, restore-queue panel, live
recent-operations feed on the dashboard, plus the two page-perf follow-ups
(A1: /files + /backends pages render from the config-only backend listing;
A2: /api/status rides the ~30 s TTL status cache instead of probing every
backend on the dashboard's 10 s poll).

Companion to tests/test_web_perf_pages.py (Phase A) — same isolated
env/backends fixtures, TestClient(raise_server_exceptions=False) black-box
pattern, gc finalizer for stray portal/executor sqlite handles. Everything
runs against scratch DBs only (never the live database).

Contracts under test here:

- POST /api/backends/{name}/speedtest (registered BEFORE
  /api/backends/{name}): uploads a small unique test object to the backend,
  downloads it back, hash-verifies, deletes it (delete runs even when a
  step fails), measures upload/download ms; single-flight 409 like the db
  check; audited in op_log as op_type='speedtest'; `speedtest: false` in a
  backend's YAML entry opts it out (BackendDef.speedtest).
- GET /api/backends/speedtest/status: live single-flight state plus the
  latest persisted outcome per backend (from op_log) for the per-card
  result line.
- GET /api/restore/queue: the restore-queue panel feed — live single-flight
  restore state + recent op_log rows for op_type='restore', with honest
  `stale` marks for op rows stuck at 'running' while nothing runs.
- Dashboard (GET /): renders the Recent Operations card wired to
  /api/activity?limit=8; the /api/activity contract itself is unchanged.
- GET /files and GET /backends: backend resolution rigged to raise proves
  the pages no longer touch backends on the request path (config-only
  listing; the backends page fills via /api/backends/status from JS).
- GET /api/status: response shape unchanged (pinned by
  tests/test_test_qa1_web_sweep.py) but per-backend rows come from the shared TTL
  cache — within the TTL it does not re-probe.
"""
import gc
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute

_SPEEDTEST_OBJECT_SIZE = 8 * 1024  # _SPEEDTEST_SIZE_BYTES in routes/backends.py


@pytest.fixture(autouse=True)
def _release_stray_db_locks():
    """TestClient portal threads / executor threads leave thread-local
    sqlite connections behind; gc them so the next test's fresh-DB init
    never hits a Windows file lock (same finalizer as test_web_perf_pages)."""
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


def _storage_root(env) -> Path:
    """The fixture local backend's storage root (mirrors tests/conftest.py)."""
    return Path(env).parent / "storage"


def _speedtest_status(client) -> dict:
    return client.get("/api/backends/speedtest/status").json()


def _speedtest_finished(client) -> bool:
    snap = _speedtest_status(client)
    return snap.get("completed") and not snap.get("running")


# ---------------------------------------------------------------------------
# 1. Backend speed test — success round trip
# ---------------------------------------------------------------------------

def test_speedtest_success_round_trip(client, env, backends):
    r = client.post("/api/backends/test_local/speedtest")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "started", "backend": "test_local"}, r.json()

    assert _wait_for(lambda: _speedtest_finished(client), timeout=15.0), \
        "speed test never completed"
    snap = _speedtest_status(client)
    assert snap["status"] == "ok", snap
    assert snap["error"] == "", snap
    result = snap["result"]
    assert result["backend"] == "test_local", result
    assert result["size"] == _SPEEDTEST_OBJECT_SIZE, result
    assert result["verified"] is True, result
    assert result["deleted"] is True, result
    assert isinstance(result["upload_ms"], (int, float)), result
    assert isinstance(result["download_ms"], (int, float)), result
    assert result["object"].startswith("speedtest/speedtest-"), result

    # The test object was uploaded to (and deleted from) the fixture local
    # backend: nothing lingers under its speedtest/ namespace.
    leftovers = list((_storage_root(env) / "speedtest").glob("*")) \
        if (_storage_root(env) / "speedtest").exists() else []
    assert leftovers == [], f"speedtest object left on the backend: {leftovers}"

    # Audited in op_log like every other background op.
    row = execute(
        "SELECT op_type, status, target_json FROM op_log "
        "WHERE op_type = 'speedtest' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "no op_log entry for the speedtest op"
    assert row["status"] == "completed", dict(row)
    payload = json.loads(row["target_json"])
    assert payload["verified"] is True, payload
    assert payload["size"] == _SPEEDTEST_OBJECT_SIZE, payload

    # The result is persisted for display: the status feed carries it in
    # `recent` (latest outcome per backend).
    recent = [r for r in snap["recent"] if r["backend"] == "test_local"]
    assert recent, "speedtest status feed lost the persisted result"
    assert recent[0]["status"] == "completed", recent
    assert recent[0]["upload_ms"] is not None, recent


def test_speedtest_status_endpoint_shape_on_fresh_app(client):
    """Fresh app: idle snapshot plus an empty recent list, 200 JSON."""
    snap = _speedtest_status(client)
    assert snap["running"] is False, snap
    assert snap["completed"] is False, snap
    assert snap["status"] == "", snap
    assert snap["backend"] == "", snap
    assert snap["recent"] == [], snap


# ---------------------------------------------------------------------------
# 2. Speed test — download failure still deletes the test object
# ---------------------------------------------------------------------------

class _RiggedBackend:
    """Wraps a real LocalBackend; records every call and rigs `download`
    to raise after the object was uploaded (the delete in the worker's
    finally clause must still clean the object up)."""

    def __init__(self, real):
        self._real = real
        self.calls = []
        self.name = real.name
        self.type = real.type
        self.connected = real.connected
        self.config = real.config

    def connect(self):
        self.calls.append("connect")
        return self._real.connect()

    def upload(self, local_path, remote_rel_path):
        self.calls.append(("upload", remote_rel_path))
        return self._real.upload(local_path, remote_rel_path)

    def download(self, remote_rel_path, local_path):
        self.calls.append(("download", remote_rel_path))
        raise RuntimeError("rigged download failure")

    def delete(self, remote_rel_path):
        self.calls.append(("delete", remote_rel_path))
        return self._real.delete(remote_rel_path)


def test_speedtest_download_failure_still_deletes(client, env, backends, monkeypatch):
    from backupme.backends.local import LocalBackend
    real = LocalBackend("test_local", {"path": str(_storage_root(env))})
    fake = _RiggedBackend(real)
    monkeypatch.setattr("backupme.web.routes.backends._get_backend",
                        lambda ctx, name: fake)

    r = client.post("/api/backends/test_local/speedtest")
    assert r.status_code == 200, r.text
    assert _wait_for(lambda: _speedtest_finished(client), timeout=15.0), \
        "speed test never completed"

    snap = _speedtest_status(client)
    assert snap["status"] == "error", snap
    assert "rigged download failure" in snap["error"], snap
    assert snap["result"]["verified"] is False, snap["result"]

    # The worker reached upload AND download, and the finally-clause delete
    # ran even though the download raised.
    ops = [c[0] if isinstance(c, tuple) else c for c in fake.calls]
    assert "upload" in ops and "download" in ops, fake.calls
    assert "delete" in ops, f"test object not deleted after failure: {fake.calls}"
    assert snap["result"]["deleted"] is True, snap["result"]
    assert "delete" in ops[ops.index("download"):], "delete must run after download"

    # Nothing lingers on the backend.
    leftovers = list((_storage_root(env) / "speedtest").glob("*")) \
        if (_storage_root(env) / "speedtest").exists() else []
    assert leftovers == [], f"speedtest object left on the backend: {leftovers}"

    # The failure is honestly audited (op_log failed, error text kept in
    # the payload — op_log_finish writes target_json OR last_error).
    row = execute(
        "SELECT status, target_json FROM op_log WHERE op_type = 'speedtest' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "no op_log entry for the failed speedtest op"
    assert row["status"] == "failed", dict(row)
    assert "rigged download failure" in json.loads(row["target_json"]).get("error", ""), \
        dict(row)


# ---------------------------------------------------------------------------
# 3. Speed test — single-flight 409 while running
# ---------------------------------------------------------------------------

def test_speedtest_second_start_is_409_while_running(client, env, backends,
                                                     monkeypatch):
    from backupme.backends.local import LocalBackend
    gate = {"release": False}
    real = LocalBackend("test_local", {"path": str(_storage_root(env))})

    class _SlowUploadBackend(_RiggedBackend):
        def upload(self, local_path, remote_rel_path):
            # Block inside the worker so the op stays running while the
            # second start is rejected.
            while not gate["release"]:
                time.sleep(0.02)
            return self._real.upload(local_path, remote_rel_path)

        def download(self, remote_rel_path, local_path):
            return self._real.download(remote_rel_path, local_path)

    fake = _SlowUploadBackend(real)
    monkeypatch.setattr("backupme.web.routes.backends._get_backend",
                        lambda ctx, name: fake)

    first = client.post("/api/backends/test_local/speedtest")
    assert first.status_code == 200, first.text

    second = client.post("/api/backends/test_local/speedtest")
    assert second.status_code == 409, second.text
    assert second.json()["status"] == "busy", second.json()

    gate["release"] = True
    assert _wait_for(lambda: _speedtest_finished(client), timeout=15.0), \
        "gated speed test never completed"
    assert _speedtest_status(client)["status"] == "ok"


# ---------------------------------------------------------------------------
# 4. Speed test — `speedtest: false` opt-out
# ---------------------------------------------------------------------------

def test_speedtest_opt_out_config_respected(env, backends):
    cfg_path = Path(env) / "config.yaml"
    cfg_path.write_text(cfg_path.read_text().replace(
        "    priority: 100\n",
        "    priority: 100\n    speedtest: false\n"))
    from backupme.config import load_config
    armed = TestClient(create_app(load_config()), raise_server_exceptions=False)

    r = armed.post("/api/backends/test_local/speedtest")
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["status"] == "error", body
    assert "speed test is disabled" in body["message"], body

    # Refused before any op_log row is written (no phantom audit entries).
    row = execute(
        "SELECT COUNT(*) FROM op_log WHERE op_type = 'speedtest'"
    ).fetchone()
    assert row[0] == 0, "opted-out speedtest must not write an op_log row"

    # The config-only listing carries the flag so the page can render the
    # button disabled without probing anything.
    r2 = armed.get("/backends")
    assert r2.status_code == 200, r2.text[:300]
    assert "speedtest: false" in r2.text, "disabled speed-test hint missing"
    assert "disabled" in r2.text, "the speed test button must render disabled"


# ---------------------------------------------------------------------------
# 5. Restore-queue panel — seeded op states + honest stale marking
# ---------------------------------------------------------------------------

def test_restore_queue_shows_seeded_states_with_stale_mark(client):
    rows = [
        ("cq-restore-1", "restore", json.dumps({
            "source": "S1", "dest": "restore_out/a", "restored": 5, "errors": 0}),
         "completed", "2020-05-01 10:00:00", "2020-05-01 10:00:03", None),
        ("cq-restore-2", "restore", json.dumps({
            "source": "S2", "dest": "restore_out/b", "restored": 3, "errors": 2}),
         "failed", "2020-05-02 10:00:00", "2020-05-02 10:00:04", "intentional"),
        # A 'running' row with nothing running in this app instance:
        # interrupted by a restart -> the feed must mark it stale, honestly.
        ("cq-restore-3", "restore", json.dumps({
            "source": "S3", "dest": "restore_out/c", "files": 9}),
         "running", "2020-05-03 10:00:00", None, None),
        # A different op type must NOT appear in the restore queue.
        ("cq-upload-1", "upload", json.dumps({"filename": "x.txt"}),
         "completed", "2020-05-04 10:00:00", "2020-05-04 10:00:01", None),
    ]
    for key, op_type, target, status, started, completed, last_error in rows:
        execute(
            "INSERT INTO op_log (idempotency_key, op_type, target_json, status, "
            "started_at, completed_at, last_error) VALUES (?,?,?,?,?,?,?)",
            (key, op_type, target, status, started, completed, last_error),
        )
    execute("commit")

    r = client.get("/api/restore/queue")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"live", "recent"}, body
    assert body["live"]["running"] is False, body["live"]

    recent = body["recent"]
    assert len(recent) == 3, f"non-restore op leaked into the queue: {recent}"
    by_status = {row["status"]: row for row in recent}
    assert set(by_status) == {"completed", "failed", "running"}, recent

    completed_row = by_status["completed"]
    assert completed_row["restored"] == 5 and completed_row["errors"] == 0, completed_row
    assert completed_row["stale"] is False, completed_row
    assert completed_row["dest"] == "restore_out/a", completed_row

    failed_row = by_status["failed"]
    assert failed_row["restored"] == 3 and failed_row["errors"] == 2, failed_row
    assert failed_row["stale"] is False, failed_row

    running_row = by_status["running"]
    assert running_row["stale"] is True, (
        "an op_log row stuck at 'running' while nothing runs must be marked stale")
    assert running_row["dest"] == "restore_out/c", running_row


def test_restore_panel_wired_on_files_page(client, env, backends):
    """The Files page ships the queue panel, the SSE-backed live progress
    and the Activity link filtered to restore ops."""
    r = client.get("/files")
    assert r.status_code == 200, r.text[:300]
    assert 'id="restore-queue-card"' in r.text, "restore queue panel missing"
    assert "/api/restore/queue" in r.text, "queue poll wiring missing"
    assert "/api/restore/stream" in r.text, "live progress must ride the existing SSE stream"
    assert 'href="/logs?op=restore"' in r.text, "Activity link (restore filter) missing"
    assert "stale" in r.text, "the honest stale state must be rendered by the panel"


def test_real_restore_writes_op_log_and_appears_in_queue(client, env, backends):
    _upload(env, backends, "seed/queue_seed.txt", b"restore queue seed")
    dest = Path(env) / "queue_restore_out"
    start = client.post("/api/restore/all", json={
        "source_root": str(env), "dest_path": str(dest)})
    assert start.status_code == 200, start.text
    assert start.json()["status"] == "started"

    def _row_done():
        rows = execute(
            "SELECT status, target_json FROM op_log WHERE op_type = 'restore' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchall()
        return bool(rows) and rows[0]["status"] in ("completed", "failed")

    assert _wait_for(_row_done, timeout=15.0), "restore op never recorded in op_log"
    row = execute(
        "SELECT status, target_json FROM op_log WHERE op_type = 'restore' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "completed", dict(row)
    payload = json.loads(row["target_json"])
    assert payload["restored"] == 1 and payload["errors"] == 0, payload

    body = client.get("/api/restore/queue").json()
    assert body["recent"], "completed restore missing from the queue feed"
    newest = body["recent"][0]
    assert newest["status"] == "completed", newest
    assert newest["stale"] is False, newest
    assert body["live"]["completed"] is True and body["live"]["running"] is False, body["live"]


# ---------------------------------------------------------------------------
# 6. Dashboard — Recent Operations card + /api/activity contract
# ---------------------------------------------------------------------------

def test_dashboard_renders_recent_ops_card(client):
    r = client.get("/")
    assert r.status_code == 200, r.text[:300]
    assert 'id="recent-ops-card"' in r.text, "Recent Operations card missing"
    assert "Recent Operations" in r.text, "card title missing"
    assert "/api/activity?limit=8" in r.text, "op_log poll wiring missing"
    # Honest failure state ships with the page (a poll error must never
    # leave the card stuck on 'loading').
    assert "Activity feed unavailable" in r.text, "feed-unavailable state missing"


def test_api_activity_contract_unchanged(client):
    """The dashboard poll target is the existing op_log JSON endpoint: LIMIT
    is honored, rows are dicts with op_type/status, newest first."""
    stamp = "2020-06-0{} 08:00:00"
    for i in range(1, 5):
        execute(
            "INSERT INTO op_log (idempotency_key, op_type, target_json, status, "
            "started_at, completed_at) VALUES (?,?,?,?,?,?)",
            (f"cq-act-{i}", "qa_probe", json.dumps({"probe": i}),
             "completed", stamp.format(i), stamp.format(i)),
        )
    execute("commit")
    r = client.get("/api/activity", params={"limit": 8})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list) and len(body) == 4, body
    ids = [row["id"] for row in body]
    assert ids == sorted(ids, reverse=True), "activity must be newest first"
    assert all("op_type" in row and "status" in row for row in body), body


# ---------------------------------------------------------------------------
# 7. Perf A1 — /files and /backends render without backend resolution
# ---------------------------------------------------------------------------

def _rig_resolution_boom(monkeypatch):
    """Rig BOTH import surfaces so any residual backend resolution on a
    page-render path detonates (same pattern as test_web_perf_pages)."""
    def _boom(ctx):
        raise RuntimeError("backend resolution must not run on the request path")
    monkeypatch.setattr("backupme.web.routes.common._get_backends", _boom)
    monkeypatch.setattr("backupme.web.routes.pages._get_backends", _boom)


def test_files_page_renders_without_backend_resolution(client, env, backends,
                                                       monkeypatch):
    _rig_resolution_boom(monkeypatch)
    r = client.get("/files")
    assert r.status_code == 200, r.text[:300]
    # The filter dropdown still lists backend NAMES (config-only listing is
    # enough for the files page — no probe needed there).
    assert '<option value="test_local">' in r.text, \
        "files page lost the backend filter options"


def test_backends_page_renders_without_backend_resolution(client, env, backends,
                                                          monkeypatch):
    _rig_resolution_boom(monkeypatch)
    r = client.get("/backends")
    assert r.status_code == 200, r.text[:300]
    # Skeleton cards (config-only) + the JS fill wiring from
    # /api/backends/status, same pattern as the Phase A dashboard cards.
    assert "data-be-conn=" in r.text, "backend skeleton badge marker missing"
    assert "checking" in r.text, "skeleton badge must render a checking state"
    assert "/api/backends/status" in r.text, "status fill wiring missing"
    assert "runSpeedTest" in r.text, "speed test button wiring missing"


def test_backends_page_still_renders_usage_bar_markup(client, env, backends):
    """tests/test_web_polish.py pins `data-usage-bar` on the capacity bar
    and the Connected/Not connected badge wording — the JS-filled skeleton
    must keep shipping both."""
    r = client.get("/backends")
    assert r.status_code == 200, r.text[:300]
    assert "data-usage-bar" in r.text, "capacity usage bar markup lost"
    assert "Connected" in r.text and "Not connected" in r.text, \
        "connection badge wording lost"
    assert ">Online<" not in r.text and ">Offline<" not in r.text


# ---------------------------------------------------------------------------
# 8. Perf A2 — /api/status rides the TTL status cache (shape unchanged)
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
    """Counting fake for backend resolution in the status probe path (the
    count doubles as the probe counter: a call = a probe pass)."""
    calls = {"n": 0}

    def fake_get_backends(ctx):
        calls["n"] += 1
        return [_FakeBackend("test_local")]

    monkeypatch.setattr("backupme.web.routes.backends._get_backends", fake_get_backends)
    return calls


def test_api_status_shape_unchanged(client, env, backends):
    """The pinned /api/status contract (tests/test_test_qa1_web_sweep.py
    test_api_status_shape): same keys, per-backend rows still carry
    name/type/connected/stored_bytes."""
    result = _upload(env, backends, "seed/status_seed.txt", b"status shape seed")
    assert "file_id" in result, result
    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("total_files", "blob_count", "stored_bytes", "dedup_ratio",
                "generation", "backends"):
        assert key in body, f"status missing {key}: {body.keys()}"
    row = next(b for b in body["backends"] if b["name"] == "test_local")
    assert row["type"] == "local", row
    assert row["connected"] is True, row
    assert row["stored_bytes"] >= 15, row


def test_api_status_within_ttl_does_not_reprobe(client, env, backends,
                                                probe_counter, monkeypatch):
    """The dashboard polls /api/status every 10 s; the per-backend rows
    must come from the ~30 s TTL cache shared with /api/backends/status —
    and misc.py must not resolve backends inline anymore."""
    def _misc_boom(ctx):
        raise RuntimeError("misc.py must not resolve backends inline (A2)")

    monkeypatch.setattr("backupme.web.routes.misc._get_backends", _misc_boom)

    # Prime the cache through the status endpoint the cards use.
    first = client.get("/api/backends/status")
    assert first.status_code == 200, first.text
    assert probe_counter["n"] == 1, "priming call must probe exactly once"

    # The 10 s dashboard poll rides the cache: no re-probe, honest rows.
    s1 = client.get("/api/status")
    s2 = client.get("/api/status")
    assert s1.status_code == 200 and s2.status_code == 200, (s1.text, s2.text)
    assert probe_counter["n"] == 1, (
        f"/api/status re-probed {probe_counter['n'] - 1}x inside the TTL — "
        "the shared status cache is not being consulted")
    row = next(b for b in s1.json()["backends"] if b["name"] == "test_local")
    assert row["connected"] is True, row


def test_api_status_degrades_when_probe_fails(client, env, backends, monkeypatch):
    """A failing probe path still serves /api/status 200 with honest
    offline rows (never a 500) — same degradation as /api/backends/status."""
    def _boom(ctx):
        raise RuntimeError("storage box unreachable")

    monkeypatch.setattr("backupme.web.routes.backends._get_backends", _boom)
    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backends"], "configured backends must still be listed"
    row = body["backends"][0]
    # Honest offline row (the pinned /api/status row shape carries only
    # name/type/connected/stored_bytes — the error string lives on
    # /api/backends/status, covered by tests/test_web_perf_pages.py).
    assert row["connected"] is False, row
    assert row["stored_bytes"] == 0, row


# ---------------------------------------------------------------------------
# 9. Route order — the speedtest POST cannot be captured by detail routes
# ---------------------------------------------------------------------------

def test_speedtest_route_order(client, app):
    """POST /api/backends/{name}/speedtest is registered before
    GET /api/backends/{name} and /api/backends/speedtest/status resolves as
    its own route, not the {name} detail path (backends.py ORDER CRITICAL)."""
    paths = [r.path for r in app.routes]
    assert "/api/backends/{name}/speedtest" in paths
    assert "/api/backends/speedtest/status" in paths
    idx_speedtest = paths.index("/api/backends/{name}/speedtest")
    idx_detail = paths.index("/api/backends/{name}")
    assert idx_speedtest < idx_detail, (
        "speedtest POST must be registered before the {name} detail route")

    r = client.get("/api/backends/speedtest/status")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), dict), "speedtest/status must not be captured by {name}"


def test_speedtest_unknown_backend_is_404(client):
    r = client.post("/api/backends/ghost/speedtest")
    assert r.status_code == 404, r.text
    assert r.json()["status"] == "error", r.json()

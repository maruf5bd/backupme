"""Tests for Plan 04 web GUI: /ingest page and ingest APIs."""
import io
import json
import tarfile
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute
from backupme.core import cpanel_ingest as ci


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(env / "drop")
    cfg.cpanel_ingest.staging_dir = str(env / "staging")
    app = create_app(cfg)
    return TestClient(app)


def _make_cpmove(env, name: str = "cpmove-acct.tar.gz",
                 marker: bytes = b"<?php echo 1;"):
    drop = env / "drop" / "acct"
    drop.mkdir(parents=True, exist_ok=True)
    arch = drop / name
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("homedir/index.php", marker),
                           ("homedir/config.php", b"<?php 1234567890"),
                           ("mysql/db.sql", b"INSERT INTO x VALUES (1);")]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    arch.write_bytes(buf.getvalue())
    return arch


def test_ingest_page_renders(client):
    r = client.get("/ingest")
    assert r.status_code == 200
    assert "cPanel Ingest" in r.text
    assert "Drop Folder" in r.text
    assert "DB Health" in r.text


def test_base_nav_has_ingest(client):
    r = client.get("/")
    assert r.status_code == 200
    assert '/ingest' in r.text


def test_api_ingest_status(client):
    r = client.get("/api/ingest/status")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    assert "staging" in data and "sqlite" in data and "parquet" in data
    assert data["sqlite"]["files"] >= 0
    assert data["queue"]["archives_total"] == 0


def test_api_ingest_scan_flow(client, env, backends, monkeypatch):
    """Scan starts a background worker that really ingests; a second scan
    while the worker runs is rejected 409 (QA W6/L8). Archive 1 blocks
    mid-ingest so the single-flight branch is deterministic, and the worker
    is joined before the test ends (its open SQLite handle locks the DB file
    on Windows otherwise)."""
    import threading
    _make_cpmove(env)

    first_called = threading.Event()
    release = threading.Event()

    def slow_first(path, account, backends_arg, config, **kw):
        first_called.set()
        release.wait(timeout=20)
        return {"status": "ok", "account": account, "archive": path,
                "members": 3, "bytes": 0, "dupe": 0, "errors": 0,
                "excluded": 0, "skipped_oversize": 0, "disposition_errors": []}

    monkeypatch.setattr(ci, "ingest_archive", slow_first)
    r = client.post("/api/ingest/scan", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"
    assert first_called.wait(timeout=15), "worker never reached archive 1"

    # Single-flight: a scan while the worker is running is rejected.
    r2 = client.post("/api/ingest/scan", json={})
    assert r2.status_code == 409

    release.set()
    s = _wait_ingest_done(client, want_done=1)
    assert s["queue"]["archives_done"] == 1
    assert execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0  # fake wrote nothing

    # Real pass: the worker ingests and joins before the test ends.
    monkeypatch.undo()
    assert client.post("/api/ingest/scan", json={}).status_code == 200
    _wait_ingest_done(client, want_done=1)
    n = execute("SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'").fetchone()[0]
    assert n == 3


def test_api_ingest_scan_missing_drop_root(client):
    r = client.post("/api/ingest/scan", json={"drop_root": "J:/definitely/not/here"})
    assert r.status_code == 404


def test_api_ingest_generations(client, env, backends):
    _make_cpmove(env)
    ci.ingest_tree(str(env / "drop"), backends, _cfg(env))
    r = client.get("/api/ingest/generations", params={"account": "acct"})
    assert r.status_code == 200
    gens = r.json()["generations"]
    assert gens and gens[0]["generation"] == 1
    assert gens[0]["members"] == 3


def _cfg(env):
    from backupme.config import load_config
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(env / "drop")
    cfg.cpanel_ingest.staging_dir = str(env / "staging")
    return cfg


def test_api_ingest_history_templates(client, env, backends):
    pytest.importorskip("duckdb")
    _make_cpmove(env)
    from backupme.core import db_lifecycle as lc
    cfg = _cfg(env)
    ci.ingest_tree(str(env / "drop"), backends, cfg)
    lc.cold_export_to_parquet(cfg)

    r = client.get("/api/ingest/history", params={"template": "recent", "limit": 10})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] >= 3

    r2 = client.get("/api/ingest/history", params={"template": "member_history", "account": "acct"})
    assert r2.status_code == 200
    assert r2.json()["count"] >= 3

    r3 = client.get("/api/ingest/history", params={"template": "bogus"})
    assert r3.status_code == 400


def test_api_ingest_trend(client):
    r = client.get("/api/stats/ingest-trend", params={"days": 30})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_api_ingest_restore_generation(client, env, backends):
    _make_cpmove(env)
    ci.ingest_tree(str(env / "drop"), backends, _cfg(env))
    r = client.post("/api/ingest/restore-generation", json={
        "account": "acct", "generation": 1, "out": str(env / "restore_out")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert data["members"] == 3
    # restore_cpanel already appends /<account> under out.
    assert (Path(data["out"]) / "homedir" / "index.php").read_bytes() == b"<?php echo 1;"

    bad = client.post("/api/ingest/restore-generation", json={"account": "", "generation": 0})
    assert bad.status_code == 400


def test_pause_resume(client):
    assert client.post("/api/ingest/pause").json()["status"] == "paused"
    s = client.get("/api/ingest/status").json()
    assert s["paused"] is True
    assert client.post("/api/ingest/resume").json()["status"] == "resumed"
    s = client.get("/api/ingest/status").json()
    assert s["paused"] is False


def _wait_ingest_done(client, want_done=None):
    import time
    for _ in range(80):
        s = client.get("/api/ingest/status").json()
        if not s["running"] and (want_done is None
                                 or s["queue"]["archives_done"] >= want_done):
            return s
        time.sleep(0.1)
    raise AssertionError(f"ingest worker never finished: {s}")


def test_scan_refuses_while_paused(client, env):
    """A pause is explicit operator consent to not ingest: scan must not
    silently clear it (it used to resume ingest as a side effect)."""
    _make_cpmove(env)
    client.post("/api/ingest/pause")
    r = client.post("/api/ingest/scan", json={})
    assert r.status_code == 409
    assert "paused" in r.json()["error"]
    assert execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    # Explicit resume re-enables scans.
    client.post("/api/ingest/resume")


def test_pause_defers_remaining_archives_mid_scan(client, env, monkeypatch):
    """Archives queued behind a paused worker are deferred, not ingested."""
    import threading
    _make_cpmove(env)
    # Distinct content: identical archives would share an idempotency key and
    # the second one would be skipped on re-scan.
    _make_cpmove(env, name="cpmove-two.tar.gz", marker=b"<?php echo 2;")

    # While paused, scan refuses (explicit control).
    client.post("/api/ingest/pause")
    assert client.post("/api/ingest/scan", json={}).status_code == 409

    # Resume, then make archive 1 block mid-ingest so we can pause mid-scan
    # deterministically before archive 2 is reached.
    client.post("/api/ingest/resume")
    first_called = threading.Event()
    release = threading.Event()

    def slow_first(path, account, backends, config, **kw):
        first_called.set()
        release.wait(timeout=20)
        return {"status": "ok", "account": account, "archive": path,
                "members": 3, "bytes": 0, "dupe": 0, "errors": 0,
                "excluded": 0, "skipped_oversize": 0, "disposition_errors": []}

    monkeypatch.setattr(ci, "ingest_archive", slow_first)
    r = client.post("/api/ingest/scan", json={})
    assert r.status_code == 200
    assert first_called.wait(timeout=15), "worker never reached archive 1"

    client.post("/api/ingest/pause")     # pause while archive 1 is mid-flight
    release.set()
    s = _wait_ingest_done(client, want_done=2)

    assert s["paused"] is True
    assert s["queue"]["archives_total"] == 2
    assert s["queue"]["archives_done"] == 2   # deferred archives count as handled
    assert s["errors"] == []
    # Nothing was ingested for the deferred archive.
    assert execute(
        "SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'"
    ).fetchone()[0] == 0

    # Resume + re-scan ingests both archives for real.
    monkeypatch.undo()
    client.post("/api/ingest/resume")
    assert client.post("/api/ingest/scan", json={}).status_code == 200
    _wait_ingest_done(client, want_done=2)
    assert execute(
        "SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'"
    ).fetchone()[0] == 6


def test_files_page_source_filter(client, env, backends):
    _make_cpmove(env)
    ci.ingest_tree(str(env / "drop"), backends, _cfg(env))
    r = client.get("/files", params={"source": "cpanel/"})
    assert r.status_code == 200
    assert "cpanel/acct" in r.text
    assert "cPanel accounts" in r.text


# ---------------------------------------------------------------------------
# SSE streams (QA coverage M6)
# ---------------------------------------------------------------------------

def _sse_events(resp) -> list[str]:
    raw = ""
    assert resp.headers["content-type"].startswith("text/event-stream")
    for chunk in resp.iter_text():
        raw += chunk
    events = [e for e in raw.split("\n\n") if e.strip()]
    assert events, "no SSE events received"
    assert all(e.startswith("data: ") for e in events), events
    return events


def test_ingest_stream_sse_format_and_termination(client, env):
    """/api/ingest/stream frames events as `data: <json>\\n\\n` and terminates
    with a completed snapshot once the scan finishes."""
    _make_cpmove(env)
    assert client.post("/api/ingest/scan", json={}).status_code == 200
    _wait_ingest_done(client, want_done=1)

    with client.stream("GET", "/api/ingest/stream") as resp:
        events = _sse_events(resp)
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is True
    assert last["running"] is False


def test_rebalance_stream_sse_terminates(client, env, backends):
    """/api/backends/rebalance/stream terminates with a completed snapshot
    carrying the rebalance result."""
    from tests.helpers import add_backend
    add_backend(env, "second", env.parent / "storage2")
    _make_cpmove(env)
    ci.ingest_tree(str(env / "drop"), backends, _cfg(env))

    start = client.post("/api/backends/second/rebalance", json={})
    assert start.status_code == 200, start.text

    with client.stream("GET", "/api/backends/rebalance/stream") as resp:
        events = _sse_events(resp)
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is True
    assert last["running"] is False
    assert (last.get("result") or {}).get("status") == "ok"
    assert last.get("backend") == "second"


# ---------------------------------------------------------------------------
# Web auth token (QA M1) — optional bearer guard over /api/*
# ---------------------------------------------------------------------------

def test_api_unauthenticated_by_default(client):
    """No web.token configured -> the dashboard is open (loopback-only today)."""
    r = client.get("/api/status")
    assert r.status_code == 200


def test_api_requires_bearer_token_when_configured(env):
    from backupme.config import load_config
    cfg = load_config()
    cfg.web.token = "secret-token-123"
    app = create_app(cfg)
    client = TestClient(app)

    # Static pages are public; the admin API is not.
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 401

    # Wrong token -> 401.
    assert client.get(
        "/api/status", headers={"authorization": "Bearer nope"}).status_code == 401

    # Correct token -> 200.
    r = client.get(
        "/api/status", headers={"authorization": "Bearer secret-token-123"})
    assert r.status_code == 200
    assert "backend" in r.json() or "backends" in r.json() or "db" in r.json()

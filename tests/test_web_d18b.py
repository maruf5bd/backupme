"""D18 second-wave tests: the remaining dashboard handlers that ran long
blocking work inline must run it via asyncio.to_thread.

Covers:
- /api/db/check          (PRAGMA integrity_check — the 29s class)
- /api/db/snapshot       (sqlite VACUUM INTO + zstd — seconds on a big DB)
- /api/backup/source and /api/backup (os.walk + real upload pipeline — minutes)
- /health page           (data_health.summary multi-query scan)

Companion to tests/test_web_p2.py — same isolated env/backends fixtures,
TestClient(raise_server_exceptions=False) black-box pattern, and the
gc.collect finalizer for stray portal/executor sqlite handles. Everything
runs against scratch DBs only (never the live database); no destructive
confirm:true is ever sent.
"""
import asyncio
import gc
import hashlib
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute


# ---------------------------------------------------------------------------
# Helpers / fixtures (same pattern as tests/test_web_p2.py)
# ---------------------------------------------------------------------------

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


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


# ---------------------------------------------------------------------------
# /api/db/check — integrity_check off the event loop, shapes unchanged
# ---------------------------------------------------------------------------

def test_db_check_ok_shape_on_scratch_db(client, env, backends):
    """Seeded scratch DB -> the healthy branch returns exactly the old
    {"status": "ok"} shape (qa1 test_db_check_checkpoint pins the same)."""
    result = _upload(env, backends, "seed/checkme.txt", b"db check seed")
    assert "file_id" in result, result
    r = client.post("/api/db/check")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}, r.json()


def test_db_check_error_shape_preserved(client, monkeypatch):
    """The corrupt branch must keep the exact {"status": "error", "errors":
    [...]} shape — the wrap only moved WHERE check_integrity runs, not what
    the caller sees. Patched probe, no destructive operation involved."""
    from backupme.core import database as dbmod
    monkeypatch.setattr(dbmod, "check_integrity",
                        lambda: ["*** in database main *** frame 42 damage"])
    r = client.post("/api/db/check")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "error",
                        "errors": ["*** in database main *** frame 42 damage"]}, r.json()


# ---------------------------------------------------------------------------
# /api/db/snapshot — sqlite backup off the event loop, on the scratch env
# ---------------------------------------------------------------------------

def test_db_snapshot_creates_snapshot_and_op_log(client, env, backends):
    _upload(env, backends, "seed/snapme.txt", b"snapshot seed")
    r = client.post("/api/db/snapshot")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok", body
    assert set(body.keys()) == {"status", "snapshot", "generation"}, body
    assert body["snapshot"].endswith(".db.zst"), body
    assert Path(body["snapshot"]).exists(), "snapshot file must exist on disk"
    snaps = client.get("/api/db/snapshots").json()
    assert [s["name"] for s in snaps] == [body["snapshot"]], snaps
    assert snaps[0]["size"] > 0, snaps
    # op_log bookkeeping still lands completed (writes stay in the handler).
    logs = client.get("/api/logs", params={"op": "snapshot", "limit": 5}).json()
    assert any(o["status"] == "completed" for o in logs), logs


# ---------------------------------------------------------------------------
# Backup trigger endpoints — the blocking upload pipeline must run off the
# event loop (they were fully inline before the D18 wrap; the response
# contract stays synchronous: completed result, not a "started" handshake)
# ---------------------------------------------------------------------------

def _make_upload_probe(calls: dict):
    """Replace upload_file with a probe that records WHERE it ran.

    A to_thread executor thread has NO running event loop, so
    asyncio.get_running_loop() raising RuntimeError there proves the
    blocking pipeline left the event loop. If the handler ever regresses to
    calling upload_file inline, the probe records the loop instead."""
    def probe(**kwargs):
        try:
            calls["loop"] = asyncio.get_running_loop()
        except RuntimeError:
            calls["loop"] = None
        calls["thread"] = threading.get_ident()
        return {"status": "ok", "file_id": 1}
    return probe


def test_backup_source_runs_off_event_loop(client, env, backends, monkeypatch):
    src = Path(env) / "d18b_source.txt"
    src.write_bytes(b"backup source probe")
    calls: dict = {}
    monkeypatch.setattr("backupme.core.uploader.upload_file", _make_upload_probe(calls))
    r = client.post("/api/backup/source", json={"source": str(src)})
    assert r.status_code == 200, r.text
    # Response shape byte-identical to the pre-wrap contract.
    assert r.json() == {"status": "ok", "file_id": 1}, r.json()
    # The blocking work ran on an executor thread, not the event loop.
    assert calls.get("loop") is None, \
        "upload pipeline ran inline on the event loop (D18 regression)"
    assert calls.get("thread") != threading.get_ident(), calls
    # op_log bookkeeping still lands completed.
    logs = client.get("/api/logs", params={"op": "backup", "limit": 5}).json()
    assert any(o["status"] == "completed" for o in logs), logs


def test_backup_all_runs_off_event_loop(env, backends, monkeypatch):
    """/api/backup with a configured source: the os.walk + upload execution
    must run in the to_thread executor, with the response shape unchanged."""
    from backupme.config import load_config
    from backupme.backends import invalidate_cache

    src = Path(env) / "d18b_all_target.txt"
    src.write_bytes(b"backup all probe")
    # The config stores the source posix-style; the handler echoes it back.
    source_str = src.as_posix()
    cfg_path = Path(env) / "config.yaml"
    lines = cfg_path.read_text().splitlines(keepends=True)
    lines.append("backups:\n")
    lines.append(f"- source: {source_str}\n")
    cfg_path.write_text("".join(lines))
    invalidate_cache()

    calls: dict = {}
    monkeypatch.setattr("backupme.core.uploader.upload_file", _make_upload_probe(calls))
    app = create_app(load_config())
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/api/backup")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed", body
        assert body["results"] == [{"source": source_str, "status": "ok", "file_id": 1}], body
    assert calls.get("loop") is None, \
        "backup-all execution ran inline on the event loop (D18 regression)"
    assert calls.get("thread") != threading.get_ident(), calls


def test_backup_source_missing_path_unchanged(client):
    """Validation short-circuit stays in the handler (no thread started)."""
    r = client.post("/api/backup/source", json={"source": "J:\\definitely\\missing\\d18b"})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "error", "message": "Source not found"}, r.json()


# ---------------------------------------------------------------------------
# /health page — the multi-query summary scan off the event loop
# ---------------------------------------------------------------------------

def test_health_page_renders_summary(client, env, backends):
    result = _upload(env, backends, "seed/healthme.txt", b"health page seed")
    assert "file_id" in result, result
    r = client.get("/health")
    assert r.status_code == 200, r.text[:300]
    assert "Data Health" in r.text
    # The seeded upload must be visible in the rendered summary numbers.
    assert 'id="dh-live-files">1</div>' in r.text, "summary.live_files missing"
    assert 'id="dh-no-chunks">0</div>' in r.text, "summary.no_chunks_files missing"
    assert 'id="dh-purge-candidates">0</div>' in r.text, "summary.purge_candidates missing"

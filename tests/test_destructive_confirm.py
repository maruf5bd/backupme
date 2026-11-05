"""Destructive endpoints require confirm:true (backlog B03)."""
import pytest

pytestmark = pytest.mark.usefixtures("env")


@pytest.fixture()
def client(env):
    from backupme.web.app import create_app
    from starlette.testclient import TestClient
    from backupme.config import load_config

    return TestClient(create_app(load_config()), raise_server_exceptions=False)


def test_restart_requires_confirm(client):
    assert client.post("/api/server/restart").status_code == 400
    assert client.post("/api/server/restart", json={"confirm": False}).status_code == 400


def test_restart_with_confirm_does_not_execv(client, monkeypatch):
    calls = []
    monkeypatch.setattr("os.execv", lambda *a, **k: calls.append(a))
    r = client.post("/api/server/restart", json={"confirm": True})
    assert r.status_code == 200
    import time
    deadline = time.time() + 3
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    assert calls, "restart thread should invoke the (patched) execv"
    assert r.json()["status"] == "restarting"


def test_db_restore_requires_confirm(client, monkeypatch):
    calls = []
    from backupme.web import routes as routes_mod
    monkeypatch.setattr(routes_mod.recover_mod, "recover_database",
                        lambda backends: calls.append(1) or {"status": "ok"})
    assert client.post("/api/db/restore").status_code == 400
    assert not calls
    r = client.post("/api/db/restore", json={"confirm": True})
    assert r.status_code == 200
    assert calls


def test_db_rebuild_requires_confirm(client, monkeypatch):
    calls = []
    from backupme.web import routes as routes_mod
    monkeypatch.setattr(routes_mod.recover_mod, "rebuild_from_manifests",
                        lambda: calls.append(1) or {"status": "ok", "files": 0})
    assert client.post("/api/db/rebuild").status_code == 400
    assert not calls
    r = client.post("/api/db/rebuild", json={"confirm": True})
    assert r.status_code == 200
    assert calls


def test_backend_remove_requires_confirm(client, env):
    from pathlib import Path
    cfg = Path(env) / "config.yaml"
    text = cfg.read_text()
    assert "test_local" in text
    assert client.request("DELETE", "/api/backends/remove/test_local").status_code == 400
    assert "test_local" in cfg.read_text()  # untouched
    r = client.request("DELETE", "/api/backends/remove/test_local",
                       json={"confirm": True})
    assert r.status_code == 200
    assert "test_local" not in cfg.read_text()


def test_snapshot_delete_requires_confirm(client, tmp_path):
    snap = tmp_path / "snapshot-test.db.zst"
    snap.write_bytes(b"x")
    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert client.request("DELETE", "/api/db/snapshots/snapshot-test.db.zst").status_code == 400
        assert snap.exists()
        r = client.request("DELETE", "/api/db/snapshots/snapshot-test.db.zst",
                           json={"confirm": True})
        assert r.status_code == 200
        assert not snap.exists()
    finally:
        os.chdir(old_cwd)


def test_snapshot_prune_requires_confirm(client, tmp_path):
    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert client.post("/api/db/snapshots/prune?keep=1").status_code == 400
        r = client.post("/api/db/snapshots/prune?keep=1", json={"confirm": True})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
    finally:
        os.chdir(old_cwd)

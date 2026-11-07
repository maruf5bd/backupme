"""POST /api/restore/files: restoring an explicit id set (e.g. search
results) to a server-side folder — the surface behind the files-page
buttons "Restore all results to folder" / "Restore selected to folder".

Covers: real restore of two files with skip accounting for unknown ids,
version dedupe (latest id wins per relative_path), bad-input 400s, and
409 while another restore is running.
"""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from tests.helpers import backup_bytes


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    app = create_app(load_config())
    return TestClient(app)


@pytest.fixture()
def seeded(env, backends):
    alpha = backup_bytes(Path(env), "seed/alpha.txt", b"alpha payload", backends)
    beta = backup_bytes(Path(env), "seed/beta.txt", b"beta payload", backends)
    return {"alpha_id": alpha["file_id"], "beta_id": beta["file_id"], "root": Path(env)}


def _wait_restore_done(client, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get("/api/restore/status").json()
        if state["completed"] and not state["running"]:
            return state
        time.sleep(0.1)
    raise AssertionError("restore never completed")


def test_restore_files_restores_and_skips_unknown(client, seeded):
    dest = seeded["root"] / "restore_out_files"
    r = client.post("/api/restore/files", json={
        "file_ids": [seeded["alpha_id"], seeded["beta_id"], 999999],
        "dest_path": str(dest)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "started"
    assert body["restoring"] == 2
    assert body["skipped"] == 1

    state = _wait_restore_done(client)
    assert state["errors"] == 0, state["error_list"]
    assert (dest / "seed" / "alpha.txt").read_bytes() == b"alpha payload"
    assert (dest / "seed" / "beta.txt").read_bytes() == b"beta payload"


def test_restore_files_latest_version_wins(client, seeded, env, backends):
    v2 = backup_bytes(Path(env), "seed/alpha.txt", b"alpha v2", backends)
    dest = seeded["root"] / "restore_out_versions"
    r = client.post("/api/restore/files", json={
        "file_ids": [seeded["alpha_id"], v2["file_id"]],
        "dest_path": str(dest)})
    assert r.status_code == 200, r.text
    assert r.json()["restoring"] == 1  # same path -> restore once, latest id

    state = _wait_restore_done(client)
    assert state["errors"] == 0, state["error_list"]
    assert (dest / "seed" / "alpha.txt").read_bytes() == b"alpha v2"


def test_restore_files_rejects_bad_input(client, seeded):
    for payload in (
        {"file_ids": [], "dest_path": "x"},
        {"file_ids": "nope", "dest_path": "x"},
        {"file_ids": [seeded["alpha_id"]], "dest_path": ""},
        {"file_ids": [999999], "dest_path": "x"},
        {"dest_path": "x"},
    ):
        r = client.post("/api/restore/files", json=payload)
        assert r.status_code == 400, (payload, r.text)
        assert "error" in r.json()


def test_restore_files_conflicts_with_running_restore(client, seeded, monkeypatch):
    import time as _time
    from backupme.web.routes import files as files_routes

    real_download = files_routes.downloader_mod.download_file

    def slow_download(file_id, out_path):
        _time.sleep(0.6)
        return real_download(file_id, out_path)

    monkeypatch.setattr(files_routes.downloader_mod, "download_file", slow_download)

    dest = seeded["root"] / "restore_out_busy"
    r = client.post("/api/restore/files", json={
        "file_ids": [seeded["alpha_id"]], "dest_path": str(dest)})
    assert r.status_code == 200, r.text

    r2 = client.post("/api/restore/files", json={
        "file_ids": [seeded["beta_id"]], "dest_path": str(dest) + "_2"})
    assert r2.status_code == 409, r2.text
    assert "already running" in r2.json()["error"]

    state = _wait_restore_done(client)
    assert state["errors"] == 0, state["error_list"]

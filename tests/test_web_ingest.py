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


def _make_cpmove(env):
    drop = env / "drop" / "acct"
    drop.mkdir(parents=True, exist_ok=True)
    arch = drop / "cpmove-acct.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("homedir/index.php", b"<?php echo 1;"),
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


def test_api_ingest_scan_flow(client, env, backends):
    _make_cpmove(env)
    r = client.post("/api/ingest/scan", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"

    # Wait for the background worker to finish (bounded).
    import time
    for _ in range(60):
        s = client.get("/api/ingest/status").json()
        if not s["running"] and s["queue"]["archives_done"] >= 1:
            break
        time.sleep(0.2)
    assert s["queue"]["archives_done"] == 1
    n = execute("SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'").fetchone()[0]
    assert n == 3

    # Single-flight: second scan while... (worker finished; just verify 200/idle)
    r2 = client.post("/api/ingest/scan", json={})
    assert r2.status_code == 200


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


def test_files_page_source_filter(client, env, backends):
    _make_cpmove(env)
    ci.ingest_tree(str(env / "drop"), backends, _cfg(env))
    r = client.get("/files", params={"source": "cpanel/"})
    assert r.status_code == 200
    assert "cpanel/acct" in r.text
    assert "cPanel accounts" in r.text

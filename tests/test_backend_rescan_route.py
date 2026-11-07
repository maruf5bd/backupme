"""Web surface for backend rescan (backlog B13): POST /api/backends/{name}/rescan.

Error conventions mirror the sibling backend routes: 404 for an unknown
backend (rebalance/drain style), 502 only when the backend cannot connect,
and the JSON-ready rescan_backend dict on success.
"""
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute, commit


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _insert_row(backend_name: str, path: str, size: int):
    # FK enforcement is ON: the storage row needs its parent blobs row first
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
            (_sha(path), size))
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
        "VALUES (?, ?, ?, ?, ?)",
        (_sha(path), backend_name, path, size, _sha("stored-" + path)),
    )
    commit()


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    cfg = load_config()
    app = create_app(cfg)
    return TestClient(app)


@pytest.fixture()
def local_backend(backends):
    bk = backends[0]
    base = Path(bk.base_path)
    base.mkdir(parents=True, exist_ok=True)
    (base / "objects" / "aa").mkdir(parents=True, exist_ok=True)
    (base / "objects" / "bb").mkdir(parents=True, exist_ok=True)
    (base / "objects" / "aa" / "ok.bin").write_bytes(b"x" * 11)
    (base / "objects" / "bb" / "extra.bin").write_bytes(b"y" * 7)
    return bk


def test_rescan_unknown_backend_returns_404(client):
    r = client.post("/api/backends/does-not-exist/rescan")
    assert r.status_code == 404
    body = r.json()
    assert "not found" in body.get("error", "")


def test_rescan_route_reports_missing_extra_and_mismatch(client, local_backend):
    bk = local_backend
    _insert_row(bk.name, "objects/aa/ok.bin", 11)          # present, size matches
    _insert_row(bk.name, "objects/cc/lost.bin", 5)         # in DB, not on box

    r = client.post(f"/api/backends/{bk.name}/rescan")
    assert r.status_code == 200
    data = r.json()
    assert data["backend"] == bk.name
    assert data["missing_on_box"] == ["objects/cc/lost.bin"]
    assert data["extra_on_box"] == ["objects/bb/extra.bin"]
    assert data["missing_bytes"] == 5
    assert data["size_mismatch"] == []
    assert data["walk_files"] >= 2

    # Corrupt/changed file: same path, different size -> size mismatch via route.
    (Path(bk.base_path) / "objects" / "aa" / "ok.bin").write_bytes(b"z" * 12)
    r = client.post(f"/api/backends/{bk.name}/rescan")
    assert r.status_code == 200
    data = r.json()
    assert len(data["size_mismatch"]) == 1
    assert data["size_mismatch"][0]["path"] == "objects/aa/ok.bin"
    assert data["size_mismatch"][0]["db_size"] == 11
    assert data["size_mismatch"][0]["box_size"] == 12

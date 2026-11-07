"""Health surface consolidation (backlog B04).

/api/health/data is the single canonical health payload; /api/health/detailed
must stay up as a thin deprecated alias returning the exact same JSON shape.
"""
import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    cfg = load_config()
    app = create_app(cfg)
    return TestClient(app)


def test_health_data_shape_intact(client):
    """Canonical endpoint keeps its summary + last-scan-snapshot shape."""
    r = client.get("/api/health/data")
    assert r.status_code == 200
    data = r.json()

    assert set(data.keys()) == {"summary", "scan", "scan_running", "scan_age_seconds"}

    # summary: the cheap-SQL snapshot from data_health.summary()
    summary = data["summary"]
    for key in ("live_files", "no_chunks_files", "missing_blob_files",
                "missing_blobs", "backend_blobs", "same_name_groups",
                "same_name_files", "purge_candidates"):
        assert key in summary
    assert isinstance(summary["backend_blobs"], list)

    # last scan snapshot fields (idle app: never scanned, not running)
    scan = data["scan"]
    for key in ("running", "started_at", "finished_at", "completed",
                "results", "remote", "errors"):
        assert key in scan
    assert data["scan_running"] is False
    assert scan["running"] is False
    assert scan["results"] == {}


def test_health_detailed_returns_canonical_shape(client):
    """Deprecated alias must expose the same top-level keys as the canonical
    endpoint (and an identical summary payload)."""
    canonical = client.get("/api/health/data")
    assert canonical.status_code == 200
    legacy = client.get("/api/health/detailed")
    assert legacy.status_code == 200

    assert set(legacy.json().keys()) == set(canonical.json().keys())
    assert legacy.json()["summary"] == canonical.json()["summary"]

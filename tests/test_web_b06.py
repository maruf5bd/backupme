"""B06 adoption + /api/recompress rework regression tests.

Covers:
- POST /api/recompress runs through core compress.recompress_blobs, surfaces
  its counters (with the legacy "converted" alias settings.html reads) and
  audits the run to op_log (op_type 'recompress');
- the op_log row's target_json holds the counters dict;
- the shared op_log helpers keep their never-raise contract (B06/B07): a
  failing op_log INSERT is logged through backupme.web.routes and the
  endpoint response is unchanged.
"""
import json
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backupme.core import database as dbmod
from backupme.core import compress as compress_mod
from backupme.core.database import execute, commit


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    from backupme.web.app import create_app
    return TestClient(create_app(load_config()))


def _seed_non_zstd_blob(backend_name: str = "test_local") -> str:
    """Seed one gzip-compressed blob_storage row in the scratch DB."""
    blob_id = "b" * 64
    execute(
        "INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
        (blob_id, 100),
    )
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, "
        "stored_sha256, compression_algo, compression_level, encrypted) "
        "VALUES (?, ?, ?, 80, ?, 'gzip', 6, 0)",
        (blob_id, backend_name, f"chunks/{blob_id}.gz", "c" * 64),
    )
    commit()
    return blob_id


def test_recompress_returns_counters_and_writes_op_log(client, monkeypatch):
    """The endpoint delegates to core recompress_blobs, returns its counters
    (plus the legacy converted alias) and audits the run to op_log."""
    _seed_non_zstd_blob()
    counters = {"recompressed": 5, "total_scanned": 6,
                "skipped_encrypted": 1, "failed": 0}
    calls = {}

    def fake_recompress(algo="zstd", level=3, backend_name=None, key=None):
        calls["kwargs"] = {"algo": algo, "level": level,
                           "backend_name": backend_name, "key": key}
        return dict(counters)

    monkeypatch.setattr(compress_mod, "recompress_blobs", fake_recompress)
    r = client.post("/api/recompress")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    for key, value in counters.items():
        assert body[key] == value
    # Legacy alias kept: settings.html reads result.converted.
    assert body["converted"] == 5
    # Endpoint defaults match the old inline behavior: zstd level 3, every
    # backend (no backend param exists in the request contract today).
    assert calls["kwargs"] == {"algo": "zstd", "level": 3,
                               "backend_name": None, "key": None}
    row = execute(
        "SELECT status, target_json FROM op_log WHERE op_type = 'recompress' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "recompress must be audited to op_log"
    assert row["status"] == "completed"


def test_recompress_op_log_target_json_holds_counters(client, monkeypatch):
    """The completed op_log row's target_json is exactly the counters dict
    (that is what /api/report's op_counters surface for 'recompress')."""
    _seed_non_zstd_blob()
    counters = {"recompressed": 2, "total_scanned": 3,
                "skipped_encrypted": 0, "failed": 1}
    monkeypatch.setattr(compress_mod, "recompress_blobs",
                        lambda *args, **kwargs: dict(counters))
    r = client.post("/api/recompress")
    assert r.status_code == 200
    row = execute(
        "SELECT target_json, completed_at FROM op_log WHERE op_type = 'recompress' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert json.loads(row["target_json"]) == counters
    assert row["completed_at"] is not None


def test_db_snapshot_op_log_failure_logs_and_response_unchanged(client, monkeypatch, caplog):
    """B06 adoption: after moving dbops.py onto the shared op_log helpers the
    failure path still never breaks the endpoint — a failing op_log INSERT is
    logged through backupme.web.routes (helpers log their own failures, B07
    style) and the /api/db/snapshot response keeps its original contract."""
    real_execute = dbmod.execute

    def failing_execute(sql, params=()):
        if "INSERT INTO op_log" in sql:
            raise sqlite3.OperationalError("op_log insert exploded")
        return real_execute(sql, params)

    monkeypatch.setattr(dbmod, "execute", failing_execute)
    with caplog.at_level(logging.ERROR, logger="backupme.web.routes"):
        r = client.post("/api/db/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "snapshot" in body and "generation" in body
    records = [rec for rec in caplog.records if rec.name == "backupme.web.routes"]
    assert records, "expected the op_log start failure to be logged"
    assert any("op_log start failed" in rec.getMessage() for rec in records)
    # The audit row itself could not be written — nothing partial left behind.
    row = execute("SELECT id FROM op_log WHERE op_type = 'snapshot'").fetchone()
    assert row is None

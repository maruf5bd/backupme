"""Fold-in pinning tests: crypto capabilities in /api/encrypt/status (B14),
per-op counters in /api/report (B09), op_log audit for /api/health/purge (B12)."""
import json

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute, commit


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    cfg = load_config()
    app = create_app(cfg)
    return TestClient(app)


def test_encrypt_status_includes_capabilities(client):
    r = client.get("/api/encrypt/status")
    assert r.status_code == 200
    body = r.json()
    # Legacy keys preserved (old callers read these).
    assert {"key_exists", "key_path", "key_size"} <= set(body.keys())
    # B14: cipher/kdf/file-layout surfaced for every caller.
    caps = body["capabilities"]
    for key in ("cipher", "kdf", "aead_available", "file_layout"):
        assert key in caps
    assert caps["cipher"] == "aes-256-gcm"


def test_report_includes_op_counters(client):
    # Seed a completed recompress op with its B09 counters in target_json.
    execute(
        "INSERT INTO op_log (idempotency_key, op_type, target_json, status, started_at, "
        "completed_at) VALUES (?, ?, ?, 'completed', datetime('now'), datetime('now'))",
        ("test:recompress:1", "recompress",
         json.dumps({"recompressed": 3, "skipped_encrypted": 1, "failed": 0})),
    )
    commit()
    r = client.get("/api/report")
    assert r.status_code == 200
    body = r.json()
    assert "op_counters" in body
    rec = body["op_counters"].get("recompress")
    assert rec is not None
    assert rec["status"] == "completed"
    assert rec["skipped_encrypted"] == 1
    assert rec["failed"] == 0


def test_health_purge_writes_op_log_audit(client):
    # A file with no chunks is a purge candidate.
    fid = execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
        "VALUES ('docs', 'purge-me.txt', ?, 10, 'vg-p')",
        ("a" * 64,),
    ).lastrowid
    commit()
    r = client.post("/api/health/purge", json={"confirm": True, "file_ids": [fid]})
    assert r.status_code == 200
    row = execute(
        "SELECT status, target_json FROM op_log WHERE op_type = 'purge' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "purge must be audited to op_log"
    assert row["status"] == "completed"
    payload = json.loads(row["target_json"])
    assert payload["requested"] == 1
    assert payload["ok"] == 1
    # The file itself is soft-deleted by the purge.
    deleted = execute("SELECT deleted_at FROM files WHERE id = ?", (fid,)).fetchone()
    assert deleted["deleted_at"] is not None


def test_health_purge_rejected_request_writes_no_op_log(client):
    # confirm gate refuses before any purge work — no audit row either.
    r = client.post("/api/health/purge", json={"confirm": False, "file_ids": [1]})
    assert r.status_code == 400
    row = execute("SELECT id FROM op_log WHERE op_type = 'purge'").fetchone()
    assert row is None

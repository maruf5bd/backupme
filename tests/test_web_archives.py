"""Dashboard Archives surface (extract-first archives, Phase 2): tests.

Covers the /archives page render (real engine seeding, test_archives.py
pattern), the /api/archives list + detail shape with pagination, the
confirm gate on /api/archives/restore (exactly-true semantics), a full
sha-matched restore through the endpoint, and the unsafe-dest_path guard.
Auth-gate compatibility is deliberately out of scope (token tests live in
tests/test_web_token_config.py / qa1_web_sweep.py).
"""
import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.config import load_config
from backupme.core import archives as archives_mod
from backupme.core.database import execute

from tests.test_archives import build_tar_gz


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_id(fp: str) -> int:
    return execute(
        "SELECT id FROM archives WHERE fingerprint = ?", (fp,)
    ).fetchone()[0]


@pytest.fixture()
def app(env, backends):
    return create_app(load_config())


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def seeded_archives(env, backends):
    """Two archives through the REAL engine (like tests/test_archives.py)."""
    r1 = archives_mod.ingest_archive_file(
        str(_write_archive(Path(env), "first.tar.gz",
                           {"docs/readme.txt": b"archive one", "sub/b.bin": b"BB" * 32})),
        backends, load_config())
    r2 = archives_mod.ingest_archive_file(
        str(_write_archive(Path(env), "second.tar.gz",
                           {"x.txt": b"second archive"})),
        backends, load_config())
    assert r1["status"] == "archive_ingested" and r2["status"] == "archive_ingested"
    r1["id"] = _archive_id(r1["fingerprint"])
    r2["id"] = _archive_id(r2["fingerprint"])
    return {"first": r1, "second": r2}


def _write_archive(root: Path, name: str, members: dict) -> Path:
    path = root / name
    path.write_bytes(build_tar_gz(members))
    return path


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def test_archives_page_renders_table(client, seeded_archives):
    r = client.get("/archives")
    assert r.status_code == 200
    body = r.text
    assert "Archives" in body
    # The table renders the real engine's fingerprints (short form) and names.
    assert seeded_archives["first"]["fingerprint"][:16] in body
    assert seeded_archives["second"]["fingerprint"][:16] in body
    assert "first.tar.gz" in body and "second.tar.gz" in body


def test_archives_page_empty_state_renders(client):
    r = client.get("/archives")
    assert r.status_code == 200
    assert "No archives ingested yet" in r.text


def test_archives_in_nav(client):
    body = client.get("/files").text
    assert 'href="/archives"' in body


# ---------------------------------------------------------------------------
# List + detail APIs
# ---------------------------------------------------------------------------

def test_api_archives_list_shape(client, seeded_archives):
    r = client.get("/api/archives")
    assert r.status_code == 200
    payload = r.json()
    assert payload["total"] == 2
    assert payload["limit"] == 50 and payload["offset"] == 0
    assert len(payload["archives"]) == 2
    row = payload["archives"][0]
    for key in ("id", "fingerprint", "format", "original_name",
                "member_count", "total_uncompressed", "created_at"):
        assert key in row
    # newest id first
    assert row["fingerprint"] == seeded_archives["second"]["fingerprint"]


def test_api_archives_list_pagination(client, seeded_archives):
    r = client.get("/api/archives?limit=1&offset=1")
    payload = r.json()
    assert payload["total"] == 2
    assert len(payload["archives"]) == 1
    assert payload["archives"][0]["fingerprint"] == \
        seeded_archives["first"]["fingerprint"]


def test_api_archives_detail_shape_and_404(client, seeded_archives):
    detail = client.get(f"/api/archives/{seeded_archives['first']['id']}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["archive"]["fingerprint"] == seeded_archives["first"]["fingerprint"]
    assert payload["total"] == 2
    paths = {m["relative_path"] for m in payload["members"]}
    assert paths == {"docs/readme.txt", "sub/b.bin"}
    for m in payload["members"]:
        for key in ("id", "relative_path", "file_id", "member_sha256",
                    "member_size", "mtime", "nested_archive"):
            assert key in m

    missing = client.get("/api/archives/999999")
    assert missing.status_code == 404
    assert missing.json() == {"error": "not found"}


# ---------------------------------------------------------------------------
# Restore endpoint
# ---------------------------------------------------------------------------

def _restore_body(fp, dest, **extra):
    body = {"fingerprint": fp, "dest_path": dest}
    body.update(extra)
    return body


def test_restore_confirm_gate(client, seeded_archives, tmp_path):
    fp = seeded_archives["first"]["fingerprint"]
    dest = str(tmp_path / "out")
    # Missing confirm entirely.
    r = client.post("/api/archives/restore", json=_restore_body(fp, dest))
    assert r.status_code == 400
    assert r.json() == {"error": "confirm must be exactly true"}
    # String "true" is rejected (exactly-true semantics, dbops pattern).
    r = client.post("/api/archives/restore",
                    json=_restore_body(fp, dest, confirm="true"))
    assert r.status_code == 400
    assert r.json() == {"error": "confirm must be exactly true"}
    # Truthy 1 is not the literal True.
    r = client.post("/api/archives/restore",
                    json=_restore_body(fp, dest, confirm=1))
    assert r.status_code == 400


def test_restore_requires_fields(client, seeded_archives):
    r = client.post("/api/archives/restore", json={"confirm": True})
    assert r.status_code == 400
    assert r.json() == {"error": "fingerprint and dest_path required"}


def test_restore_unknown_fingerprint_404(client, seeded_archives, tmp_path):
    r = client.post("/api/archives/restore", json=_restore_body(
        "f" * 64, str(tmp_path / "out"), confirm=True))
    assert r.status_code == 404
    assert r.json()["status"] == "error"


def test_restore_unsafe_dest_rejected(client, seeded_archives):
    fp = seeded_archives["first"]["fingerprint"]
    # "" is rejected earlier as a missing field (required-fields test).
    for bad in ("../arch3_evil", "sub/../../arch3_evil", "C:", "/",
                "C:relative"):
        r = client.post("/api/archives/restore",
                        json=_restore_body(fp, bad, confirm=True))
        assert r.status_code == 400, bad
        assert r.json() == {"error": "unsafe dest_path rejected"}, bad


def test_restore_writes_members_sha_matched(client, seeded_archives, tmp_path):
    fp = seeded_archives["first"]["fingerprint"]
    dest = tmp_path / "restored"
    r = client.post("/api/archives/restore", json=_restore_body(
        fp[:12], str(dest), confirm=True))   # prefix fingerprint accepted
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "ok"
    assert payload["members"] == 2 and payload["errors"] == 0
    assert payload["fingerprint"] == fp

    readme = dest / "docs" / "readme.txt"
    blob = dest / "sub" / "b.bin"
    assert readme.is_file() and blob.is_file()
    assert _sha(readme.read_bytes()) == _sha(b"archive one")
    assert _sha(blob.read_bytes()) == _sha(b"BB" * 32)


def test_restore_dest_must_be_a_directory(client, seeded_archives, tmp_path):
    fp = seeded_archives["first"]["fingerprint"]
    occupied = tmp_path / "occupied.file"
    occupied.write_text("not a dir")
    r = client.post("/api/archives/restore", json=_restore_body(
        fp, str(occupied), confirm=True))
    assert r.status_code == 400
    assert r.json() == {"error": "dest_path exists and is not a directory"}

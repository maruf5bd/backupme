"""Web upload destination folder: files land under web_upload/<dest_folder>/
<filename>; traversal and absolute/drive paths are rejected; the no-dest
behavior is unchanged."""
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    app = create_app(load_config())
    return TestClient(app)


def _upload(client, name, payload=b"dest test payload", dest=""):
    data = {"file": (name, BytesIO(payload), "text/plain")}
    form = {}
    if dest:
        form["dest_folder"] = dest
    return client.post("/api/upload/file", data=form, files=data)


def test_upload_without_dest_keeps_flat_layout(client):
    r = _upload(client, "plain.txt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["dest"] == "plain.txt"
    row = execute(
        "SELECT relative_path FROM files WHERE source_root = 'web_upload' "
        "AND relative_path = 'plain.txt' AND deleted_at IS NULL").fetchone()
    assert row is not None


def test_upload_with_dest_folder_lands_inside_it(client):
    r = _upload(client, "report.pdf", b"%PDF-ish", dest="projects/2026")
    assert r.status_code == 200, r.text
    assert r.json()["dest"] == "projects/2026/report.pdf"
    row = execute(
        "SELECT id FROM files WHERE source_root = 'web_upload' "
        "AND relative_path = 'projects/2026/report.pdf' AND deleted_at IS NULL").fetchone()
    assert row is not None
    # and it restores through the normal pipeline
    file_id = row["id"]
    dl = client.get(f"/api/download/{file_id}")
    assert dl.status_code == 200
    assert dl.content == b"%PDF-ish"


def test_upload_rejects_traversal_and_absolute_dest(client):
    for bad in ("../evil", "..\\evil", "C:\\evil", "a/../b", "."):
        r = _upload(client, "x.txt", dest=bad)
        assert r.status_code == 400, (bad, r.text)
        assert "relative folder path" in r.json()["message"]
    rows = execute(
        "SELECT COUNT(*) FROM files WHERE source_root = 'web_upload' "
        "AND relative_path LIKE '%evil%'").fetchone()[0]
    assert rows == 0


def test_upload_normalizes_leading_slash_dest(client):
    # "/abs" is harmless after normalization: it becomes a relative folder.
    r = _upload(client, "x.txt", dest="/abs")
    assert r.status_code == 200, r.text
    assert r.json()["dest"] == "abs/x.txt"


def test_upload_sanitizes_path_components_in_filename(client):
    r = _upload(client, "sub/dir/naughty.txt")
    assert r.status_code == 200, r.text
    assert r.json()["dest"] == "naughty.txt"  # browser-style name only

"""Regression tests for the QA black-box dashboard sweep (qa defects D3-D5):

- D3: search endpoints and the /files page never 500 on user-typed
  punctuation (raw q used to hit FTS5 MATCH -> 'syntax error near "."').
- D4: /api/browse descends into forward-slash rows (all cPanel-ingested
  paths) as well as backslash rows.
- D5: unknown file ids on /api/download/{id} and /api/files/{id}/preview
  return 404 with a JSON error body instead of 500.
"""
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import write_txn
from tests.helpers import backup_bytes


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    app = create_app(load_config())
    return TestClient(app)


@pytest.fixture()
def seeded(env, backends):
    """One real uploaded file so FTS/LIKE search has a row to find."""
    result = backup_bytes(Path(env), "seed/delta.txt", b"delta content 123", backends)
    return {"file_id": result["file_id"], "root": str(env)}


# ---------------------------------------------------------------------------
# D3: search never 500s on punctuation
# ---------------------------------------------------------------------------

def test_files_search_finds_filename_with_dot(client, seeded):
    r = client.get("/api/files", params={"q": "delta.txt"})
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
    assert seeded["file_id"] in [f.get("id") for f in r.json()]


def test_search_endpoint_finds_filename_with_dot(client, seeded):
    r = client.get("/api/search", params={"q": "delta.txt"})
    assert r.status_code == 200, r.text
    assert seeded["file_id"] in [f.get("id") for f in r.json()]


def test_files_page_renders_with_punctuation_query(client, seeded):
    r = client.get("/files", params={"q": "delta.txt"})
    assert r.status_code == 200, r.text


def test_plain_word_search_still_matches(client, seeded):
    for endpoint in ("/api/files", "/api/search"):
        r = client.get(endpoint, params={"q": "delta"})
        assert r.status_code == 200
        assert seeded["file_id"] in [f.get("id") for f in r.json()]


@pytest.mark.parametrize("q", [
    'a"b', "((", ")", "*", "**", "NEAR(a b", "a OR (", "^x", "\\delta",
    "delta.txt", "x*y?z", ":", "; DROP TABLE files; --", "*****",
])
def test_hostile_queries_never_500(client, seeded, q):
    for path in ("/api/files", "/api/search", "/files"):
        r = client.get(path, params={"q": q})
        assert r.status_code == 200, f"{path}?q={q!r} -> {r.status_code}"


# ---------------------------------------------------------------------------
# D4: browse handles both separator styles
# ---------------------------------------------------------------------------

def _insert_row(source_root: str, relative_path: str, size: int = 10) -> int:
    sha = hashlib.sha256(relative_path.encode()).hexdigest()
    with write_txn() as conn:
        fid = conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
            "VALUES (?,?,?,?,?)",
            (source_root, relative_path, sha, size, f"vg-{relative_path}"),
        ).lastrowid
    return fid


@pytest.fixture()
def fs_tree():
    """Forward-slash tree, exactly like cPanel-ingested rows."""
    root = "R"
    ids = {
        "nested_log": _insert_row(root, "sub/nested/epsilon.log", 30),
        "alpha": _insert_row(root, "sub/alpha.txt", 11),
        "beta": _insert_row(root, "sub/beta.txt", 12),
        "top": _insert_row(root, "rootfile.txt", 5),
    }
    return {"root": root, **ids}


def test_browse_descends_forward_slash_path(client, fs_tree):
    r = client.get("/api/browse", params={"root": fs_tree["root"], "path": "sub"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["dirs"] == ["nested"]
    got = {f["relative_path"] for f in data["files"]}
    assert got == {"sub/alpha.txt", "sub/beta.txt"}


def test_browse_accepts_trailing_separator(client, fs_tree):
    for path in ("sub/", "sub\\"):
        r = client.get("/api/browse", params={"root": fs_tree["root"], "path": path})
        assert r.status_code == 200
        data = r.json()
        assert data["dirs"] == ["nested"], f"path={path!r} -> {data}"
        assert len(data["files"]) == 2


def test_browse_top_level_lists_forward_slash_dirs(client, fs_tree):
    r = client.get("/api/browse", params={"root": fs_tree["root"]})
    assert r.status_code == 200
    data = r.json()
    assert data["dirs"] == ["sub"]
    assert {f["relative_path"] for f in data["files"]} == {"rootfile.txt"}


def test_browse_backslash_rows_still_work(client):
    _insert_row("B", "bs\\deep\\f.txt", 7)
    _insert_row("B", "bs\\top.txt", 3)
    r = client.get("/api/browse", params={"root": "B", "path": "bs"})
    assert r.status_code == 200
    data = r.json()
    assert data["dirs"] == ["deep"]
    assert {f["relative_path"] for f in data["files"]} == {"bs\\top.txt"}


def test_browse_second_level_forward_slash(client, fs_tree):
    r = client.get("/api/browse", params={"root": fs_tree["root"], "path": "sub/nested"})
    assert r.status_code == 200
    data = r.json()
    assert data["dirs"] == []
    assert {f["relative_path"] for f in data["files"]} == {"sub/nested/epsilon.log"}


# ---------------------------------------------------------------------------
# D5: unknown file id -> 404 with JSON body
# ---------------------------------------------------------------------------

def test_download_unknown_id_returns_404_json(client, seeded):
    r = client.get("/api/download/999999")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body
    assert "999999" in body["error"]


def test_preview_unknown_id_returns_404_json(client, seeded):
    r = client.get("/api/files/999999/preview")
    assert r.status_code == 404
    body = r.json()
    assert body["status"] == "error"
    assert "999999" in body["message"]


def test_download_soft_deleted_id_returns_404(client, seeded):
    assert client.delete(f"/api/files/{seeded['file_id']}").status_code == 200
    r = client.get(f"/api/download/{seeded['file_id']}")
    assert r.status_code == 404
    r2 = client.get(f"/api/files/{seeded['file_id']}/preview")
    assert r2.status_code == 404


def test_download_known_id_still_round_trips(client, seeded):
    r = client.get(f"/api/download/{seeded['file_id']}")
    assert r.status_code == 200, r.text
    assert r.content == b"delta content 123"

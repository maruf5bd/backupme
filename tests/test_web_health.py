"""Tests for the Data Health dashboard: live issue buckets, physical
reachability scan, and confirm-gated purge."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute, write_txn
from backupme.backends import invalidate_cache
import backupme.core.data_health as dh


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    cfg = load_config()
    app = create_app(cfg)
    return TestClient(app)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _write_backend_file(root: Path, rel: str, data: bytes) -> int:
    p = root / rel.replace("/", os.sep)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return len(data)


@pytest.fixture()
def fixture_ids(env, backends):
    """Curated broken/healthy dataset:

    - healthy file (chunk -> stored object, real file on disk)
    - three no-chunk files
    - orphan file (chunk -> blob with zero blob_storage rows)
    - same-name group (alpha/report.pdf + beta/report.pdf, both healthy)
    - bundle member whose parent bundle file exists (reachable)
    - bundle member whose parent bundle file is missing on disk
    """
    backend = next(b for b in backends if b.name == "test_local")
    root = Path(backend.base_path)
    backend_name = backend.name
    ids = {"backend": backend_name}
    with write_txn() as conn:
        # Healthy file: chunk -> object blob with a real file at the root.
        b1 = _sha("b1")
        conn.execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?,?,1)",
                     (b1, 64))
        obj1 = f"objects/{b1[:2]}/{b1[2:4]}/{b1}"
        conn.execute(
            "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
            "VALUES (?,?,?,?,?)", (b1, backend_name, obj1, 64, _sha(obj1)))
        assert _write_backend_file(root, obj1, b"h" * 64) == 64
        fid = conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
            "VALUES ('docs', 'healthy.txt', ?, 100, 'vg-h')", (_sha("healthy"),)
        ).lastrowid
        conn.execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
                     "VALUES (?, 0, ?, 0, 64)", (fid, b1))
        ids["healthy"] = fid

        # No-chunk files: content never stored.
        ids["no_chunks"] = []
        for i, rel in enumerate(("logs/a.log", "logs/b.log", "logs/c.log")):
            fid = conn.execute(
                "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
                "VALUES ('logs', ?, ?, ?, ?)", (rel, _sha(rel), 10 * (i + 1), f"vg-{i}")
            ).lastrowid
            ids["no_chunks"].append(fid)

        # Orphan file: chunk references a blob with no blob_storage row.
        b3 = _sha("b3")
        conn.execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?,?,1)",
                     (b3, 50))
        fid = conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
            "VALUES ('vault', 'orphan.bin', ?, 50, 'vg-o')", (_sha("orphan"),)
        ).lastrowid
        conn.execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
                     "VALUES (?, 0, ?, 0, 50)", (fid, b3))
        ids["orphan"] = fid
        ids["orphan_blob"] = b3

        # Same-name group across source roots, both healthy.
        ids["same"] = []
        for seed, src in (("b4", "alpha"), ("b5", "beta")):
            b = _sha(seed)
            conn.execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?,?,1)",
                         (b, 32))
            obj = f"objects/{b[:2]}/{b[2:4]}/{b}"
            conn.execute(
                "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
                "VALUES (?,?,?,?,?)", (b, backend_name, obj, 32, _sha(obj)))
            _write_backend_file(root, obj, b"r" * 32)
            rel = "report.pdf"
            fid = conn.execute(
                "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
                "VALUES (?, ?, ?, 200, ?)", (src, rel, _sha(seed + rel), f"vg-{seed}")
            ).lastrowid
            conn.execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
                         "VALUES (?, 0, ?, 0, 32)", (fid, b))
            ids["same"].append(fid)

        # Bundle member whose parent bundle file IS on disk (16 bytes, member
        # range 8..13 fits).
        tar1 = _sha("tar1")
        p1 = f"bundle/{tar1[:2]}/{tar1[2:4]}/{tar1}"
        conn.execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?,?,1)",
                     (p1, 16))
        conn.execute(
            "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
            "VALUES (?,?,?,?,?)", (p1, backend_name, p1, 16, _sha(p1)))
        assert _write_backend_file(root, p1, b"B" * 16) == 16
        m1 = f"{p1}/{_sha('mem1')}"
        conn.execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?,?,1)",
                     (m1, 5))
        conn.execute(
            "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
            "VALUES (?,?,?,?,?)", (m1, backend_name, f"{p1}@8:5", 5, _sha(p1)))
        fid = conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
            "VALUES ('bundles', 'one.dat', ?, 40, 'vg-m1')", (_sha("one"),)
        ).lastrowid
        conn.execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
                     "VALUES (?, 0, ?, 0, 5)", (fid, m1))
        ids["member_ok"] = fid

        # Bundle member whose parent bundle has a storage row but NO file on disk.
        tar2 = _sha("tar2")
        p2 = f"bundle/{tar2[:2]}/{tar2[2:4]}/{tar2}"
        conn.execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?,?,1)",
                     (p2, 20))
        conn.execute(
            "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
            "VALUES (?,?,?,?,?)", (p2, backend_name, p2, 20, _sha(p2)))
        m2 = f"{p2}/{_sha('mem2')}"
        conn.execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?,?,1)",
                     (m2, 6))
        conn.execute(
            "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
            "VALUES (?,?,?,?,?)", (m2, backend_name, f"{p2}@4:6", 6, _sha(p2)))
        fid = conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
            "VALUES ('bundles', 'two.dat', ?, 60, 'vg-m2')", (_sha("two"),)
        ).lastrowid
        conn.execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
                     "VALUES (?, 0, ?, 0, 6)", (fid, m2))
        ids["member_lost"] = fid
        ids["member_lost_blob"] = m2
        ids["lost_parent_path"] = p2
        ids["ok_parent_path"] = p1
    return ids


# ---------------------------------------------------------------------------
# Page + nav
# ---------------------------------------------------------------------------

def test_health_page_renders(client, fixture_ids):
    r = client.get("/health")
    assert r.status_code == 200
    assert "Data Health" in r.text
    assert "Purge Candidates" in r.text
    assert "Physical Reachability Scan" in r.text


def test_nav_has_data_health(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/health"' in r.text
    assert "Data Health" in r.text


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_summary_buckets(client, fixture_ids):
    r = client.get("/api/health/data")
    assert r.status_code == 200
    data = r.json()
    assert set(data.keys()) == {"summary", "scan", "scan_running", "scan_age_seconds"}
    s = data["summary"]
    assert s["live_files"] == 9
    assert s["no_chunks_files"] == 3
    assert s["missing_blob_files"] == 1
    assert s["missing_blobs"] == 1
    assert s["backend_blobs"] == [{"backend": "test_local", "blobs": 7,
                                   "stored_bytes": 64 + 32 + 32 + 16 + 20 + 5 + 6}]
    assert s["same_name_groups"] == 1
    assert s["same_name_files"] == 2
    assert s["purge_candidates"] == 4
    # No scan ran yet in this app instance.
    assert data["scan_running"] is False
    assert data["scan"]["completed"] is False
    assert data["scan"]["results"] == {}
    assert data["scan_age_seconds"] is None


def test_summary_direct_module_call(env, backends, fixture_ids):
    from backupme.config import load_config
    s = dh.summary(load_config())
    assert s["live_files"] == 9
    assert s["no_chunks_files"] == 3
    assert s["purge_candidates"] == 4


# ---------------------------------------------------------------------------
# Issues drill-down + pagination
# ---------------------------------------------------------------------------

def test_issues_pagination(client, fixture_ids):
    r = client.get("/api/health/issues",
                   params={"reason": "no_chunks", "limit": 2, "offset": 0})
    assert r.status_code == 200
    d1 = r.json()
    assert d1["total"] == 3
    assert d1["limit"] == 2
    assert d1["offset"] == 0
    assert len(d1["items"]) == 2
    assert all(i["why"] == "no chunk rows stored for this file" for i in d1["items"])
    page1_ids = [i["file_id"] for i in d1["items"]]

    r2 = client.get("/api/health/issues",
                    params={"reason": "no_chunks", "limit": 2, "offset": 2})
    d2 = r2.json()
    assert len(d2["items"]) == 1
    assert d2["items"][0]["file_id"] not in page1_ids
    # Stable ordering by file id: page 2 holds the last id.
    assert d2["items"][0]["file_id"] == max(fixture_ids["no_chunks"])

    item = d1["items"][0]
    assert set(item.keys()) == {"file_id", "source_root", "relative_path", "size", "why"}


def test_issues_missing_blob_bucket(client, fixture_ids):
    r = client.get("/api/health/issues", params={"reason": "missing_blob"})
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 1
    assert d["items"][0]["file_id"] == fixture_ids["orphan"]
    assert fixture_ids["orphan_blob"] in d["items"][0]["why"]


def test_issues_purge_reason_and_unknown_reason(client, fixture_ids):
    r = client.get("/api/health/issues", params={"reason": "purge_candidates"})
    assert r.status_code == 200
    assert r.json()["total"] == 4

    r2 = client.get("/api/health/issues", params={"reason": "bogus"})
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Same-name groups + purge candidates endpoints
# ---------------------------------------------------------------------------

def test_same_name_endpoint(client, fixture_ids):
    r = client.get("/api/health/same-name")
    assert r.status_code == 200
    d = r.json()
    assert d["total_groups"] == 1
    assert d["total_files"] == 2
    g = d["groups"][0]
    assert g["base_name"] == "report.pdf"
    assert g["file_count"] == 2
    assert {m["file_id"] for m in g["members"]} == set(fixture_ids["same"])
    for m in g["members"]:
        assert set(m.keys()) == {"file_id", "source_root", "relative_path", "size"}


def test_purge_candidates_endpoint(client, fixture_ids):
    r = client.get("/api/health/purge-candidates")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 4
    got = {i["file_id"] for i in d["items"]}
    assert got == set(fixture_ids["no_chunks"]) | {fixture_ids["orphan"]}
    assert fixture_ids["healthy"] not in got
    assert fixture_ids["member_ok"] not in got
    whys = {i["file_id"]: i["why"] for i in d["items"]}
    assert whys[fixture_ids["no_chunks"][0]] == "no chunk rows stored for this file"
    assert whys[fixture_ids["orphan"]] == "every chunk blob has no storage rows"


# ---------------------------------------------------------------------------
# Purge endpoint
# ---------------------------------------------------------------------------

def test_purge_requires_exact_confirm(client, fixture_ids):
    r = client.post("/api/health/purge", json={"file_ids": [fixture_ids["healthy"]]})
    assert r.status_code == 400
    r2 = client.post("/api/health/purge",
                     json={"confirm": "yes", "file_ids": [fixture_ids["healthy"]]})
    assert r2.status_code == 400
    r3 = client.post("/api/health/purge", json={"confirm": False,
                                                "file_ids": [fixture_ids["healthy"]]})
    assert r3.status_code == 400
    r4 = client.post("/api/health/purge", json={"confirm": True, "file_ids": []})
    assert r4.status_code == 400


def test_purge_rejects_healthy_file(client, fixture_ids):
    r = client.post("/api/health/purge",
                    json={"confirm": True, "file_ids": [fixture_ids["healthy"]]})
    assert r.status_code == 400
    assert r.json()["rejected_ids"] == [fixture_ids["healthy"]]
    # Nothing was deleted anywhere.
    live = execute("SELECT COUNT(*) FROM files WHERE deleted_at IS NULL").fetchone()[0]
    assert live == 9
    deleted = execute("SELECT COUNT(*) FROM files WHERE deleted_at IS NOT NULL").fetchone()[0]
    assert deleted == 0


def test_purge_rejects_mixed_request(client, fixture_ids):
    """One non-candidate id rejects the WHOLE request — nothing is deleted."""
    targets = [fixture_ids["no_chunks"][0], fixture_ids["healthy"]]
    r = client.post("/api/health/purge", json={"confirm": True, "file_ids": targets})
    assert r.status_code == 400
    assert r.json()["rejected_ids"] == [fixture_ids["healthy"]]
    live = execute("SELECT COUNT(*) FROM files WHERE deleted_at IS NULL").fetchone()[0]
    assert live == 9


def test_purge_success_soft_deletes_exactly_broken(client, fixture_ids):
    targets = [fixture_ids["no_chunks"][0], fixture_ids["orphan"]]
    r = client.post("/api/health/purge", json={"confirm": True, "file_ids": targets})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    ok_ids = {x["file_id"] for x in body["results"] if x["status"] == "ok"}
    assert ok_ids == set(targets)

    deleted = {row["id"] for row in
               execute("SELECT id FROM files WHERE deleted_at IS NOT NULL").fetchall()}
    assert deleted == set(targets)
    assert execute("SELECT COUNT(*) FROM files WHERE deleted_at IS NULL").fetchone()[0] == 7
    # The orphan's blob ref was dropped exactly like the DELETE route does.
    ref = execute("SELECT ref_count FROM blobs WHERE blob_id = ?",
                  (fixture_ids["orphan_blob"],)).fetchone()
    assert ref["ref_count"] == 0
    # Healthy file untouched (still live, its blob ref intact).
    healthy = execute("SELECT deleted_at FROM files WHERE id = ?",
                      (fixture_ids["healthy"],)).fetchone()
    assert healthy["deleted_at"] is None
    ref_healthy = execute(
        "SELECT ref_count FROM blobs WHERE blob_id = "
        "(SELECT blob_id FROM file_chunks WHERE file_id = ?)",
        (fixture_ids["healthy"],)).fetchone()
    assert ref_healthy["ref_count"] == 1


def test_purge_result_is_no_longer_a_candidate(client, fixture_ids):
    targets = [fixture_ids["no_chunks"][0], fixture_ids["orphan"]]
    assert client.post("/api/health/purge",
                       json={"confirm": True, "file_ids": targets}).status_code == 200
    d = client.get("/api/health/purge-candidates").json()
    got = {i["file_id"] for i in d["items"]}
    assert targets[0] not in got and targets[1] not in got
    assert d["total"] == 2  # the two other no-chunk files
    # Purging again would now be rejected (no longer candidates).
    r = client.post("/api/health/purge", json={"confirm": True, "file_ids": targets})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Physical reachability scan
# ---------------------------------------------------------------------------

def _wait_scan(client) -> dict:
    for _ in range(200):
        data = client.get("/api/health/data").json()
        if data["scan"]["completed"] and not data["scan"]["running"]:
            return data
        time.sleep(0.05)
    raise AssertionError("health scan never finished")


def test_scan_reports_present_and_missing(client, fixture_ids):
    r = client.post("/api/health/scan")
    assert r.status_code == 200
    assert r.json()["status"] == "started"
    data = _wait_scan(client)

    res = data["scan"]["results"][fixture_ids["backend"]]
    assert res["objects_checked"] == 3
    assert res["objects_ok"] == 3
    assert res["objects_missing"] == []
    assert res["objects_size_mismatch"] == []
    assert res["bundles_checked"] == 2
    assert res["bundles_ok"] == 1
    assert res["bundles_missing"] == [fixture_ids["lost_parent_path"]]
    assert res["members_checked"] == 2
    assert res["members_ok"] == 1
    assert len(res["members_unreachable"]) == 1
    mu = res["members_unreachable"][0]
    assert mu["blob_id"] == fixture_ids["member_lost_blob"]
    assert mu["parent"] == fixture_ids["lost_parent_path"]
    assert mu["reason"] == "parent bundle missing"
    assert data["scan_age_seconds"] is not None and data["scan_age_seconds"] >= 0
    assert data["scan"]["remote"] == []


def test_scan_single_flight_409(client, fixture_ids, monkeypatch):
    gate = threading.Event()
    entered = threading.Event()
    real_plan = dh.local_scan_plan

    def slow_plan(names):
        entered.set()
        gate.wait(timeout=20)
        return real_plan(names)

    monkeypatch.setattr(dh, "local_scan_plan", slow_plan)
    r = client.post("/api/health/scan")
    assert r.status_code == 200
    assert entered.wait(timeout=10), "scan worker never started"

    r2 = client.post("/api/health/scan")
    assert r2.status_code == 409

    gate.set()
    _wait_scan(client)


def test_scan_notes_remote_backends(client, env, fixture_ids):
    lines = (Path(env) / "config.yaml").read_text().splitlines(keepends=True)
    entry = ["  sftp:\n", "  - name: remote_ghost\n", "    host: 127.0.0.1\n",
             "    port: 1\n", "    username: u\n", "    password: p\n"]
    idx = next(i for i, l in enumerate(lines) if l.startswith("encryption:"))
    lines[idx:idx] = entry
    (Path(env) / "config.yaml").write_text("".join(lines))
    invalidate_cache()

    r = client.post("/api/health/scan")
    assert r.status_code == 200
    data = _wait_scan(client)
    remote = {x["name"]: x for x in data["scan"]["remote"]}
    assert "remote_ghost" in remote
    assert remote["remote_ghost"]["type"] == "sftp"
    assert remote["remote_ghost"]["note"] == "not scanned (remote)"
    assert fixture_ids["backend"] in data["scan"]["results"]


def test_scan_stream_sse_terminates(client, fixture_ids):
    assert client.post("/api/health/scan").status_code == 200
    with client.stream("GET", "/api/health/scan/stream") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = ""
        for chunk in resp.iter_text():
            raw += chunk
    events = [e for e in raw.split("\n\n") if e.strip()]
    assert events
    assert all(e.startswith("data: ") for e in events)
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is True
    assert last["running"] is False


# ---------------------------------------------------------------------------
# Unit-level helpers
# ---------------------------------------------------------------------------

def test_parse_member_path():
    assert dh.parse_member_path("bundle/aa/bb/x@8:5") == ("bundle/aa/bb/x", 8, 5)
    assert dh.parse_member_path("objects/aa/bb/sha") is None
    assert dh.parse_member_path("bundle/aa@bad") is None
    assert dh.parse_member_path("bundle/aa@8:") is None
    assert dh.parse_member_path("bundle/aa@-1:5") is None
    assert dh.parse_member_path("") is None

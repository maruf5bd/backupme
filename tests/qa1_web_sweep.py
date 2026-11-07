"""QA1 black-box web dashboard sweep (agent 1 of 3, WEB + HTTP APIs).

Functional sweep over every surface of backupme.web.routes.* in ISOLATED
test envs (the conftest `env`/`backends` fixture pattern + FastAPI
TestClient). No test here ever touches the live database: each test gets a
fresh scratch config + DB + local backend, and the confirm:true exercises
only run against these throwaway apps.

Sections:
  1. HTML pages render
  2. Auth gate (web.token bearer matrix, destructive 401s)
  3. Confirm gates on destructive endpoints (400 without confirm:true)
  4. Search (plain / content / hash / punctuation / empty)
  5. /api/files pagination + backend filter
  6. Upload flow (dest_folder validation, backend selection, autocomplete,
     op_log row)
  7. Restore flows (all / files / 409 / stream / download / to-path)
  8. Browse + tree (forward and backslash paths)
  9. Health surface (data/issues/same-name/purge-candidates/scan+stream)
 10. Backends (list/usage/test/detail/add/remove/streams)
 11. dbops (info/snapshots/recovery/check/snapshot/checkpoint/replicate)
 12. Ingest (status/generations/history/trend/pause/scan)
 13. Scheduler/daemon status + scheduler config
 14. Logs (list/export csv+json), report/encrypt, misc stats
"""
import concurrent.futures
import csv
import gc
import hashlib
import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import write_txn
from tests.helpers import backup_bytes

TOKEN = "qa1-secret-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain_sse(client: TestClient, path: str) -> list[str]:
    """Read an SSE stream to its clean end; return the `data:` frames."""
    with client.stream("GET", path) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream"), \
            f"{path} content-type: {resp.headers['content-type']}"
        raw = "".join(resp.iter_text())
    return [e for e in raw.split("\n\n") if e.strip()]


def _drain_sse_bounded(client: TestClient, path: str, timeout: float = 20.0) -> list[str]:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_drain_sse, client, path)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            pytest.fail(f"SSE {path} never terminated within {timeout:.0f}s")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _assert_sse_terminated(events: list[str]) -> dict:
    assert events, "stream closed without emitting a single SSE event"
    for event in events:
        assert event.startswith("data: "), f"malformed SSE frame: {event[:80]!r}"
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is True, f"stream did not end completed: {last}"
    assert last["running"] is False, f"stream did not end idle: {last}"
    return last


def _wait_for(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _db_scalar(sql: str, params: tuple):
    from backupme.core.database import execute
    return execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _release_stray_db_locks():
    """TestClient portal threads and background workers leave thread-local
    sqlite connections behind; on Windows an unclosed handle keeps the DB
    file locked and the NEXT test's db.initialize() can hit
    'database is locked'. This finalizer runs last (autouse fixtures set up
    first), so gc.collect() finalizes every abandoned connection object."""
    yield
    gc.collect()


@pytest.fixture()
def app(env, backends):
    from backupme.config import load_config
    return create_app(load_config())


@pytest.fixture()
def client(app):
    # Black-box HTTP contract suite: a regression must show up as a 5xx
    # status, not a traceback escaping into the runner.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def token_client(env, backends):
    """App with web.token armed (isolated env only)."""
    cfg_path = Path(env) / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + f"web:\n  token: {TOKEN}\n")
    from backupme.config import load_config
    return TestClient(create_app(load_config()), raise_server_exceptions=False)


@pytest.fixture()
def seeded(env, backends):
    """Small dataset: 1 real pipeline upload + 6 direct index rows + 3
    op_log rows + 2 integrity_checks rows + a purge candidate (live row
    with no chunks)."""
    stamp = "2021-05-0{} 09:00:00"
    direct_ids = []
    rows = [
        ("QA1", "docs/alpha.txt"),
        ("QA1", "docs/beta.txt"),
        ("QA1", "media/pic.jpg"),
        ("QA2", "notes.txt"),
        ("QA3", "notes.txt"),
        ("QA1", "top.txt"),
    ]
    with write_txn() as conn:
        for i, (source_root, relative_path) in enumerate(rows, start=1):
            created = stamp.format(i)
            cur = conn.execute(
                "INSERT INTO files (source_root, relative_path, sha256, size, "
                "version_group, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (source_root, relative_path, _sha(f"{source_root}/{relative_path}"),
                 7 * i, f"vg-qa1-{i}", created, created),
            )
            direct_ids.append(cur.lastrowid)
        for i in range(1, 4):
            status = "completed" if i % 2 else "failed"
            started = f"2021-06-0{i} 08:00:00"
            conn.execute(
                "INSERT INTO op_log (idempotency_key, op_type, target_json, status, "
                "started_at, completed_at, last_error) VALUES (?,?,?,?,?,?,?)",
                (f"qa1-op-{i}", "qa1_probe", json.dumps({"probe": i}), status,
                 started, started if status == "completed" else None,
                 None if status == "completed" else "intentional qa1 probe failure"),
            )
        rep_ids = []
        for i in (1, 2):
            started = f"2021-07-01 10:00:0{i}"
            cur = conn.execute(
                "INSERT INTO integrity_checks (check_type, status, total_files, "
                "checked_files, failed_files, started_at, completed_at) "
                "VALUES ('qa1_probe','completed',5,5,0,?,?)", (started, started),
            )
            rep_ids.append(cur.lastrowid)
        # purge candidate: live row, no file_chunks rows at all
        cur = conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, "
            "version_group) VALUES ('QA1','ghost.bin',?,5,'vg-qa1-ghost')",
            (_sha("ghost"),),
        )
        ghost_id = cur.lastrowid
        # a row that carries legacy inline content (post-migration shape:
        # content_text is only ever NULL from the live pipeline, so the
        # content-search surface is exercised with a hand-seeded row)
        cur = conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, "
            "version_group, content_text) "
            "VALUES ('QA1','legacy/inline.txt',?,5,'vg-qa1-inline',?)",
            (_sha("inline"), "the qa1 needle inside content"),
        )
        content_id = cur.lastrowid

    result = backup_bytes(Path(env), "seed/delta.txt", b"delta content 123", backends)
    assert "file_id" in result, f"pipeline upload failed, no file_id: {result}"
    file_id = result["file_id"]
    return {
        "env": env,
        "file_id": file_id,
        "sha256": _db_scalar("SELECT sha256 FROM files WHERE id = ?", (file_id,)),
        "direct_ids": direct_ids,
        "ghost_id": ghost_id,
        "content_id": content_id,
        "report_id": rep_ids[-1],
        "source_root": str(env),
    }


@pytest.fixture()
def fs_tree(client, seeded):
    """Forward-slash (cPanel-style) + backslash (Windows-scan-style) rows."""
    ids = []
    with write_txn() as conn:
        specs = [
            ("T1", "sub/nested/epsilon.log", 30),
            ("T1", "sub/alpha.txt", 11),
            ("T1", "sub/beta.txt", 12),
            ("T1", "rootfile.txt", 5),
            ("T2", "bs\\deep\\f.txt", 7),
            ("T2", "bs\\top.txt", 3),
        ]
        for source_root, rel, size in specs:
            cur = conn.execute(
                "INSERT INTO files (source_root, relative_path, sha256, size, "
                "version_group) VALUES (?,?,?,?,?)",
                (source_root, rel, _sha(f"{source_root}/{rel}"), size, f"vg-{rel}"),
            )
            ids.append(cur.lastrowid)
    return {"root_fs": "T1", "root_bs": "T2", "ids": ids}


def _add_restore_rows(n: int, prefix: str = "bulk") -> list[int]:
    """Insert n chunk-less rows (fast-failing downloads) for 409 testing."""
    ids = []
    with write_txn() as conn:
        for i in range(n):
            cur = conn.execute(
                "INSERT INTO files (source_root, relative_path, sha256, size, "
                "version_group) VALUES (?,?,?,?,?)",
                ("BULK", f"{prefix}/{i}.txt", _sha(f"{prefix}-{i}"), 1, f"vg-{prefix}-{i}"),
            )
            ids.append(cur.lastrowid)
    return ids


# ---------------------------------------------------------------------------
# 1. HTML pages
# ---------------------------------------------------------------------------

PAGES = ["/", "/files", "/backends", "/ingest", "/logs", "/database",
         "/report", "/settings", "/health"]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_html_200(client, seeded, page):
    r = client.get(page)
    assert r.status_code == 200, f"GET {page} -> {r.status_code}: {r.text[:200]!r}"
    assert r.headers["content-type"].startswith("text/html"), page
    assert "<html" in r.text.lower(), f"{page} has no html marker"


def test_pages_surface_real_data(client, seeded):
    dash = client.get("/").text
    assert "delta.txt" in dash, "dashboard recent-files must include the seeded upload"
    files_page = client.get("/files", params={"q": "delta"}).text
    assert "delta.txt" in files_page, "files page search must surface the seeded file"


def test_files_page_search_modes_render(client, seeded):
    sha = seeded["sha256"]
    checks = [
        {"q": "delta"},                       # plain FTS path
        {"q": "delta content", "mode": "content"},
        {"q": sha, "mode": "hash"},           # exact hash
        {"q": sha[:12], "mode": "hash"},      # hash prefix fallback
        {"q": "delta", "backend": "test_local"},
        {"q": "", "backend": "test_local"},
        {"source": str(seeded["env"])},
        {"offset": 0, "limit": 2},
    ]
    for params in checks:
        r = client.get("/files", params=params)
        assert r.status_code == 200, f"/files {params} -> {r.status_code}: {r.text[:200]!r}"


def test_hash_mode_page_finds_file(client, seeded):
    r = client.get("/files", params={"q": seeded["sha256"], "mode": "hash"})
    assert r.status_code == 200
    assert "delta.txt" in r.text, "hash-mode page must show the matching file"


def test_logs_page_filters(client, seeded):
    r = client.get("/logs", params={"op": "qa1_probe", "status": "completed"})
    assert r.status_code == 200
    assert "qa1_probe" in r.text
    r2 = client.get("/logs", params={"limit": 1})
    assert r2.status_code == 200


def test_database_page_lists_snapshot(client, seeded):
    assert client.post("/api/db/snapshot").status_code == 200
    r = client.get("/database")
    assert r.status_code == 200
    snaps = [p.name for p in Path(".").glob("snapshot-*.db.zst")]
    assert snaps, "snapshot file should exist in the isolated cwd"
    assert snaps[0] in r.text, "database page must list the created snapshot"


def test_settings_page_lists_config_sections(client, seeded):
    r = client.get("/settings")
    assert r.status_code == 200
    for section in ("chunking", "compression", "verification", "retention",
                    "database", "scheduler", "cpanel_ingest"):
        assert section in r.text, f"settings page missing section {section}"


# ---------------------------------------------------------------------------
# 2. Auth gate
# ---------------------------------------------------------------------------

def test_auth_401_without_token(client, token_client):
    # control: the same env without a token serves the API
    assert client.get("/api/status").status_code == 200
    r = token_client.get("/api/status")
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "unauthorized"}


def test_auth_401_wrong_bearer(token_client):
    r = token_client.get("/api/status", headers={"Authorization": f"Bearer wrong-{TOKEN}"})
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


def test_auth_200_with_correct_bearer(token_client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    for path in ("/api/status", "/api/files", "/api/backends", "/api/backends/usage",
                 "/api/db/info", "/api/ingest/status", "/api/health/data",
                 "/api/logs", "/api/report", "/api/scheduler/config",
                 "/api/daemon/status", "/api/encrypt/status"):
        r = token_client.get(path, headers=headers)
        assert r.status_code == 200, f"valid bearer rejected on {path}: {r.text[:120]!r}"


def test_auth_lowercase_scheme_rejected(token_client):
    r = token_client.get("/api/status", headers={"Authorization": f"bearer {TOKEN}"})
    assert r.status_code == 401, "middleware requires the exact 'Bearer ' scheme"


def test_auth_gates_mutating_methods_too(token_client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    # tokenless mutating request -> 401 from the middleware, not a handler run
    r = token_client.post("/api/upload/file",
                          files={"file": ("x.txt", b"x")},
                          data={"dest_folder": ""})
    assert r.status_code == 401, r.text[:120]
    r2 = token_client.delete("/api/files/1")
    assert r2.status_code == 401
    # correct bearer passes through to the handler (404 here, not 401)
    r3 = token_client.delete("/api/files/999999", headers=headers)
    assert r3.status_code == 404, r3.text[:120]


def test_auth_pages_remain_public_by_design(token_client):
    # Documented behavior: only /api/* is gated; pages rely on loopback binding.
    assert token_client.get("/").status_code == 200
    assert token_client.get("/health").status_code == 200
    assert token_client.get("/files").status_code == 200


def test_destructive_endpoints_401_unauthenticated(token_client):
    """Unauthenticated destructive calls never reach the handler (401 first),
    even with confirm:true in the body — the middleware runs first."""
    checks = [
        ("delete", "/api/backends/remove/test_local", {"confirm": True}),
        ("post", "/api/db/restore", {"confirm": True}),
        ("post", "/api/health/purge", {"confirm": True, "file_ids": [1]}),
        ("delete", "/api/db/snapshots/snapshot-x.db.zst", {"confirm": True}),
        ("post", "/api/server/restart", {"confirm": True}),
    ]
    for method, path, body in checks:
        r = token_client.request(method, path, json=body)
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}: {r.text[:120]!r}"
        assert r.json() == {"detail": "unauthorized"}


# ---------------------------------------------------------------------------
# 3. Confirm gates (400 without confirm:true) — isolated apps only
# ---------------------------------------------------------------------------

CONFIRM_GATED = [
    ("delete", "/api/backends/remove/test_local", None),
    ("delete", "/api/backends/remove/test_local", {"confirm": False}),
    ("delete", "/api/backends/remove/test_local", {"confirm": "true"}),  # string != true
    ("post", "/api/db/restore", None),
    ("post", "/api/db/restore", {"confirm": False}),
    ("post", "/api/db/rebuild", None),
    ("post", "/api/db/rebuild", {"confirm": "true"}),
    ("delete", "/api/db/snapshots/snapshot-2099-01-01.db.zst", None),
    ("delete", "/api/db/snapshots/snapshot-2099-01-01.db.zst", {"confirm": False}),
    ("post", "/api/db/snapshots/prune", None),
    ("post", "/api/db/snapshots/prune", {"keep": 5}),
    ("post", "/api/server/restart", None),
    ("post", "/api/server/restart", {"confirm": False}),
    ("post", "/api/health/purge", None),
    ("post", "/api/health/purge", {"file_ids": [1]}),
]


def test_purge_missing_file_ids_400(client, seeded):
    # confirm:true but no file_ids -> input validation, not the confirm gate
    r = client.post("/api/health/purge", json={"confirm": True})
    assert r.status_code == 400
    assert "file_ids" in r.json()["error"]


@pytest.mark.parametrize("method,path,body", CONFIRM_GATED)
def test_confirm_gate_rejects_without_exact_true(client, seeded, method, path, body):
    # NOTE: confirm:true is NEVER sent for the destructive happy paths here
    # except where explicitly safe (purge candidate / snapshot delete tests
    # below); server/restart is NEVER confirmed — it would execv the process.
    r = client.request(method, path, json=body)
    assert r.status_code == 400, f"{method} {path} {body} -> {r.status_code}: {r.text[:160]!r}"
    assert r.json() == {"error": "confirm must be exactly true"}


def test_purge_with_confirm_executes_and_logs(client, seeded):
    r = client.post("/api/health/purge",
                    json={"confirm": True, "file_ids": [seeded["ghost_id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["results"][0]["status"] == "ok"
    deleted_at = _db_scalar("SELECT deleted_at FROM files WHERE id = ?",
                            (seeded["ghost_id"],))
    assert deleted_at, "purged row must be soft-deleted"
    # op_log row for the purge (B12 audit)
    r2 = client.get("/api/logs", params={"op": "purge", "limit": 5})
    ops = r2.json()
    assert any(o["status"] == "completed" for o in ops), ops


def test_purge_rejects_non_candidates(client, seeded):
    # the real uploaded file HAS chunks -> not a purge candidate
    r = client.post("/api/health/purge",
                    json={"confirm": True, "file_ids": [seeded["file_id"]]})
    assert r.status_code == 400, r.text
    assert "rejected_ids" in r.json()


def test_snapshot_delete_and_prune_with_confirm(client, seeded):
    made = client.post("/api/db/snapshot")
    assert made.status_code == 200, made.text
    snaps = client.get("/api/db/snapshots").json()
    assert len(snaps) == 1
    name = snaps[0]["name"]
    # nonexistent name with confirm -> clean "not found" message, never 500
    r404 = client.request("DELETE", "/api/db/snapshots/snapshot-nope.db.zst",
                          json={"confirm": True})
    assert r404.status_code == 200
    assert r404.json()["status"] == "error"
    # real delete
    rdel = client.request("DELETE", f"/api/db/snapshots/{name}", json={"confirm": True})
    assert rdel.status_code == 200 and rdel.json()["status"] == "ok"
    assert client.get("/api/db/snapshots").json() == []
    # prune with confirm on an empty set
    rprune = client.post("/api/db/snapshots/prune", params={"keep": 5},
                         json={"confirm": True})
    assert rprune.status_code == 200
    assert rprune.json()["deleted"] == 0


def test_backend_remove_with_confirm_updates_config(client, seeded):
    add = client.post("/api/backends/add", json={
        "type": "local", "name": "qa1_extra",
        "path": str(Path(seeded["env"]).parent / "qa1_extra_storage")})
    assert add.status_code == 200 and add.json()["status"] == "ok"
    cfg = (Path(seeded["env"]) / "config.yaml").read_text()
    assert "qa1_extra" in cfg
    r = client.request("DELETE", "/api/backends/remove/qa1_extra", json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    cfg2 = (Path(seeded["env"]) / "config.yaml").read_text()
    assert "qa1_extra" not in cfg2


# ---------------------------------------------------------------------------
# 4. Search
# ---------------------------------------------------------------------------

def test_api_search_basic_and_empty(client, seeded):
    r = client.get("/api/search", params={"q": "delta"})
    assert r.status_code == 200
    assert seeded["file_id"] in [f["id"] for f in r.json()]
    r2 = client.get("/api/search", params={"q": ""})
    assert r2.status_code == 200 and r2.json() == []


def test_api_search_content_scope(client, seeded):
    r = client.get("/api/search", params={"q": "qa1 needle", "scope": "content"})
    assert r.status_code == 200, r.text
    assert seeded["content_id"] in [f["id"] for f in r.json()], r.json()
    # D17 fixed: the real pipeline now populates content_text for small
    # unencrypted strict-UTF-8 uploads, so content search matches fresh
    # uploads too (previously only hand-seeded legacy rows were findable).
    r2 = client.get("/api/search", params={"q": "delta content 123", "scope": "content"})
    assert r2.status_code == 200, r2.text
    assert seeded["file_id"] in [f["id"] for f in r2.json()], r2.json()


def test_api_files_hash_and_punctuation_never_500(client, seeded):
    sha = seeded["sha256"]
    for q in ('a"b', "((", "delta.txt", "; DROP TABLE files; --", "x*y?z", "***"):
        r = client.get("/api/files", params={"q": q})
        assert r.status_code == 200, f"q={q!r} -> {r.status_code}"
        r2 = client.get("/api/search", params={"q": q})
        assert r2.status_code == 200, f"/api/search q={q!r} -> {r2.status_code}"


def test_api_files_finds_by_exact_sha256(client, seeded):
    # D15 fixed: /api/files now has a hash mode (pure-hex q of >= 8 chars
    # matches sha256 exact first, then prefix) — pin unconverted from xfail.
    ok = client.get("/api/files", params={"q": seeded["sha256"]})
    assert ok.status_code == 200
    assert seeded["file_id"] in [f["id"] for f in ok.json()], ok.json()


def test_api_files_search_miss_is_empty_list(client, seeded):
    r = client.get("/api/files", params={"q": "zzz_no_such_file_qq"})
    assert r.status_code == 200 and r.json() == []


def test_api_files_no_q_returns_list(client, seeded):
    r = client.get("/api/files")
    assert r.status_code == 200
    ids = [f["id"] for f in r.json()]
    assert seeded["file_id"] in ids and seeded["direct_ids"][0] in ids


# ---------------------------------------------------------------------------
# 5. /api/files pagination + backend filter
# ---------------------------------------------------------------------------

def test_api_files_pagination(client, seeded):
    r1 = client.get("/api/files", params={"limit": 2, "offset": 0})
    r2 = client.get("/api/files", params={"limit": 2, "offset": 2})
    assert r1.status_code == 200 and r2.status_code == 200
    a = [f["id"] for f in r1.json()]
    b = [f["id"] for f in r2.json()]
    assert len(a) == 2
    assert not set(a) & set(b), "offset windows must not overlap"
    r3 = client.get("/api/files", params={"limit": 3, "offset": 0})
    assert [f["id"] for f in r3.json()][:2] == a, "stable ordering across pages"


def test_api_files_backend_filter(client, seeded):
    r = client.get("/api/files", params={"backend": "test_local"})
    assert r.status_code == 200
    ids = [f["id"] for f in r.json()]
    # direct rows have no chunks -> backend filter must exclude them
    assert seeded["file_id"] in ids
    assert seeded["direct_ids"][0] not in ids
    r2 = client.get("/api/files", params={"backend": "no_such_backend"})
    assert r2.status_code == 200 and r2.json() == []


def test_api_files_backend_filter_with_query(client, seeded):
    r = client.get("/api/files", params={"q": "delta", "backend": "test_local"})
    assert r.status_code == 200
    assert seeded["file_id"] in [f["id"] for f in r.json()]
    r2 = client.get("/api/files", params={"q": "delta", "backend": "no_such_backend"})
    assert r2.status_code == 200 and r2.json() == []


# ---------------------------------------------------------------------------
# 6. Upload flow
# ---------------------------------------------------------------------------

def test_upload_file_roundtrip_and_op_log(client, seeded):
    r = client.post("/api/upload/file",
                    files={"file": ("qa1up.txt", b"hello from qa1", "text/plain")},
                    data={"backend": "test_local", "dest_folder": ""})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["dest"] == "qa1up.txt"
    # searchable right after
    r2 = client.get("/api/files", params={"q": "qa1up.txt"})
    assert any(f["relative_path"] == "qa1up.txt" for f in r2.json()), r2.json()
    # op_log row: upload completed
    r3 = client.get("/api/logs", params={"op": "upload", "limit": 5})
    ups = [o for o in r3.json() if o["op_type"] == "upload"]
    assert ups and ups[0]["status"] == "completed", ups


def test_upload_dest_folder_nested(client, seeded):
    r = client.post("/api/upload/file",
                    files={"file": ("nested.txt", b"nested body", "text/plain")},
                    data={"dest_folder": "reports/2024"})
    assert r.status_code == 200, r.text
    assert r.json()["dest"] == "reports/2024/nested.txt"
    r2 = client.get("/api/upload/folders")
    folders = r2.json()["folders"]
    assert "reports" in folders and "reports/2024" in folders, folders
    # autocomplete by prefix and "under folder" mode
    r3 = client.get("/api/upload/folders", params={"q": "rep"})
    assert "reports" in r3.json()["folders"]
    r4 = client.get("/api/upload/folders", params={"q": "reports/"})
    assert "reports/2024" in r4.json()["folders"]


@pytest.mark.parametrize("bad", ["../evil", "a/../../b", "C:\\evil", "C:/evil",
                                 ".", "..", "a/./b"])
def test_upload_dest_folder_traversal_rejected(client, seeded, bad):
    r = client.post("/api/upload/file",
                    files={"file": ("x.txt", b"x", "text/plain")},
                    data={"dest_folder": bad})
    assert r.status_code == 400, f"dest_folder={bad!r} -> {r.status_code}: {r.text[:160]!r}"
    assert r.json()["status"] == "error"


@pytest.mark.parametrize("weird", ["/abs", "\\\\server\\share"])
def test_upload_dest_folder_weird_but_safe_is_sanitized(client, seeded, weird):
    """Leading slashes / UNC-style input is stripped to a safe relative
    folder instead of rejected — acceptable normalization as long as the
    result stays inside web_upload."""
    r = client.post("/api/upload/file",
                    files={"file": ("x.txt", b"x", "text/plain")},
                    data={"dest_folder": weird})
    assert r.status_code == 200, f"dest_folder={weird!r} -> {r.status_code}: {r.text[:160]!r}"
    dest = r.json()["dest"]
    assert ".." not in dest.split("/") and ":" not in dest, dest
    assert dest.endswith("x.txt"), dest


@pytest.mark.xfail(reason="qa defect D12: uploading to an unknown backend "
                          "name answers HTTP 200 with status=error ('all "
                          "backends failed') instead of 400 — "
                          "indistinguishable from success at the HTTP layer; "
                          "the response also leaks the server temp local_path",
                   strict=False)
def test_upload_unknown_backend_name_is_400(client, seeded):
    r = client.post("/api/upload/file",
                    files={"file": ("x.txt", b"x", "text/plain")},
                    data={"backend": "no_such_backend"})
    assert r.status_code == 400, r.text
    assert "local_path" not in r.json()


@pytest.mark.xfail(reason="qa defect D13: the upload route always writes "
                          "op_log status=completed/result=success even when "
                          "the uploader returned status=error (no exception) — "
                          "the audit log shows failed uploads as successes",
                   strict=False)
def test_upload_failure_is_logged_as_failed(client, seeded):
    r = client.post("/api/upload/file",
                    files={"file": ("x.txt", b"x", "text/plain")},
                    data={"backend": "no_such_backend"})
    assert r.status_code == 200  # observed contract (see D12)
    assert r.json()["status"] == "error"
    logs = client.get("/api/logs", params={"op": "upload", "limit": 5}).json()
    assert logs and logs[0]["status"] == "failed", logs


def test_upload_filename_path_stripped(client, seeded):
    r = client.post("/api/upload/file",
                    files={"file": ("sub/dir/win.txt", b"win path", "text/plain")})
    assert r.status_code == 200, r.text
    assert "\\" not in r.json()["dest"] and "/" not in r.json()["dest"], r.json()


# ---------------------------------------------------------------------------
# 7. Restore flows
# ---------------------------------------------------------------------------

def test_restore_all_roundtrip(client, seeded):
    dest = Path(seeded["env"]) / "qa1_restore_out"
    r = client.post("/api/restore/all", json={
        "source_root": seeded["source_root"], "dest_path": str(dest)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "started" and body["restoring"] >= 1

    def done():
        st = client.get("/api/restore/status").json()
        return st["completed"] and not st["running"]

    assert _wait_for(done), "restore never completed"
    st = client.get("/api/restore/status").json()
    assert st["errors"] == 0, st["error_list"]
    restored = dest / "seed" / "delta.txt"
    assert restored.exists()
    assert restored.read_bytes() == b"delta content 123"


def test_restore_all_missing_params_400(client, seeded):
    r = client.post("/api/restore/all", json={"source_root": "x"})
    assert r.status_code == 400
    r2 = client.post("/api/restore/all", json={"dest_path": "x"})
    assert r2.status_code == 400


@pytest.mark.xfail(reason="qa defect D7: /api/restore/all path_filter never "
                          "matches forward-slash (cPanel-style) rows — "
                          "startswith(path_filter + os.sep) only tests the "
                          "backslash prefix on Windows", strict=False)
def test_restore_all_subfolder_filter(client, seeded):
    dest = Path(seeded["env"]) / "qa1_restore_sub"
    r = client.post("/api/restore/all", json={
        "source_root": "QA1", "dest_path": str(dest), "path": "docs"})
    assert r.status_code == 200, r.text
    assert r.json()["restoring"] == 2, r.json()

    def done():
        st = client.get("/api/restore/status").json()
        return st["completed"] and not st["running"]

    assert _wait_for(done)
    names = sorted(p.name for p in dest.rglob("*.txt"))
    assert names == ["alpha.txt", "beta.txt"], names


def test_restore_all_skips_unsafe_index_paths(client, seeded):
    with write_txn() as conn:
        conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
            "VALUES ('POISON','..\\evil.txt',?,1,'vg-evil')", (_sha("e1"),))
        conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
            "VALUES ('POISON','C:\\temp\\evil2.txt',?,1,'vg-evil2')", (_sha("e2"),))
    dest = Path(seeded["env"]) / "qa1_restore_poison"
    r = client.post("/api/restore/all", json={
        "source_root": "POISON", "dest_path": str(dest)})
    assert r.status_code == 200

    def done():
        st = client.get("/api/restore/status").json()
        return st["completed"] and not st["running"]

    assert _wait_for(done)
    st = client.get("/api/restore/status").json()
    assert st["errors"] == 2, st
    assert all("unsafe path skipped" in e for e in st["error_list"]), st["error_list"]
    assert not dest.exists() or not any(dest.rglob("evil*")), "nothing may be written outside dest"


@pytest.mark.xfail(reason="qa defect D8: the restore worker's unsafe-path "
                          "guard misses drive-less POSIX-absolute rows "
                          "('/abs/x') on Windows — Path.is_absolute() is "
                          "False without a drive, and dest / '/abs/x' "
                          "resolves OUTSIDE the destination (observed: the "
                          "worker created J:\\abs from a poisoned row)",
                   strict=False)
def test_restore_all_guard_covers_posix_absolute_paths(client, seeded):
    with write_txn() as conn:
        conn.execute(
            "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
            "VALUES ('POISON2','/abs/evil3.txt',?,1,'vg-evil3')", (_sha("e3"),))
    dest = Path(seeded["env"]) / "qa1_restore_poison2"
    try:
        r = client.post("/api/restore/all", json={
            "source_root": "POISON2", "dest_path": str(dest)})
        assert r.status_code == 200

        def done():
            st = client.get("/api/restore/status").json()
            return st["completed"] and not st["running"]

        assert _wait_for(done)
        st = client.get("/api/restore/status").json()
        assert st["errors"] == 1, st
        assert any("unsafe path skipped" in e for e in st["error_list"]), st["error_list"]
        # nothing may exist outside the destination for this poisoned row
        stray = f"{dest.drive}\\abs"
        assert not Path(stray).exists(), f"worker escaped the destination: {stray}"
    finally:
        stray_path = Path(f"{dest.drive}\\abs")
        if stray_path.exists() and not any(stray_path.iterdir()):
            stray_path.rmdir()


@pytest.mark.xfail(reason="qa defect D9: the restore worker ignores "
                          "download_file error results ({'status': 'error'} "
                          "without exception) — chunk-less rows are counted "
                          "as restored (done++) with errors==0, so a broken "
                          "restore is reported as a success", strict=False)
def test_restore_worker_counts_broken_rows_as_errors(client, seeded):
    dest = Path(seeded["env"]) / "qa1_restore_broken"
    r = client.post("/api/restore/all", json={
        "source_root": "QA1", "dest_path": str(dest), "path": "ghost.bin"})
    assert r.status_code == 200

    def done():
        st = client.get("/api/restore/status").json()
        return st["completed"] and not st["running"]

    assert _wait_for(done)
    st = client.get("/api/restore/status").json()
    assert st["errors"] >= 1, st
    assert st["done"] == 0, st


def test_restore_files_by_ids(client, seeded):
    dest = Path(seeded["env"]) / "qa1_restore_ids"
    r = client.post("/api/restore/files", json={
        "dest_path": str(dest),
        "file_ids": [seeded["file_id"], 99999999]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "started"
    assert body.get("skipped") == 1, body

    def done():
        st = client.get("/api/restore/status").json()
        return st["completed"] and not st["running"]

    assert _wait_for(done)
    st = client.get("/api/restore/status").json()
    assert st["errors"] == 0, st["error_list"]
    assert st["total"] == 1
    restored = dest / "seed" / "delta.txt"
    assert restored.exists() and restored.read_bytes() == b"delta content 123"


def test_restore_files_validation(client, seeded):
    checks = [
        {"dest_path": "", "file_ids": [seeded["file_id"]]},
        {"dest_path": str(Path(seeded["env"]) / "x"), "file_ids": []},
        {"dest_path": str(Path(seeded["env"]) / "x")},
        {"dest_path": str(Path(seeded["env"]) / "x"), "file_ids": ["abc"]},
        {"dest_path": str(Path(seeded["env"]) / "x"), "file_ids": [99999999]},
    ]
    for body in checks:
        r = client.post("/api/restore/files", json=body)
        assert r.status_code == 400, f"{body} -> {r.status_code}: {r.text[:160]!r}"


def test_restore_conflict_409_while_running(client, seeded):
    """Second restore start while one is running must 409."""
    ids = _add_restore_rows(800)
    dest = Path(seeded["env"]) / "qa1_restore_409"
    saw_409 = False
    try:
        for attempt in range(4):
            r1 = client.post("/api/restore/all", json={
                "source_root": "BULK", "dest_path": str(dest)})
            assert r1.status_code == 200, r1.text

            def running():
                st = client.get("/api/restore/status").json()
                return st["running"]

            if _wait_for(running, timeout=2.0):
                r2 = client.post("/api/restore/all", json={
                    "source_root": "QA1", "dest_path": str(dest)})
                if r2.status_code == 409:
                    saw_409 = True
            assert _wait_for(lambda: client.get("/api/restore/status").json()["completed"],
                             timeout=30), "restore never completed between attempts"
        assert saw_409, "never observed 409 while a restore was running"
    finally:
        # Leave NO restore running: a lingering worker thread keeps its
        # thread-local sqlite connection open, which on Windows locks the
        # DB file and can break the next test's fresh-DB initialization.
        assert _wait_for(lambda: client.get("/api/restore/status").json()["completed"],
                         timeout=30), "restore still running at teardown"
        gc.collect()


def test_restore_stream_emits_and_terminates(client, seeded):
    dest = Path(seeded["env"]) / "qa1_restore_sse"
    start = client.post("/api/restore/all", json={
        "source_root": seeded["source_root"], "dest_path": str(dest)})
    assert start.status_code == 200
    events = _drain_sse_bounded(client, "/api/restore/stream")
    last = _assert_sse_terminated(events)
    assert last["done"] >= 1 and last["errors"] == 0, last


def test_download_roundtrip_and_404s(client, seeded):
    r = client.get(f"/api/download/{seeded['file_id']}")
    assert r.status_code == 200, r.text
    assert r.content == b"delta content 123"
    r404 = client.get("/api/download/999999")
    assert r404.status_code == 404
    assert "999999" in r404.json()["error"]


@pytest.mark.xfail(reason="qa defect D10: /api/download/{id} names the file "
                          "after its temp staging file (filename=\"dl_{id}\") "
                          "instead of the original relative_path basename, so "
                          "every browser download gets a meaningless name",
                   strict=False)
def test_download_serves_original_filename(client, seeded):
    r = client.get(f"/api/download/{seeded['file_id']}")
    assert r.status_code == 200, r.text
    assert "delta.txt" in r.headers.get("content-disposition", ""), \
        r.headers.get("content-disposition")


def test_download_to_path(client, seeded):
    target = Path(seeded["env"]) / "qa1_dl_target" / "out.txt"
    r = client.post(f"/api/download/{seeded['file_id']}/to-path",
                    json={"path": str(target)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["size"] == len(b"delta content 123")
    assert target.read_bytes() == b"delta content 123"
    # missing path / unknown id -> clean error bodies with real status codes
    # (D11 fixed: 400 for missing path, 404 for unknown id — never a 500 or
    # a 200-with-error)
    r2 = client.post(f"/api/download/{seeded['file_id']}/to-path", json={})
    assert r2.status_code == 400 and r2.json()["status"] == "error", r2.text
    r3 = client.post("/api/download/999999/to-path",
                     json={"path": str(target.parent / "nope.txt")})
    assert r3.status_code == 404, f"unknown id -> {r3.status_code}: {r3.text[:200]!r}"
    assert r3.json()["status"] == "error"


def test_download_to_path_input_errors_use_error_status_codes(client, seeded):
    """D11 fixed: input errors answer 400/404 like every other endpoint."""
    r2 = client.post(f"/api/download/{seeded['file_id']}/to-path", json={})
    assert r2.status_code == 400, r2.text
    r3 = client.post("/api/download/999999/to-path", json={"path": "somewhere"})
    assert r3.status_code == 404, r3.text


def test_file_detail_and_versions_and_replication(client, seeded):
    r = client.get(f"/api/files/{seeded['file_id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["relative_path"] == "seed/delta.txt"
    assert isinstance(detail["chunks"], list) and detail["chunks"], detail
    assert all(c.get("backend_name") == "test_local" for c in detail["chunks"]), detail["chunks"]
    r404 = client.get("/api/files/999999")
    assert r404.status_code == 404

    r2 = client.get(f"/api/files/{seeded['file_id']}/versions")
    assert r2.status_code == 200 and isinstance(r2.json(), list)
    r3 = client.get("/api/files/999999/versions")
    assert r3.status_code == 404

    r4 = client.get(f"/api/files/{seeded['file_id']}/replication")
    assert r4.status_code == 200
    reps = r4.json()
    assert reps and all(rep["copy_count"] >= 1 for rep in reps), reps
    assert any(c["backend_name"] == "test_local"
               for rep in reps for c in rep["copies"]), reps


def test_file_verify_endpoint(client, seeded):
    r = client.post(f"/api/files/{seeded['file_id']}/verify")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body


def test_file_delete_then_restore_visibility(client, seeded):
    r = client.delete(f"/api/files/{seeded['direct_ids'][0]}")
    assert r.status_code == 200, r.text
    listing = client.get("/api/files").json()
    assert seeded["direct_ids"][0] not in [f["id"] for f in listing]
    r404 = client.delete("/api/files/999999")
    assert r404.status_code == 404


def test_restore_status_idle_initially(client, seeded):
    st = client.get("/api/restore/status").json()
    assert st["running"] is False and st["completed"] is False


# ---------------------------------------------------------------------------
# 8. Browse + tree
# ---------------------------------------------------------------------------

def test_browse_roots_listing(client, fs_tree):
    r = client.get("/api/browse")
    assert r.status_code == 200
    body = r.json()
    assert set(body["drives"]) >= {"T1", "T2"}
    assert body["dirs"] == [] and body["files"] == []


def test_browse_forward_and_backslash_paths(client, fs_tree):
    r = client.get("/api/browse", params={"root": "T1"})
    assert r.status_code == 200
    body = r.json()
    assert body["dirs"] == ["sub"]
    assert {f["relative_path"] for f in body["files"]} == {"rootfile.txt"}

    r2 = client.get("/api/browse", params={"root": "T1", "path": "sub"})
    body2 = r2.json()
    assert body2["dirs"] == ["nested"]
    assert {f["relative_path"] for f in body2["files"]} == {"sub/alpha.txt", "sub/beta.txt"}

    r3 = client.get("/api/browse", params={"root": "T1", "path": "sub/"})
    assert r3.json()["dirs"] == ["nested"], "trailing separator tolerated"
    r4 = client.get("/api/browse", params={"root": "T1", "path": "sub\\"})
    assert r4.json()["dirs"] == ["nested"], "trailing backslash tolerated"

    r5 = client.get("/api/browse", params={"root": "T1", "path": "sub/nested"})
    assert r5.json()["dirs"] == []
    assert {f["relative_path"] for f in r5.json()["files"]} == {"sub/nested/epsilon.log"}

    rb = client.get("/api/browse", params={"root": "T2", "path": "bs"})
    assert rb.json()["dirs"] == ["deep"]
    assert {f["relative_path"] for f in rb.json()["files"]} == {"bs\\top.txt"}


def test_browse_unknown_root_is_clean_empty(client, seeded):
    r = client.get("/api/browse", params={"root": "NOPE"})
    assert r.status_code == 200
    assert r.json()["dirs"] == [] and r.json()["files"] == []


def test_tree_roots_and_full_tree(client, fs_tree):
    r = client.get("/api/tree")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "roots"
    t1 = next(i for i in body["items"] if i["name"] == "T1")
    assert t1["files"] == 4 and t1["size"] == 30 + 11 + 12 + 5, t1

    r2 = client.get("/api/tree", params={"root": "T1"})
    assert r2.status_code == 200
    items = r2.json()["items"]
    folders = [i for i in items if i["type"] == "folder"]
    files = [i for i in items if i["type"] == "file"]
    assert [f["name"] for f in folders] == ["sub"]
    assert folders[0]["path"] == "sub"
    assert folders[0]["files"] == 3, "aggregated count must include nested"
    assert {f["name"] for f in files} == {"rootfile.txt"}
    nested = [c for c in folders[0]["children"] if c["type"] == "folder"][0]
    assert nested["name"] == "nested" and nested["path"] == "sub\\nested"
    leaf = [c for c in nested["children"] if c["type"] == "file"]
    assert leaf[0]["name"] == "epsilon.log"


def test_tree_unknown_root_empty(client, seeded):
    r = client.get("/api/tree", params={"root": "NOPE"})
    assert r.status_code == 200 and r.json()["items"] == []


# ---------------------------------------------------------------------------
# 9. Health surface
# ---------------------------------------------------------------------------

def test_health_data_shape(client, seeded):
    r = client.get("/api/health/data")
    assert r.status_code == 200
    body = r.json()
    assert "summary" in body and "scan" in body
    assert body["scan_running"] is False
    assert body["scan"]["completed"] is False


def test_health_detailed_alias_matches_data(client, seeded):
    a = client.get("/api/health/data").json()
    b = client.get("/api/health/detailed").json()
    assert a == b


def test_health_issues_pagination_and_bad_reason(client, seeded):
    r = client.get("/api/health/issues", params={"reason": "no_chunks", "limit": 10, "offset": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"] == "no_chunks"
    assert body["total"] >= 1, "the chunk-less ghost row is a no_chunks issue"
    r2 = client.get("/api/health/issues", params={"limit": 1, "offset": 1})
    assert r2.status_code == 200
    r3 = client.get("/api/health/issues", params={"reason": "made_up_reason"})
    assert r3.status_code == 400 and "error" in r3.json()


def test_health_same_name_groups(client, seeded):
    r = client.get("/api/health/same-name")
    assert r.status_code == 200
    body = r.json()
    assert body["total_groups"] >= 1 and body["total_files"] >= 2, body
    groups = {g["base_name"]: g["file_count"] for g in body["groups"]}
    assert groups.get("notes.txt") == 2, groups
    members = next(g["members"] for g in body["groups"] if g["base_name"] == "notes.txt")
    assert {m["source_root"] for m in members} == {"QA2", "QA3"}, members


def test_health_purge_candidates_lists_ghost(client, seeded):
    r = client.get("/api/health/purge-candidates")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert seeded["ghost_id"] in [i["file_id"] for i in body["items"]], body


def test_health_scan_flow_and_stream(client, seeded):
    r = client.post("/api/health/scan")
    assert r.status_code == 200 and r.json()["status"] == "started"

    def done():
        data = client.get("/api/health/data").json()
        return data["scan"]["completed"] and not data["scan_running"]

    assert _wait_for(done), "health scan never completed"
    data = client.get("/api/health/data").json()
    assert "test_local" in data["scan"]["results"], data["scan"]["results"]
    result = data["scan"]["results"]["test_local"]
    # the seeded blob is a bundle parent + one member byte-range
    assert result["bundles_ok"] >= 1, result
    assert result["members_ok"] >= 1, result
    assert result["bundles_missing"] == [] and result["members_unreachable"] == [], result
    events = _drain_sse_bounded(client, "/api/health/scan/stream")
    _assert_sse_terminated(events)


# ---------------------------------------------------------------------------
# 10. Backends
# ---------------------------------------------------------------------------

def test_backends_list_and_detail(client, seeded):
    r = client.get("/api/backends")
    assert r.status_code == 200
    names = [b["name"] for b in r.json()]
    assert "test_local" in names
    for b in r.json():
        for key in ("name", "type", "connected", "free_space", "total_space",
                    "priority", "quota"):
            assert key in b, f"list shape missing {key}: {b}"
    r2 = client.get("/api/backends/test_local")
    assert r2.status_code == 200
    detail = r2.json()
    assert detail["name"] == "test_local"
    assert "password" not in (detail.get("config") or {}), "detail must never leak the password"
    r404 = client.get("/api/backends/no_such")
    assert r404.status_code == 404 and r404.json() == {"error": "not found"}


def test_backends_usage_shape_and_order(client, seeded):
    r = client.get("/api/backends/usage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"backends"}, "usage must not be shadowed by /{name}"
    entry = next(b for b in body["backends"] if b["name"] == "test_local")
    assert entry["stored_bytes"] >= 15, entry  # the seeded blob bytes
    for key in ("blobs", "ingest_24h_bytes", "free_space", "total_space",
                "connected", "type"):
        assert key in entry, f"usage shape missing {key}: {entry}"


def test_backend_test_endpoint(client, seeded):
    r = client.get("/api/backends/test/test_local")
    assert r.status_code == 200
    assert r.json()["connected"] is True, r.json()
    r2 = client.get("/api/backends/test/no_such")
    assert r2.status_code == 200
    assert r2.json() == {"connected": False, "error": "not found"}


def test_backend_add_then_visible(client, seeded):
    r = client.post("/api/backends/add", json={
        "type": "local", "name": "qa1_be2",
        "path": str(Path(seeded["env"]).parent / "qa1_be2_storage"),
        "priority": "80"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    names = [b["name"] for b in client.get("/api/backends").json()]
    assert "qa1_be2" in names
    missing_type = client.post("/api/backends/add", json={"name": "x"})
    assert missing_type.status_code == 200
    assert missing_type.json()["status"] == "error", "missing type/name must be a clean error"


def test_rebalance_stream_requires_op_or_terminates_with_op(client, seeded):
    # The stream only terminates once a rebalance op ran; drive a no-op-ish
    # rebalance on the unknown backend -> worker errors out -> completed=True.
    r = client.post("/api/backends/no_such/rebalance", json={})
    assert r.status_code == 404, r.text
    # use the real backend so the worker runs and completes immediately
    r2 = client.post("/api/backends/test_local/rebalance", json={})
    assert r2.status_code == 200 and r2.json()["status"] == "started", r2.text
    # no status endpoint exists for rebalance; the bounded drain is the proof
    events = _drain_sse_bounded(client, "/api/backends/rebalance/stream", timeout=30)
    assert events, "rebalance stream emitted nothing"
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is True and last["running"] is False, last


# ---------------------------------------------------------------------------
# 11. dbops
# ---------------------------------------------------------------------------

def test_db_info_shape(client, seeded):
    r = client.get("/api/db/info")
    assert r.status_code == 200
    body = r.json()
    for key in ("generation", "instance_id", "size", "integrity_status",
                "integrity_errors", "wal_mode"):
        assert key in body, f"db info missing {key}: {body}"
    assert body["integrity_status"] == "ok"
    assert body["wal_mode"] is True


def test_db_snapshots_flow(client, seeded):
    assert client.get("/api/db/snapshots").json() == []
    made = client.post("/api/db/snapshot")
    assert made.status_code == 200 and made.json()["status"] == "ok", made.text
    snaps = client.get("/api/db/snapshots").json()
    assert len(snaps) == 1 and snaps[0]["size"] > 0
    # op_log bookkeeping for snapshot
    logs = client.get("/api/logs", params={"op": "snapshot", "limit": 5}).json()
    assert any(o["status"] == "completed" for o in logs), logs
    # download the snapshot
    dl = client.get(f"/api/db/snapshots/{snaps[0]['name']}/download")
    assert dl.status_code == 200 and len(dl.content) == snaps[0]["size"]
    dl404 = client.get("/api/db/snapshots/not-a-snapshot.db.zst/download")
    assert dl404.status_code == 404


def test_db_recovery_empty(client, seeded):
    r = client.get("/api/db/recovery")
    assert r.status_code == 200 and r.json() == []


def test_db_check_checkpoint(client, seeded):
    r = client.post("/api/db/check")
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    r2 = client.post("/api/db/checkpoint")
    assert r2.status_code == 200 and r2.json()["status"] == "ok", r2.text


def test_db_replicate_local_backend(client, seeded):
    r = client.post("/api/db/replicate")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok", r.text


# ---------------------------------------------------------------------------
# 12. Ingest
# ---------------------------------------------------------------------------

def test_ingest_status_shape(client, seeded):
    r = client.get("/api/ingest/status")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("enabled", "drop_root", "staging", "sqlite", "parquet",
                "accounts", "checkpoint", "running", "paused", "queue", "errors"):
        assert key in body, f"ingest status missing {key}: {body.keys()}"
    assert body["sqlite"]["files"] >= 7
    assert body["running"] is False and body["paused"] is False


def test_ingest_generations(client, seeded):
    r = client.get("/api/ingest/generations")
    assert r.status_code == 400 and "account" in r.json()["error"]
    r2 = client.get("/api/ingest/generations", params={"account": "QA1"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["account"] == "QA1"
    assert isinstance(body["generations"], list)


def test_ingest_history_templates(client, seeded):
    r = client.get("/api/ingest/history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["template"] == "member_history" and isinstance(body["rows"], list)
    r2 = client.get("/api/ingest/history", params={"template": "account_bytes", "limit": 5})
    assert r2.status_code == 200
    r3 = client.get("/api/ingest/history", params={"template": "recent", "limit": 3})
    assert r3.status_code == 200 and len(r3.json()["rows"]) <= 3
    r4 = client.get("/api/ingest/history", params={"template": "bogus"})
    assert r4.status_code == 400 and "unknown template" in r4.json()["error"]


def test_ingest_trend(client, seeded):
    r = client.get("/api/stats/ingest-trend")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_ingest_pause_resume_gate(client, seeded):
    r = client.post("/api/ingest/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    st = client.get("/api/ingest/status").json()
    assert st["paused"] is True
    drop = Path(seeded["env"]) / "drop_paused"
    drop.mkdir(exist_ok=True)
    r2 = client.post("/api/ingest/scan", json={"drop_root": str(drop)})
    assert r2.status_code == 409, "scan must refuse while paused"
    assert "resume" in r2.json()["error"], r2.json()
    r3 = client.post("/api/ingest/resume")
    assert r3.status_code == 200 and r3.json()["status"] == "resumed"
    assert client.get("/api/ingest/status").json()["paused"] is False


def test_ingest_scan_validation(client, seeded):
    r = client.post("/api/ingest/scan", json={})
    assert r.status_code == 400, "missing drop_root with no config -> 400"
    r2 = client.post("/api/ingest/scan", json={"drop_root": str(Path(seeded["env"]) / "nope")})
    assert r2.status_code == 404


def test_ingest_stream_terminates_after_empty_scan(client, seeded):
    drop = Path(seeded["env"]) / "drop_empty"
    drop.mkdir(exist_ok=True)
    start = client.post("/api/ingest/scan", json={"drop_root": str(drop)})
    assert start.status_code == 200 and start.json()["status"] == "started"
    events = _drain_sse_bounded(client, "/api/ingest/stream")
    last = _assert_sse_terminated(events)
    assert last["archives_total"] == 0
    assert not last["errors"]


# ---------------------------------------------------------------------------
# 13. Scheduler / daemon
# ---------------------------------------------------------------------------

def test_daemon_status_idle(client, seeded):
    r = client.get("/api/daemon/status")
    assert r.status_code == 200
    assert r.json() == {"running": False}


def test_scheduler_config_get_and_save(client, seeded):
    r = client.get("/api/scheduler/config")
    assert r.status_code == 200
    body = r.json()
    assert "interval" in body and "enabled" in body
    r2 = client.post("/api/scheduler/config", json={"interval": 1234, "enabled": True})
    assert r2.status_code == 200 and r2.json()["status"] == "ok"
    r3 = client.get("/api/scheduler/config")
    assert r3.json()["interval"] == 1234 and r3.json()["enabled"] is True
    # daemon stays untouched
    assert client.get("/api/daemon/status").json() == {"running": False}


# ---------------------------------------------------------------------------
# 14. Logs / report / encrypt / misc
# ---------------------------------------------------------------------------

def test_api_logs_filters(client, seeded):
    r = client.get("/api/logs")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 3
    r2 = client.get("/api/logs", params={"status": "completed"})
    assert all(o["status"] == "completed" for o in r2.json())
    r3 = client.get("/api/logs", params={"status": "failed"})
    assert all(o["status"] == "failed" for o in r3.json()) and r3.json()
    r4 = client.get("/api/logs", params={"limit": 1})
    assert len(r4.json()) == 1


def test_logs_export_csv_and_json(client, seeded):
    r = client.get("/api/logs/export", params={"fmt": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows and "op_type" in rows[0]
    r2 = client.get("/api/logs/export", params={"fmt": "json"})
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("application/json")
    data = json.loads(r2.text)
    assert isinstance(data, list) and data


def test_api_report_and_reports_history(client, seeded):
    r = client.get("/api/report")
    assert r.status_code == 200
    body = r.json()
    assert "op_counters" in body, "B09 counters must be surfaced"
    r2 = client.get("/api/reports")
    assert r2.status_code == 200
    reports = r2.json()
    assert len(reports) == 2
    rid = reports[0]["id"]
    r3 = client.get(f"/api/reports/{rid}")
    assert r3.status_code == 200 and r3.json()["id"] == rid
    r404 = client.get("/api/reports/999999")
    assert r404.status_code == 404


def test_report_generate_runs_integrity_check(client, seeded):
    r = client.post("/api/report/generate", params={"count": 12})
    assert r.status_code == 200, r.text
    body = r.json()
    # The seeded dataset intentionally contains chunk-less rows, so the
    # sampled check may legitimately report failures; the contract under
    # test is that the check RAN, counted files, and was audited to op_log.
    # count=12 > the 9 seeded rows, so the real uploaded file is always
    # sampled (checked >= 1 deterministically).
    assert body["status"] in ("completed", "failed"), body
    assert body["checked"] >= 1, body
    assert body["failed"] + body["checked"] >= 1, body
    logs = client.get("/api/logs", params={"op": "report", "limit": 5}).json()
    assert any(o["status"] == "completed" for o in logs), logs


def test_report_generate_op_log_counters_are_real(client, seeded):
    """D16 fixed: op_log target_json now carries the real checked/failed
    counts (run_integrity_check's 'checked'/'failed' keys)."""
    client.post("/api/report/generate", params={"count": 12})
    logs = client.get("/api/logs", params={"op": "report", "limit": 5}).json()
    row = next(o for o in logs if o["op_type"] == "report")
    payload = json.loads(row["target_json"] or "{}")
    assert payload.get("checked_files", 0) >= 1, payload


def test_encrypt_status_and_init(client, seeded):
    r = client.get("/api/encrypt/status")
    assert r.status_code == 200
    body = r.json()
    assert body["key_exists"] is False
    assert "capabilities" in body and isinstance(body["capabilities"], dict), body
    r2 = client.post("/api/encrypt/init")
    assert r2.status_code == 200 and r2.json()["status"] == "ok", r2.text
    r3 = client.get("/api/encrypt/status")
    assert r3.json()["key_exists"] is True and r3.json()["key_size"] > 0


def test_api_status_shape(client, seeded):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("total_files", "blob_count", "stored_bytes", "dedup_ratio",
                "generation", "backends"):
        assert key in body, f"status missing {key}: {body.keys()}"
    assert body["total_files"] >= 7
    be = next(b for b in body["backends"] if b["name"] == "test_local")
    assert be["stored_bytes"] >= 15


def test_activity_and_trends(client, seeded):
    r = client.get("/api/activity", params={"limit": 2})
    assert r.status_code == 200 and len(r.json()) <= 2
    r2 = client.get("/api/stats/trend")
    assert r2.status_code == 200 and isinstance(r2.json(), list)
    r3 = client.get("/api/stats/storage-trend")
    assert r3.status_code == 200 and isinstance(r3.json(), list)


def test_hash_lookup(client, seeded):
    r = client.get(f"/api/hash/{seeded['sha256']}")
    assert r.status_code == 200
    assert seeded["file_id"] in [f["id"] for f in r.json()]
    r2 = client.get(f"/api/hash/{seeded['sha256'][:10]}")
    assert r2.status_code == 200
    assert seeded["file_id"] in [f["id"] for f in r2.json()], "prefix fallback"
    r3 = client.get("/api/hash/" + "f" * 64)
    assert r3.status_code == 200 and r3.json() == []


def test_replication_status(client, seeded):
    r = client.get("/api/replication/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_blobs"] >= 1
    assert body["per_backend"].get("test_local", 0) >= 1
    assert body["single_copy"] + body["fully_replicated"] == body["total_blobs"]


def test_sources_roundtrip(client, seeded):
    r = client.get("/api/sources")
    assert r.status_code == 200
    sources = r.json()
    assert seeded["source_root"] in sources
    assert "QA1" in sources
    # add / duplicate / delete (isolated config.yaml)
    add = client.post("/api/config/sources", json={"source": "J:\\qa1_new_source"})
    assert add.status_code == 200 and add.json()["status"] == "ok", add.text
    dup = client.post("/api/config/sources", json={"source": "J:\\qa1_new_source"})
    assert dup.status_code == 400
    dele = client.delete("/api/config/sources", params={"source": "J:\\qa1_new_source"})
    assert dele.status_code == 200 and dele.json()["status"] == "ok"
    dele404 = client.delete("/api/config/sources", params={"source": "J:\\not_there"})
    assert dele404.status_code == 404


def test_config_export_and_save(client, seeded):
    r = client.get("/api/config/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/yaml")
    assert "test_local" in r.text
    r2 = client.post("/api/config", json={"retention": {"keep_days": 45}})
    assert r2.status_code == 200 and r2.json()["status"] == "ok"
    r3 = client.get("/api/config/export")
    assert "keep_days" in r3.text and "45" in r3.text


def test_convert_unknown_backend_clean_error(client, seeded):
    r = client.post("/api/convert/no_such_backend")
    assert r.status_code < 500, f"unknown backend convert -> {r.status_code}: {r.text[:200]!r}"
    assert r.json().get("status") == "error"


def test_recompress_runs_on_seeded_blob(client, seeded):
    r = client.post("/api/recompress")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    for key in ("recompressed", "total_scanned", "skipped_encrypted", "failed", "converted"):
        assert key in body, f"recompress counters missing {key}: {body}"
    # seeded blob is already zstd-compressed by the pipeline; it must not
    # corrupt anything either way
    dl = client.get(f"/api/download/{seeded['file_id']}")
    assert dl.status_code == 200 and dl.content == b"delta content 123"


def test_no_trailing_slash_404s_are_json_or_404(client, seeded):
    """Random unknown paths return 404 (FastAPI default), not 500."""
    for path in ("/api/no_such_endpoint", "/api/files/not-an-int"):
        r = client.get(path)
        assert r.status_code in (404, 422), f"{path} -> {r.status_code}"


# ---------------------------------------------------------------------------
# 15. Backup trigger endpoints (isolated env only)
# ---------------------------------------------------------------------------

def test_backup_all_no_sources_configured(client, seeded):
    r = client.post("/api/backup")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["results"] == [], "no backups configured -> nothing ran"


def test_backup_source_single_file(client, seeded):
    src = Path(seeded["env"]) / "backup_me.txt"
    src.write_bytes(b"qa1 backup source")
    r = client.post("/api/backup/source", json={"source": str(src)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok", body
    # searchable + downloadable afterwards
    found = client.get("/api/files", params={"q": "backup_me"}).json()
    assert any(f["relative_path"] == "backup_me.txt" for f in found), found
    logs = client.get("/api/logs", params={"op": "backup", "limit": 5}).json()
    assert any(o["status"] == "completed" for o in logs), logs


def test_backup_source_missing_path(client, seeded):
    r = client.post("/api/backup/source", json={"source": "J:\\definitely\\missing\\qa1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "error", body


def test_backup_scan_start_status_roundtrip(client, seeded):
    src = Path(seeded["env"]) / "scan_dir_qa1"
    src.mkdir(exist_ok=True)
    (src / "tiny1.txt").write_bytes(b"one")
    (src / "tiny2.txt").write_bytes(b"two")
    r = client.post("/api/backup/scan", json={"path": str(src), "backend": "test_local"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"

    def done():
        st = client.get("/api/backup/scan/status").json()
        return st["completed"] and not st["running"]

    assert _wait_for(done, timeout=30), "scan never completed"
    st = client.get("/api/backup/scan/status").json()
    assert st["files_total"] == 2 and st["files_error"] == 0, st
    r2 = client.post("/api/backup/scan", json={"path": ""})
    assert r2.status_code == 400
    r3 = client.post("/api/backup/scan", json={"path": "J:\\definitely\\missing"})
    assert r3.status_code == 404
    gc.collect()

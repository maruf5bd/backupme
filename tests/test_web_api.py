"""Black-box API regression suite for the BackupMe web dashboard (backlog B08).

Source material: the QA sweep in diagnostics/qa_dashboard_report.txt
(~126 checks across the P1 + P1AUTH phases, each with a repro) and the
harness scripts under diagnostics/qa_agent_tmp/ (used as read-only
reference; their ideas live on here as pytest checks).

What this file adds on top of the existing suites (deliberate
non-duplication contract — the checks below are referenced, not repeated):

- tests/test_web_defects.py       -> qa defects D3 (search punctuation
  never 500s), D4 (browse descends forward-slash rows), D5 (unknown ids
  on download/preview return 404 JSON).
- tests/test_web_health.py        -> /api/health/* buckets, the
  confirm-gated purge endpoint, the physical reachability scan and its
  SSE stream contract.
- tests/test_web_ingest.py        -> ingest scan flow, generations,
  history templates, pause/resume, plus the ingest and rebalance SSE
  streams with a real cpmove archive.
- tests/test_destructive_confirm.py -> confirm:true gates (qa D1/D2
  follow-ups): restart, db restore/rebuild, backend remove, snapshot
  delete/prune.
- tests/test_web_token_config.py  -> web.token config parsing and the
  bearer-format matrix (wrong/lowercase/empty token).

Groups implemented HERE:
  1. inventory + full read-only GET /api sweep (200 + parses as JSON),
  2. pagination contract for the paginated endpoints,
  3. error-shape contract for unknown names/ids (4xx JSON, never 500,
     never a hang),
  4. route-order regression (/api/backends/usage vs /api/backends/{name}),
  5. SSE contract for the four operation streams,
  6. bounded concurrency smoke (4 threads x 50 mixed GETs),
  7. auth-gate 401 matrix summary with a token-armed app instance.

Skipped by design (documented, QA sweep covered them):
  - GET /api/db/snapshots/{name}/download: needs a snapshot artifact and
    creating one is a state-changing op (qa report lines 96-98).
  - POST/PUT/DELETE routes: state-changing or confirm-gated; the
    destructive ones are covered by test_destructive_confirm.py.
  - GET /api/ingest/generations without an account returns 400 by
    design -> asserted in the error-shape group instead of the sweep.
  - GET /api/backends/rebalance/stream needs a second backend plus a
    real copy op -> covered by test_web_ingest.py; exempted from the
    SSE group here.
"""
import concurrent.futures
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import write_txn
from tests.helpers import backup_bytes

TOKEN = "b08-secret-token"

# FastAPI route templates covered by the JSON sweep (resolved onto the
# seeded fixture below). Kept in template form so the inventory test can
# diff the list against the app's actual route table.
SWEEP_PATHS = {
    "/api/status",
    "/api/backends",
    "/api/backends/usage",
    "/api/backends/{name}",
    "/api/backends/test/{name}",
    "/api/files",
    "/api/upload/folders",
    "/api/browse",
    "/api/tree",
    "/api/files/{file_id}",
    "/api/files/{file_id}/versions",
    "/api/files/{file_id}/replication",
    "/api/files/{file_id}/preview",
    "/api/sources",
    "/api/logs",
    "/api/logs/export",
    "/api/db/info",
    "/api/db/snapshots",
    "/api/db/recovery",
    "/api/report",
    "/api/reports",
    "/api/reports/{report_id}",
    "/api/search",
    "/api/hash/{sha256}",
    "/api/activity",
    "/api/stats/trend",
    "/api/stats/storage-trend",
    "/api/stats/ingest-trend",
    "/api/health/detailed",
    "/api/daemon/status",
    "/api/scheduler/config",
    "/api/replication/status",
    "/api/ingest/status",
    "/api/ingest/generations",
    "/api/ingest/history",
    "/api/encrypt/status",
    "/api/backup/scan/status",
    "/api/restore/status",
    "/api/health/data",
    "/api/health/issues",
    "/api/health/same-name",
    "/api/health/purge-candidates",
}

# Requested instead of the bare template where the bare call is a
# documented 400-by-design (see the error-shape group below).
REQUEST_OVERRIDES = {
    "/api/ingest/generations": "/api/ingest/generations?account=ghost",
}

# Extra query-string variants exercised on top of the bare templates
# (concrete seeded values are substituted at request time).
SWEEP_QUERY_VARIANTS = [
    "/api/files?q=delta",
    "/api/files?backend=test_local",
    "/api/browse?root=S1&path=docs",
    "/api/tree?root=S1",
    "/api/search?q=delta",
    "/api/ingest/history?template=recent&limit=10",
    "/api/health/issues?reason=no_chunks&limit=2&offset=0",
]

# SSE endpoints — each has its own contract test below; the streams stay
# out of the JSON sweep because they block until an operation completes.
STREAM_PATHS = {
    "/api/restore/stream",
    "/api/backup/scan/stream",
    "/api/ingest/stream",
    "/api/backends/rebalance/stream",  # covered by test_web_ingest.py
    "/api/health/scan/stream",
}

# Read-only GETs asserted outside the JSON sweep (non-JSON bodies).
SEPARATE_NON_JSON = {
    "/api/download/{file_id}",
    "/api/config/export",
}

# Needs a snapshot artifact; creating one is a state-changing op.
EXPLICITLY_SKIPPED = {
    "/api/db/snapshots/{name}/download",
}

# Static paths for the auth-gate matrix summary (no fixture data needed;
# the middleware answers before any handler, so literal ids are fine).
AUTH_STATIC_PATHS = [
    "/api/status",
    "/api/backends",
    "/api/backends/usage",
    "/api/backends/test_local",
    "/api/backends/test/test_local",
    "/api/files",
    "/api/files?q=delta",
    "/api/browse",
    "/api/tree",
    "/api/files/1",
    "/api/download/1",
    "/api/sources",
    "/api/logs",
    "/api/logs/export",
    "/api/db/info",
    "/api/db/snapshots",
    "/api/db/recovery",
    "/api/report",
    "/api/reports",
    "/api/reports/1",
    "/api/search?q=delta",
    "/api/activity",
    "/api/stats/trend",
    "/api/stats/storage-trend",
    "/api/stats/ingest-trend",
    "/api/health/detailed",
    "/api/daemon/status",
    "/api/scheduler/config",
    "/api/replication/status",
    "/api/ingest/status",
    "/api/ingest/generations?account=ghost",
    "/api/ingest/history",
    "/api/encrypt/status",
    "/api/backup/scan/status",
    "/api/restore/status",
    "/api/health/data",
    "/api/health/issues",
    "/api/health/same-name",
    "/api/health/purge-candidates",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bounded_get(client: TestClient, path: str, timeout: float = 10.0):
    """GET with a hard wall-clock bound.

    QA contract: a read endpoint must never hang. A request that does not
    answer within `timeout` fails the test instead of stalling the suite.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(client.get, path)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            pytest.fail(f"GET {path} did not answer within {timeout:.0f}s (hang guard)")
    finally:
        # Never join a potentially-stuck worker on the way out.
        executor.shutdown(wait=False, cancel_futures=True)


def _wait_for(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _drain_sse(client: TestClient, path: str) -> list[str]:
    """Read an SSE stream to its clean end; return the `data:` frames."""
    with client.stream("GET", path) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream"), \
            f"{path} content-type: {resp.headers['content-type']}"
        raw = "".join(resp.iter_text())
    return [e for e in raw.split("\n\n") if e.strip()]


def _drain_sse_bounded(client: TestClient, path: str, timeout: float = 20.0) -> list[str]:
    """Drain an SSE stream with a hard wall-clock bound: a stream that
    never terminates fails the test instead of hanging the suite."""
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
    """Every frame is `data: <json>`; the last snapshot is completed."""
    assert events, "stream closed without emitting a single SSE event"
    for event in events:
        assert event.startswith("data: "), f"malformed SSE frame: {event[:80]!r}"
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is True, f"stream did not end completed: {last}"
    assert last["running"] is False, f"stream did not end idle: {last}"
    return last


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures (replicated from the existing tests' pattern; conftest.py is
# shared read-only: env/backends give the isolated config + DB + backend)
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(env, backends):
    from backupme.config import load_config
    return create_app(load_config())


@pytest.fixture()
def client(app):
    # raise_server_exceptions=False: this is a black-box HTTP-contract
    # suite — a regression must show up as a 5xx status, not a traceback.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def seeded(env, backends):
    """Dataset for the sweep, pagination and restore checks:

    - 7 index rows (no chunks) inserted directly with distinct created_at
      timestamps so /api/files pagination order is deterministic;
    - 2 same-name groups (notes.txt x2, report.pdf x2) for
      /api/health/same-name;
    - 5 op_log rows (3 completed / 2 failed) for /api/logs, /api/activity
      and /api/stats/trend;
    - 3 integrity_checks rows for /api/reports;
    - 1 real uploaded file through the actual pipeline (upload_bundle via
      tests.helpers.backup_bytes) for download / preview / hash / search /
      restore / backend-filter endpoints.
    """
    stamp = "2020-01-0{} 10:00:00"
    direct_ids = []
    rows = [
        ("S1", "docs/a_one.txt"),
        ("S1", "docs/a_two.txt"),
        ("S1", "docs/a_three.txt"),
        ("S2", "report.pdf"),
        ("S3", "report.pdf"),
        ("S1", "notes.txt"),
        ("S4", "notes.txt"),
    ]
    with write_txn() as conn:
        for i, (source_root, relative_path) in enumerate(rows, start=1):
            created = stamp.format(i)
            cur = conn.execute(
                "INSERT INTO files (source_root, relative_path, sha256, size, "
                "version_group, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (source_root, relative_path, _sha(f"{source_root}/{relative_path}"),
                 10 * i, f"vg-b08-{i}", created, created),
            )
            direct_ids.append(cur.lastrowid)
        for i in range(1, 6):
            status = "completed" if i % 2 else "failed"
            started = f"2020-02-0{i} 12:00:00"
            conn.execute(
                "INSERT INTO op_log (idempotency_key, op_type, target_json, status, "
                "started_at, completed_at, last_error) VALUES (?,?,?,?,?,?,?)",
                (f"b08-op-{i}", "qa_probe", json.dumps({"probe": i}), status,
                 started, started if status == "completed" else None,
                 None if status == "completed" else "intentional probe failure"),
            )
        report_ids = []
        for i in (1, 2, 3):
            started = f"2020-03-01 10:00:0{i}"
            cur = conn.execute(
                "INSERT INTO integrity_checks (check_type, status, total_files, "
                "checked_files, failed_files, started_at, completed_at) "
                "VALUES ('qa_probe','completed',8,8,0,?,?)", (started, started),
            )
            report_ids.append(cur.lastrowid)

    result = backup_bytes(Path(env), "seed/delta.txt", b"delta content 123", backends)
    file_id = result["file_id"]
    return {
        "env": env,
        "file_id": file_id,
        "sha256": _db_scalar("SELECT sha256 FROM files WHERE id = ?", (file_id,)),
        "direct_ids": direct_ids,
        "report_id": report_ids[-1],  # newest started_at == /api/reports[0]
        "source_root": str(env),
    }


def _db_scalar(sql: str, params: tuple) -> object:
    from backupme.core.database import execute
    return execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# 1. Inventory + full read-only GET /api sweep
# ---------------------------------------------------------------------------

def test_sweep_inventory_covers_every_readonly_get_route(app):
    """Every GET /api route the app registers must be accounted for by the
    sweep list, the stream group, the separate non-JSON group, or the
    documented skip set — and vice versa. A new read endpoint (or a
    removed one) must force a conscious update of this suite."""
    app_get_paths = {
        r.path for r in app.routes
        if r.path.startswith("/api") and "GET" in getattr(r, "methods", set())
    }
    covered = SWEEP_PATHS | STREAM_PATHS | SEPARATE_NON_JSON | EXPLICITLY_SKIPPED
    uncovered = app_get_paths - covered
    assert not uncovered, (
        "GET routes not covered by tests/test_web_api.py "
        f"(add to the sweep or the documented skip set): {sorted(uncovered)}")
    stale = covered - app_get_paths
    assert not stale, (
        "test_web_api.py references GET routes the app no longer registers "
        f"(update the lists): {sorted(stale)}")


def test_readonly_get_sweep_all_200_and_json(client, seeded):
    """Full GET sweep: every read-only /api endpoint returns 200 and the
    body parses as JSON. Mirrors the qa_dashboard_report.txt P1 sweep
    ('api GET /api/...: PASS -- status=200') as a pytest check."""
    resolved = {
        "file_id": str(seeded["file_id"]),
        "report_id": str(seeded["report_id"]),
        "sha256": seeded["sha256"],
        "name": "test_local",
    }
    paths = [
        REQUEST_OVERRIDES.get(template, template).format(**resolved)
        for template in sorted(SWEEP_PATHS)
    ] + SWEEP_QUERY_VARIANTS
    assert len(paths) == len(SWEEP_PATHS) + len(SWEEP_QUERY_VARIANTS)

    failures = []
    for path in paths:
        try:
            r = _bounded_get(client, path)
            if r.status_code != 200:
                failures.append(f"GET {path} -> {r.status_code}: {r.text[:120]!r}")
                continue
            r.json()  # must parse as JSON
        except AssertionError as exc:
            failures.append(f"GET {path}: {exc}")
        except Exception as exc:  # JSONDecodeError or transport error
            failures.append(f"GET {path}: {exc!r}")
    assert not failures, (
        f"{len(failures)} of {len(paths)} read-only GET endpoints regressed:\n"
        + "\n".join(failures))


def test_download_round_trips_seeded_content(client, seeded):
    """GET /api/download/{id} returns the exact bytes (non-JSON endpoint,
    so it sits outside the JSON sweep). Unknown-id 404 shape: D5 in
    tests/test_web_defects.py."""
    r = _bounded_get(client, f"/api/download/{seeded['file_id']}")
    assert r.status_code == 200, r.text[:200]
    assert r.content == b"delta content 123"


def test_config_export_serves_yaml_config(client, seeded):
    r = _bounded_get(client, "/api/config/export")
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("text/yaml")
    assert "backends:" in r.text
    assert "test_local" in r.text


def test_logs_export_csv_content_type(client, seeded):
    r = _bounded_get(client, "/api/logs/export?fmt=csv")
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("text/csv")
    assert "idempotency_key" in r.text  # header row from the seeded op_log


# ---------------------------------------------------------------------------
# 2. Pagination contract (vary limit/offset; results must change and
#    respect the limit)
# ---------------------------------------------------------------------------

def test_files_pagination_changes_results_and_respects_limit(client, seeded):
    full = client.get("/api/files", params={"limit": 100}).json()
    assert len(full) == 8  # 7 direct rows + 1 uploaded through the pipeline

    page0 = client.get("/api/files", params={"limit": 2, "offset": 0}).json()
    page1 = client.get("/api/files", params={"limit": 2, "offset": 2}).json()
    assert len(page0) == 2 and len(page1) == 2
    ids0 = [f["id"] for f in page0]
    ids1 = [f["id"] for f in page1]
    assert set(ids0).isdisjoint(ids1), "pages must not overlap"

    # Distinct seeded created_at -> deterministic newest-first order:
    # the uploaded file is the most recent, then the direct rows in
    # reverse insertion order.
    assert ids0 == [seeded["file_id"], seeded["direct_ids"][6]]

    beyond = client.get("/api/files", params={"limit": 2, "offset": 100}).json()
    assert beyond == []

    # Walking all pages covers every row exactly once (qa pagination check).
    walked, offset = [], 0
    while True:
        page = client.get("/api/files", params={"limit": 2, "offset": offset}).json()
        if not page:
            break
        assert len(page) <= 2
        walked.extend(f["id"] for f in page)
        offset += 2
    assert sorted(walked) == sorted(f["id"] for f in full)


def test_files_backend_filter_narrows_to_stored_files(client, seeded):
    # Only the pipeline-uploaded file has blob_storage rows; the direct
    # index rows have no chunks and must be filtered out.
    filtered = client.get("/api/files", params={"backend": "test_local", "limit": 100}).json()
    assert [f["id"] for f in filtered] == [seeded["file_id"]]


def test_logs_pagination_and_status_filter(client, seeded):
    one = client.get("/api/logs", params={"limit": 1}).json()
    three = client.get("/api/logs", params={"limit": 3}).json()
    every = client.get("/api/logs").json()
    assert len(one) == 1 and len(three) == 3
    assert len(every) == 5  # 5 seeded op_log rows
    ids = [r["id"] for r in every]
    assert ids == sorted(ids, reverse=True)  # newest first
    completed = client.get("/api/logs", params={"status": "completed"}).json()
    assert len(completed) == 3
    assert all(r["status"] == "completed" for r in completed)
    failed = client.get("/api/logs", params={"status": "failed"}).json()
    assert len(failed) == 2
    assert all(r["status"] == "failed" for r in failed)


def test_activity_limit_changes_results(client, seeded):
    one = client.get("/api/activity", params={"limit": 1}).json()
    three = client.get("/api/activity", params={"limit": 3}).json()
    every = client.get("/api/activity").json()
    assert len(one) == 1 and len(three) == 3
    assert len(every) == 5
    assert one[0]["id"] == three[0]["id"] == every[0]["id"]  # same newest row


def test_reports_limit_changes_results_and_detail_round_trips(client, seeded):
    one = client.get("/api/reports", params={"limit": 1}).json()
    two = client.get("/api/reports", params={"limit": 2}).json()
    every = client.get("/api/reports").json()
    assert len(one) == 1 and len(two) == 2
    assert len(every) == 3
    assert one[0]["id"] == two[0]["id"]
    detail = client.get(f"/api/reports/{two[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == two[0]["id"]


def test_health_issues_pagination(client, seeded):
    p0 = client.get("/api/health/issues",
                    params={"reason": "no_chunks", "limit": 2, "offset": 0}).json()
    assert p0["total"] == 7  # every direct row is chunk-less
    assert p0["limit"] == 2 and p0["offset"] == 0
    assert len(p0["items"]) == 2

    p1 = client.get("/api/health/issues",
                    params={"reason": "no_chunks", "limit": 2, "offset": 2}).json()
    ids0 = [i["file_id"] for i in p0["items"]]
    ids1 = [i["file_id"] for i in p1["items"]]
    assert set(ids0).isdisjoint(ids1)
    assert ids0 == sorted(ids0) and ids1 == sorted(ids1)  # stable ORDER BY id

    beyond = client.get("/api/health/issues",
                        params={"reason": "no_chunks", "limit": 2, "offset": 100}).json()
    assert beyond["items"] == []


def test_health_same_name_pagination(client, seeded):
    full = client.get("/api/health/same-name").json()
    assert full["total_groups"] == 2  # notes.txt x2, report.pdf x2
    assert full["total_files"] == 4
    assert {g["base_name"] for g in full["groups"]} == {"notes.txt", "report.pdf"}

    p0 = client.get("/api/health/same-name", params={"limit": 1, "offset": 0}).json()
    p1 = client.get("/api/health/same-name", params={"limit": 1, "offset": 1}).json()
    assert len(p0["groups"]) == 1 and len(p1["groups"]) == 1
    assert p0["groups"][0]["base_name"] != p1["groups"][0]["base_name"]
    assert {p0["groups"][0]["base_name"], p1["groups"][0]["base_name"]} == \
        {g["base_name"] for g in full["groups"]}


def test_health_purge_candidates_pagination(client, seeded):
    p0 = client.get("/api/health/purge-candidates",
                    params={"limit": 2, "offset": 0}).json()
    assert p0["total"] == 7
    assert len(p0["items"]) == 2
    p1 = client.get("/api/health/purge-candidates",
                    params={"limit": 2, "offset": 2}).json()
    ids0 = [i["file_id"] for i in p0["items"]]
    ids1 = [i["file_id"] for i in p1["items"]]
    assert set(ids0).isdisjoint(ids1)
    assert ids0 == sorted(ids0)


def test_ingest_history_respects_limit_and_unknown_template_is_400(client, seeded):
    """History rows come from the parquet cold store, which is empty in the
    scratch env, so limit can only be asserted as an upper bound here;
    row-count-changing pagination over real history is covered by
    tests/test_db_lifecycle.py (cold export e2e)."""
    r = client.get("/api/ingest/history", params={"template": "recent", "limit": 10})
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["template"] == "recent"
    assert body["count"] == len(body["rows"]) <= 10

    r1 = client.get("/api/ingest/history", params={"template": "recent", "limit": 1})
    assert r1.status_code == 200
    assert r1.json()["count"] <= 1

    # Unknown template -> documented 400 JSON error (qa report line 187).
    bogus = client.get("/api/ingest/history", params={"template": "bogus"})
    assert bogus.status_code == 400
    assert "error" in bogus.json()


# ---------------------------------------------------------------------------
# 3. Error-shape contract: 4xx with a JSON body, never 500, never a hang
# ---------------------------------------------------------------------------

def test_unknown_backend_detail_is_404_json(client, seeded):
    r = _bounded_get(client, "/api/backends/ghost")
    assert r.status_code == 404
    body = r.json()
    assert isinstance(body, dict) and "error" in body


def test_unknown_backend_test_is_json_never_500_never_hangs(client, seeded):
    """The B08 spec asked for 4xx on /api/backends/test/{unknown}, but the
    implemented contract (routes.py api_backend_test) answers
    200 {"connected": false, "error": "not found"} so the dashboard can
    render the probe result. Pinned here with the invariants that matter:
    a JSON body that reports the failure, no 5xx, bounded latency. If
    this ever flips to a 4xx, update this test deliberately."""
    r = _bounded_get(client, "/api/backends/test/ghost")
    assert r.status_code < 500, f"unexpected 5xx: {r.status_code} {r.text[:120]!r}"
    body = r.json()
    if r.status_code >= 400:
        assert "error" in body
    else:
        assert body.get("connected") is False
        assert body.get("error")


def test_unknown_file_detail_is_404_json(client, seeded):
    r = _bounded_get(client, "/api/files/999999")
    assert r.status_code == 404
    assert "error" in r.json()


def test_unknown_file_versions_is_404_json(client, seeded):
    r = _bounded_get(client, "/api/files/999999/versions")
    assert r.status_code == 404
    assert "error" in r.json()


def test_non_integer_file_id_is_422_json(client, seeded):
    # FastAPI path validation: still 4xx JSON, never a 500 (qa line 221).
    r = _bounded_get(client, "/api/files/abc")
    assert r.status_code == 422
    assert "detail" in r.json()


def test_unknown_report_id_is_404_json(client, seeded):
    r = _bounded_get(client, "/api/reports/999999")
    assert r.status_code == 404
    assert "error" in r.json()


def test_ingest_generations_without_account_is_400_json(client, seeded):
    # Documented by-design 400 (qa report line 188) — sits in the
    # error-shape group rather than the 200 sweep.
    r = _bounded_get(client, "/api/ingest/generations")
    assert r.status_code == 400
    assert "error" in r.json()


def test_unknown_file_replication_quirk_is_pinned(client, seeded):
    """Documented qa observation (report line 220): replication for an
    unknown id returns 200 [] instead of the 404 the detail route gives.
    Pinned so a future change is a conscious decision, not an accident."""
    r = _bounded_get(client, "/api/files/999999/replication")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# 4. Route-order regression: /api/backends/usage must not be captured by
#    /api/backends/{name} (registration order matters in routes.py)
# ---------------------------------------------------------------------------

def test_backends_usage_not_shadowed_by_detail_route(client, seeded):
    r = _bounded_get(client, "/api/backends/usage")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"backends"}, body
    entry = next(b for b in body["backends"] if b["name"] == "test_local")
    for key in ("stored_bytes", "blobs", "ingest_24h_bytes",
                "free_space", "total_space", "connected", "type"):
        assert key in entry, f"usage shape missing {key!r}: {entry}"
    # The detail handler's shape carries "config" and would 404/shadow.
    assert "config" not in entry

    detail = _bounded_get(client, "/api/backends/test_local")
    assert detail.status_code == 200
    assert detail.json()["name"] == "test_local"
    assert "config" in detail.json()


# ---------------------------------------------------------------------------
# 5. SSE contract: each op stream emits >= 1 event and terminates cleanly
#    (start the op first, wait for the status endpoint, then drain)
# ---------------------------------------------------------------------------

def test_backup_scan_stream_emits_and_terminates(client, seeded, env):
    src = Path(env) / "scan_src"
    src.mkdir(exist_ok=True)
    (src / "tiny.txt").write_bytes(b"scan stream probe")

    start = client.post("/api/backup/scan", json={"path": str(src)})
    assert start.status_code == 200, start.text
    assert start.json()["status"] == "started"

    def scan_done():
        state = client.get("/api/backup/scan/status").json()
        return state["completed"] and not state["running"]

    assert _wait_for(scan_done), "backup scan never completed"
    events = _drain_sse_bounded(client, "/api/backup/scan/stream")
    last = _assert_sse_terminated(events)
    assert last["files_error"] == 0, last


def test_ingest_stream_emits_and_terminates_on_empty_drop_root(client, seeded, env):
    """Empty drop-root flavor (0 archives) — the with-archive flavor and
    the pause interplay live in tests/test_web_ingest.py.

    /api/ingest/status does not expose the worker's `completed` flag (its
    drop_root echoes the config, not the running scan), so completion
    cannot be pre-polled here; the bounded drain proves the stream itself
    terminates with the completed snapshot."""
    drop = Path(env) / "drop_empty"
    drop.mkdir(exist_ok=True)

    start = client.post("/api/ingest/scan", json={"drop_root": str(drop)})
    assert start.status_code == 200, start.text
    assert start.json()["status"] == "started"

    events = _drain_sse_bounded(client, "/api/ingest/stream")
    last = _assert_sse_terminated(events)
    # Stream frames carry the raw worker state (no status-handler shaping).
    assert last["archives_total"] == 0
    assert last["drop_root"] == str(drop)
    assert not last["errors"]


def test_restore_stream_emits_and_terminates(client, seeded):
    dest = Path(seeded["env"]) / "restore_out"
    start = client.post("/api/restore/all", json={
        "source_root": seeded["source_root"], "dest_path": str(dest)})
    assert start.status_code == 200, start.text
    assert start.json()["status"] == "started"

    def restore_done():
        state = client.get("/api/restore/status").json()
        return state["completed"] and not state["running"]

    assert _wait_for(restore_done), "restore never completed"
    events = _drain_sse_bounded(client, "/api/restore/stream")
    last = _assert_sse_terminated(events)
    assert last["errors"] == 0, last["error_list"]
    assert last["done"] == 1  # the single pipeline-uploaded file
    restored = dest / "seed" / "delta.txt"
    assert restored.exists()
    assert restored.read_bytes() == b"delta content 123"


def test_health_scan_stream_emits_and_terminates(client, seeded):
    """Empty-dataset flavor; the full bucket-level scan assertions live in
    tests/test_web_health.py (test_scan_stream_sse_terminates et al)."""
    start = client.post("/api/health/scan")
    assert start.status_code == 200, start.text
    assert start.json()["status"] == "started"

    def health_done():
        data = client.get("/api/health/data").json()
        return data["scan"]["completed"] and not data["scan_running"]

    assert _wait_for(health_done), "health scan never completed"
    events = _drain_sse_bounded(client, "/api/health/scan/stream")
    _assert_sse_terminated(events)


# ---------------------------------------------------------------------------
# 6. Concurrency smoke: 4 threads x 50 mixed GETs — zero 5xx, zero
#    exceptions escaping (qa 'concurrency smoke' check, bounded)
# ---------------------------------------------------------------------------

def test_concurrency_smoke_mixed_gets_no_5xx(app, seeded):
    paths = ["/api/files", "/api/backends", "/api/status"]
    per_thread = 50
    codes: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(thread_idx: int) -> None:
        probe = TestClient(app, raise_server_exceptions=False)
        local: list[int] = []
        try:
            for i in range(per_thread):
                r = probe.get(paths[(thread_idx + i) % len(paths)])
                local.append(r.status_code)
        except Exception as exc:  # noqa: BLE001 — anything escaping is a defect
            with lock:
                errors.append(f"thread {thread_idx}: {exc!r}")
        with lock:
            codes.extend(local)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    elapsed = time.monotonic() - started

    assert not any(t.is_alive() for t in threads), "smoke threads did not finish"
    assert not errors, f"exceptions escaped the client: {errors}"
    assert len(codes) == 4 * per_thread, f"missing responses: {len(codes)}"
    bad = sorted(c for c in codes if c >= 500)
    assert not bad, f"5xx responses escaped: {bad}"
    assert elapsed < 60, f"smoke took {elapsed:.1f}s"
    assert all(c == 200 for c in codes)  # these three routes must be clean 200s


# ---------------------------------------------------------------------------
# 7. Auth gate matrix summary (details in test_web_token_config.py)
# ---------------------------------------------------------------------------

def test_auth_gate_401_matrix_summary(env):
    """Every /api/* GET in the sweep returns 401 with the documented body
    when a token is configured and no bearer is sent; a valid bearer
    passes; non-API pages stay public by design (loopback binding is the
    documented mitigation)."""
    cfg_path = Path(env) / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + f"web:\n  token: {TOKEN}\n")
    from backupme.config import load_config
    armed = TestClient(create_app(load_config()), raise_server_exceptions=False)

    denials = []
    for path in AUTH_STATIC_PATHS:
        r = _bounded_get(armed, path)
        if r.status_code != 401 or r.json() != {"detail": "unauthorized"}:
            denials.append(f"GET {path} -> {r.status_code}: {r.text[:80]!r}")
    assert not denials, (
        f"{len(denials)} of {len(AUTH_STATIC_PATHS)} tokenless GETs were not "
        f"rejected with the documented body:\n" + "\n".join(denials))

    # A valid bearer passes on representative routes.
    headers = {"Authorization": f"Bearer {TOKEN}"}
    for path in ("/api/status", "/api/files", "/api/backends/usage",
                 "/api/health/detailed"):
        r = armed.get(path, headers=headers)
        assert r.status_code == 200, f"valid token rejected on {path}"

    # Guard scope: only /api/* is gated (qa auth matrix, 'pages public').
    assert armed.get("/").status_code == 200
    assert armed.get("/health").status_code == 200

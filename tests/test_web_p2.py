"""P2 defect-fix tests: D18 (blocking work off the event loop), D15
(/api/files hash search mode), D17 (content_text populated by the real
pipeline), and SSE pre-first-op termination.

Companion to tests/qa1_web_sweep.py — same isolated env/backends fixtures
and TestClient(raise_server_exceptions=False) black-box pattern. Everything
runs against scratch DBs only (never the live database).
"""
import concurrent.futures
import gc
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute


# ---------------------------------------------------------------------------
# Helpers / fixtures (same pattern as tests/qa1_web_sweep.py)
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


@pytest.fixture(autouse=True)
def _release_stray_db_locks():
    """TestClient portal threads / executor threads leave thread-local
    sqlite connections behind; gc them so the next test's fresh-DB init
    never hits a Windows file lock (same finalizer as the qa1 sweep)."""
    yield
    gc.collect()


@pytest.fixture()
def app(env, backends):
    from backupme.config import load_config
    return create_app(load_config())


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _upload(env, backends, rel: str, data: bytes) -> dict:
    from tests.helpers import backup_bytes
    return backup_bytes(Path(env), rel, data, backends)


def _file_row(rel_path: str):
    return execute(
        "SELECT id, sha256, content_text FROM files "
        "WHERE relative_path = ? AND deleted_at IS NULL", (rel_path,)
    ).fetchone()


# ---------------------------------------------------------------------------
# D18 — blocking work must run via asyncio.to_thread
# ---------------------------------------------------------------------------

def test_database_page_renders_correct_db_info(client, env, backends):
    result = _upload(env, backends, "seed/doc.txt", b"database page seed")
    assert "file_id" in result, result
    from backupme.core import database as db
    gen = db.get_generation()
    r = client.get("/database")
    assert r.status_code == 200, r.text[:300]
    assert "Database integrity verified" in r.text, \
        "db_info.integrity_status must be 'ok' on a healthy scratch DB"
    assert "Enabled" in r.text, "db_info.wal_mode must render the WAL badge"
    assert f'id="db-generation">{gen}</span>' in r.text, \
        "db_info.generation must render the real generation number"


def test_backend_verify_endpoint_still_runs(client, env, backends):
    """D18 regression guard: /api/backends/{name}/verify wraps
    run_integrity_check in to_thread — the endpoint contract is unchanged
    (run_integrity_check's 'status' key wins over the 'ok' literal)."""
    _upload(env, backends, "seed/verifyme.txt", b"verify me over the pipeline")
    r = client.post("/api/backends/test_local/verify", params={"count": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed", body
    assert body["checked"] >= 1, body
    assert body["failed"] == 0, body


def test_report_generate_contract_unchanged(client, seeded):
    """D18 regression guard for /api/report/generate (qa1 already pins the
    full shape; this pins the to_thread wrap did not move the contract)."""
    r = client.post("/api/report/generate", params={"count": 12})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] in ("completed", "failed"), body
    assert body["checked"] >= 1, body
    assert body["failed"] + body["checked"] >= 1, body


# ---------------------------------------------------------------------------
# D15 — /api/files hash search mode
# ---------------------------------------------------------------------------

def test_api_files_finds_by_exact_sha256(client, env, backends):
    res = _upload(env, backends, "hashfind/needle.txt", b"hash mode target")
    fid = res["file_id"]
    sha = _file_row("hashfind/needle.txt")["sha256"]
    r = client.get("/api/files", params={"q": sha})
    assert r.status_code == 200, r.text
    assert fid in [f["id"] for f in r.json()], r.json()


def test_api_files_finds_by_sha256_prefix(client, env, backends):
    _upload(env, backends, "hashfind/needle.txt", b"hash mode target")
    fid = _file_row("hashfind/needle.txt")["id"]
    sha = _file_row("hashfind/needle.txt")["sha256"]
    r = client.get("/api/files", params={"q": sha[:12]})
    assert r.status_code == 200
    assert fid in [f["id"] for f in r.json()], r.json()


def test_api_files_hash_mode_composes_with_backend_filter(client, env, backends):
    _upload(env, backends, "hashfind/filtered.txt", b"backend filter hash probe")
    fid = _file_row("hashfind/filtered.txt")["id"]
    sha = _file_row("hashfind/filtered.txt")["sha256"]
    r = client.get("/api/files", params={"q": sha, "backend": "test_local"})
    assert r.status_code == 200
    assert fid in [f["id"] for f in r.json()], r.json()
    r2 = client.get("/api/files", params={"q": sha, "backend": "no_such_backend"})
    assert r2.status_code == 200 and r2.json() == [], r2.json()


def test_api_files_short_hex_query_keeps_name_search(client, env, backends):
    """A hex string shorter than 8 chars must NOT switch to hash mode —
    'beef' stays a plain name search and finds beef.txt."""
    _upload(env, backends, "hexish/beef.txt", b"short hex probe")
    r = client.get("/api/files", params={"q": "beef"})
    assert r.status_code == 200
    assert any(f["relative_path"].endswith("beef.txt") for f in r.json()), r.json()


def test_api_files_non_hex_query_unchanged(client, env, backends):
    _upload(env, backends, "plain/marco.txt", b"plain search probe")
    r = client.get("/api/files", params={"q": "marco"})
    assert r.status_code == 200
    assert any(f["relative_path"].endswith("marco.txt") for f in r.json()), r.json()
    # punctuation-heavy queries must still never 500 (same contract as qa1)
    r2 = client.get("/api/files", params={"q": 'a"b((; drop --'})
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# D17 — content_text populated by the real pipeline
# ---------------------------------------------------------------------------

def test_content_search_finds_new_upload_via_api_and_page(client, env, backends):
    _upload(env, backends, "docs/note.txt", b"the zanzibar manifest lives here")
    # /api/search scope=content
    r = client.get("/api/search", params={"q": "zanzibar", "scope": "content"})
    assert r.status_code == 200, r.text
    hits = r.json()
    assert hits, "fresh pipeline upload must be content-searchable"
    assert all("zanzibar" in (f.get("content_text") or "") for f in hits), hits
    # /files page content mode
    r2 = client.get("/files", params={"q": "zanzibar manifest", "mode": "content"})
    assert r2.status_code == 200
    assert "note.txt" in r2.text, "files page content mode must surface the row"
    # plain FTS search also matches the indexed content token
    r3 = client.get("/api/files", params={"q": "zanzibar"})
    assert any(f["relative_path"].endswith("note.txt") for f in r3.json()), r3.json()


def test_text_upload_populates_content_text(env, backends):
    res = _upload(env, backends, "docs/tiny.txt", b"hello indexed world")
    assert "file_id" in res, res
    row = _file_row("docs/tiny.txt")
    assert row["content_text"] == "hello indexed world", row


def test_binary_upload_leaves_content_text_null(env, backends):
    res = _upload(env, backends, "bin/blobby.bin", b"\x00\xff\xfe binary \x80\x81")
    assert "file_id" in res, res
    row = _file_row("bin/blobby.bin")
    assert row["content_text"] is None, row


def test_oversized_text_upload_leaves_content_text_null(env, backends):
    data = b"hello world padding " * 4000  # 80 KB > the 64 KiB snippet cap
    assert len(data) > 65536
    res = _upload(env, backends, "big/huge.txt", data)
    assert "file_id" in res, res
    row = _file_row("big/huge.txt")
    assert row["content_text"] is None, row


def test_encrypted_upload_leaves_content_text_null(enc_env):
    from tests.helpers import backup_bytes, load_cfg
    from backupme.backends import get_all_backends
    res = backup_bytes(Path(enc_env), "enc/secret.txt",
                       b"plaintext zebra must not be indexed",
                       get_all_backends(load_cfg()))
    assert "file_id" in res, res
    row = _file_row("enc/secret.txt")
    assert row["content_text"] is None, \
        "encrypted backups must never leak plaintext into the index"


def test_dedup_donor_copies_content_text(env, backends):
    _upload(env, backends, "first/orig.txt", b"dedup donor payload qqq")
    res = _upload(env, backends, "second/copy.txt", b"dedup donor payload qqq")
    assert res.get("dedup") is True or res.get("status") == "ok", res
    row = _file_row("second/copy.txt")
    assert row["content_text"] == "dedup donor payload qqq", \
        "donor-dedup re-upload must copy the donor row's snippet"


def test_dedup_donor_without_content_leaves_null(env, backends):
    # binary donor (content_text NULL) -> the dedup copy must stay NULL too
    donor_payload = b"\xff\xd8\xff\xe0 binary donor"
    _upload(env, backends, "first/img.bin", donor_payload)
    res = _upload(env, backends, "second/img_copy.bin", donor_payload)
    assert res.get("status") == "ok", res
    row = _file_row("second/img_copy.bin")
    assert row["content_text"] is None, row


def test_changed_reupload_updates_content_text(env, backends):
    _upload(env, backends, "chg/story.txt", b"first version about apples")
    _upload(env, backends, "chg/story.txt", b"second version about bananas")
    row = _file_row("chg/story.txt")
    assert row["sha256"] == _sha("second version about bananas"), row
    assert row["content_text"] == "second version about bananas", \
        "rows are updated in place — the snippet must come from the NEW content"


def test_changed_reupload_to_binary_clears_content_text(env, backends):
    _upload(env, backends, "flip/mood.txt", b"clear text version alpha")
    _upload(env, backends, "flip/mood.txt", b"\xff\xfe\x00 binary now")
    row = _file_row("flip/mood.txt")
    assert row["content_text"] is None, \
        "a binary re-upload must clear the previous version's stale snippet"


def test_chunked_large_upload_has_no_content_text(env, backends):
    """A file larger than the bundle threshold goes down the chunked path:
    no donor, bytes never whole in memory -> content_text stays NULL."""
    from backupme.core.uploader import upload_bundle
    big = Path(env) / "chunked"
    big.mkdir(exist_ok=True)
    p = big / "large.bin"
    payload = b"chunk filler \x00\x01" * (2 * 1024 * 1024 // 16)  # ~2 MiB
    p.write_bytes(payload)
    chunk_cfg = {"chunking": {"min_size": "1MB", "chunk_size": "1MB"}}
    results = upload_bundle(
        [{"local_path": str(p), "source_root": str(env), "relative_path": "chunked/large.bin"}],
        backends, chunk_cfg, 1,
    )
    assert results and results[0].get("status") in ("ok", "partial"), results
    row = _file_row("chunked/large.bin")
    assert row["content_text"] is None, row


# ---------------------------------------------------------------------------
# SSE pre-first-op termination
# ---------------------------------------------------------------------------

FRESH_STREAMS = [
    "/api/restore/stream",
    "/api/ingest/stream",
    "/api/backup/scan/stream",
    "/api/backends/rebalance/stream",
    "/api/health/scan/stream",
]


@pytest.mark.parametrize("path", FRESH_STREAMS)
def test_fresh_app_stream_emits_one_snapshot_then_closes(client, path):
    """A fresh app's op streams must terminate after exactly ONE idle
    snapshot — scripted consumers waiting for EOF must not hang forever
    (pre-first-op termination defect)."""
    events = _drain_sse_bounded(client, path, timeout=15)
    assert len(events) == 1, f"{path}: expected exactly one snapshot, got {len(events)}"
    snap = json.loads(events[0][len("data: "):])
    assert snap["completed"] is False and snap["running"] is False, snap


def test_restore_stream_still_terminates_completed_after_op(client, env, backends):
    """The fix must not change post-op behavior: after a real restore runs,
    the stream ends on a completed frame (same contract as qa1)."""
    _upload(env, backends, "sse/delta.txt", b"delta content 123")
    start = client.post("/api/restore/all", json={
        "source_root": str(env), "dest_path": str(Path(env) / "p2_restore_out")})
    assert start.status_code == 200, start.text
    events = _drain_sse_bounded(client, "/api/restore/stream", timeout=30)
    assert events, "post-op stream must still emit and terminate"
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is True and last["running"] is False, last


# ---------------------------------------------------------------------------
# index.py FTS discipline (defects found by the content-backfill work: the
# NameError crash in rebuild_fts(), and stale FTS terms after a changed
# re-upload because the files row was updated before the FTS row was deleted)
# ---------------------------------------------------------------------------

def _fts_matches(term: str) -> list[int]:
    rows = execute(
        "SELECT rowid FROM files_fts WHERE files_fts MATCH ?", (f'"{term}"',)
    ).fetchall()
    return [r[0] for r in rows]


def test_changed_reupload_leaves_no_stale_fts_terms(env, backends):
    _upload(env, backends, "fts/story.txt", b"the zebra sleeps at noon")
    row1 = _file_row("fts/story.txt")
    assert row1["content_text"] == "the zebra sleeps at noon", row1
    assert _fts_matches("zebra") == [row1["id"]]

    _upload(env, backends, "fts/story.txt", b"the quokka hops at dawn")
    row2 = _file_row("fts/story.txt")
    assert row2["id"] == row1["id"], "rows are updated in place"

    # The external-content index is deleted BEFORE the files row changes, so
    # the OLD terms must be gone and only the NEW terms searchable.
    assert _fts_matches("zebra") == [], \
        "stale terms from the previous version survived the re-upload"
    assert _fts_matches("quokka") == [row1["id"]], row2


def test_rebuild_fts_runs_and_repairs_drift(env, backends):
    from backupme.core import index as index_mod

    _upload(env, backends, "drift/one.txt", b"unique alpha here")
    _upload(env, backends, "drift/two.txt", b"unique beta here")
    row = _file_row("drift/one.txt")
    assert row["id"] is not None

    # Simulate legacy drift: index carries a term the files row no longer has.
    execute("INSERT INTO files_fts(rowid, relative_path, content_text) VALUES (?, ?, ?)",
            (row["id"], "drift/one.txt", "ghostterm"))

    # rebuild_fts() used to raise NameError (executemany never imported),
    # which crashed 'backupme db rebuild' at its FTS step.
    indexed = index_mod.rebuild_fts()
    assert indexed >= 2, indexed
    assert _fts_matches("alpha") and _fts_matches("beta")
    assert _fts_matches("ghostterm") == [], "rebuild must clear drifted terms"


# ---------------------------------------------------------------------------
# Seed fixture shared by the D18 report-generate guard above
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded(env, backends):
    """Minimal seed: one real pipeline upload (same shape as qa1's)."""
    result = _upload(env, backends, "seed/delta.txt", b"delta content 123")
    assert "file_id" in result, f"pipeline upload failed: {result}"
    return {"file_id": result["file_id"]}

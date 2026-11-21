"""Regression tests for the security defects D26-D28 (2026-11-20 audit,
diagnostics/audit-2026-11-19/report_security.md SEC-1/2/6/7/9/10/11):

- D26: POST /api/config/import needs a literal-true confirm (raw YAML
  mapping or {"confirm": true, "config": {...}} envelope), keeps a
  timestamped config.yaml.bak-<ts> backup (last 5), and refuses a config
  with ZERO backends unless allow_empty: true. POST /api/encrypt/init
  answers 409 when backupme.key exists without confirm, and rotation
  preserves the old key as backupme.key.bak-<ts>.
- D27: restore/download/restore-generation destinations go through the
  _unsafe_dest policy (+ UNC rejection); /api/download/{id}/to-path and
  /api/ingest/restore-generation need confirm: true; /api/db/snapshots
  download/delete reject separators/'..' in the name and require the
  resolved path to sit in the server working directory.
- D28: state-changing requests with a cross-site Origin/Referer or
  Sec-Fetch-Site: cross-site are 403; same-origin and header-less
  (curl/CLI) callers are unaffected. With web.token configured, pages
  accept ?token= / X-BackupMe-Token / bm_token cookie (the API keeps the
  Bearer header only).
"""
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from tests.helpers import backup_bytes

TOKEN = "fix-sec-token"


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    app = create_app(load_config())
    return TestClient(app)


@pytest.fixture()
def seeded(env, backends):
    """One real uploaded file for download/restore round-trips."""
    result = backup_bytes(Path(env), "seed/delta.txt", b"delta content 123", backends)
    return {"file_id": result["file_id"], "root": str(env)}


def _token_client(env, token: str = TOKEN) -> TestClient:
    """App with web.token armed (config section appended like the CLI does)."""
    cfg_path = Path(env) / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + f"web:\n  token: {token}\n")
    from backupme.config import load_config
    return TestClient(create_app(load_config()))


def _wait_restore_done(client: TestClient, timeout_s: float = 15.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = client.get("/api/restore/status").json()
        if state.get("completed") and not state.get("running"):
            return state
        time.sleep(0.1)
    return client.get("/api/restore/status").json()


def _bak_files(name: str) -> list[Path]:
    return sorted(Path.cwd().glob(f"{name}.bak-*"))


# ---------------------------------------------------------------------------
# D26: POST /api/config/import
# ---------------------------------------------------------------------------

def test_config_import_without_confirm_rejected(client):
    body = "backends:\n  local:\n  - name: kept\n    path: store\n"
    r = client.post("/api/config/import", content=body.encode("utf-8"),
                    headers={"Content-Type": "application/x-yaml"})
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["message"]
    # Nothing was written: config.yaml still holds the original backend.
    assert "test_local" in Path("config.yaml").read_text()


def test_config_import_non_mapping_rejected(client):
    r = client.post("/api/config/import", content=b"just a string",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 400, r.text


def test_config_import_raw_mapping_with_confirm_writes_and_backs_up(client):
    original = Path("config.yaml").read_text()
    body = ("confirm: true\n"
            "backends:\n  local:\n  - name: imported_be\n    path: store\n")
    r = client.post("/api/config/import", content=body.encode("utf-8"),
                    headers={"Content-Type": "application/x-yaml"})
    assert r.status_code == 200, r.text
    written = Path("config.yaml").read_text()
    assert "imported_be" in written
    # The control key never lands in config.yaml.
    assert "confirm" not in written
    backups = _bak_files("config.yaml")
    assert len(backups) == 1, backups
    assert backups[0].read_text() == original


def test_config_import_envelope_with_confirm_writes(client):
    r = client.post("/api/config/import", json={
        "confirm": True,
        "config": {"backends": {"local": [{"name": "envelope_be", "path": "store"}]}},
    })
    assert r.status_code == 200, r.text
    assert "envelope_be" in Path("config.yaml").read_text()


def test_config_import_zero_backends_rejected(client):
    r = client.post("/api/config/import", content=b"confirm: true\n",
                    headers={"Content-Type": "application/x-yaml"})
    assert r.status_code == 400, r.text
    assert "allow_empty" in r.json()["message"]
    assert "test_local" in Path("config.yaml").read_text()


def test_config_import_zero_backends_envelope_rejected(client):
    r = client.post("/api/config/import", json={"confirm": True, "config": {}})
    assert r.status_code == 400, r.text


def test_config_import_zero_backends_with_allow_empty_writes(client):
    r = client.post("/api/config/import", json={
        "confirm": True, "allow_empty": True, "config": {}})
    assert r.status_code == 200, r.text
    written = Path("config.yaml").read_text()
    assert "confirm" not in written and "allow_empty" not in written


def test_config_import_backup_pruned_to_five(client):
    for i in range(5):
        bak = Path(f"config.yaml.bak-20200101T00000{i}000000Z")
        bak.write_text(f"old backup {i}")
        # Staggered mtimes so "keep newest 5" is deterministic.
        stamp = 1577836800 + i
        import os
        os.utime(bak, (stamp, stamp))
    body = ("confirm: true\n"
            "backends:\n  local:\n  - name: prune_be\n    path: store\n")
    r = client.post("/api/config/import", content=body.encode("utf-8"),
                    headers={"Content-Type": "application/x-yaml"})
    assert r.status_code == 200, r.text
    backups = _bak_files("config.yaml")
    assert len(backups) == 5, [p.name for p in backups]
    # The oldest seeded backup was pruned, the fresh one survived.
    assert not Path("config.yaml.bak-20200101T000000000000Z").exists()
    assert any("prune_be" not in p.read_text() for p in backups)


# ---------------------------------------------------------------------------
# D26: POST /api/encrypt/init
# ---------------------------------------------------------------------------

def test_encrypt_init_without_key_unchanged(client):
    assert not Path("backupme.key").exists()
    r = client.post("/api/encrypt/init")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert Path("backupme.key").exists()


def test_encrypt_init_existing_key_409_without_confirm(client):
    client.post("/api/encrypt/init")
    key_before = Path("backupme.key").read_bytes()
    r = client.post("/api/encrypt/init")
    assert r.status_code == 409, r.text
    assert "confirm" in r.json()["message"]
    # The key file was NOT touched.
    assert Path("backupme.key").read_bytes() == key_before


def test_encrypt_init_confirm_not_literal_true_409(client):
    client.post("/api/encrypt/init")
    for wrong in ("true", 1, "yes"):
        r = client.post("/api/encrypt/init", json={"confirm": wrong})
        assert r.status_code == 409, f"{wrong!r} -> {r.status_code}"


def test_encrypt_init_confirm_rotates_and_preserves_old_key_bak(client):
    client.post("/api/encrypt/init")
    old_key = Path("backupme.key").read_bytes()
    r = client.post("/api/encrypt/init", json={"confirm": True})
    assert r.status_code == 200, r.text
    new_key = Path("backupme.key").read_bytes()
    assert new_key != old_key
    backups = _bak_files("backupme.key")
    assert len(backups) == 1, backups
    assert backups[0].read_bytes() == old_key


# ---------------------------------------------------------------------------
# D27: destination guards on restore/download/restore-generation
# ---------------------------------------------------------------------------

def test_restore_all_traversal_dest_400(client):
    r = client.post("/api/restore/all", json={
        "source_root": "R", "dest_path": "../escape_rel_restore"})
    assert r.status_code == 400, r.text
    assert "unsafe destination" in r.json()["error"]
    assert not Path.cwd().joinpath("..", "escape_rel_restore").exists()


def test_restore_all_unc_dest_400(client):
    r = client.post("/api/restore/all", json={
        "source_root": "R", "dest_path": "\\\\127.0.0.1\\no_such_share\\x"})
    assert r.status_code == 400, r.text
    assert "UNC" in r.json()["error"]


def test_restore_all_absolute_dest_still_allowed(client, seeded):
    """The _unsafe_dest contract keeps absolute server paths legal (same as
    /api/archives/restore) — only traversal/UNC/bare roots are refused."""
    dest = Path(seeded["root"]) / "fixsec_restore_out"
    r = client.post("/api/restore/all", json={
        "source_root": str(seeded["root"]), "dest_path": str(dest)})
    assert r.status_code == 200, r.text
    state = _wait_restore_done(client)
    assert state["errors"] == 0, state["error_list"]
    restored = dest / "seed" / "delta.txt"
    assert restored.exists()
    assert restored.read_bytes() == b"delta content 123"


def test_restore_files_traversal_dest_400(client, seeded):
    r = client.post("/api/restore/files", json={
        "dest_path": "../escape_ids_restore", "file_ids": [seeded["file_id"]]})
    assert r.status_code == 400, r.text
    assert "unsafe destination" in r.json()["error"]


def test_download_to_path_requires_confirm(client, seeded):
    target = Path(seeded["root"]) / "fixsec_dl" / "out.txt"
    r = client.post(f"/api/download/{seeded['file_id']}/to-path",
                    json={"path": str(target)})
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["message"]
    assert not target.exists(), "no write may happen without confirm"


def test_download_to_path_traversal_dest_400(client, seeded):
    r = client.post(f"/api/download/{seeded['file_id']}/to-path",
                    json={"path": "../escape_to_path.bin", "confirm": True})
    assert r.status_code == 400, r.text
    assert "unsafe destination" in r.json()["message"]


def test_download_to_path_with_confirm_writes_file(client, seeded):
    target = Path(seeded["root"]) / "fixsec_dl2" / "out.txt"
    r = client.post(f"/api/download/{seeded['file_id']}/to-path",
                    json={"path": str(target), "confirm": True})
    assert r.status_code == 200, r.text
    assert target.read_bytes() == b"delta content 123"


def test_ingest_restore_generation_requires_confirm(client):
    r = client.post("/api/ingest/restore-generation", json={
        "account": "acct", "generation": 1, "out": "restore_out/acct"})
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["error"]


def test_ingest_restore_generation_out_traversal_400(client):
    r = client.post("/api/ingest/restore-generation", json={
        "account": "acct", "generation": 1, "out": "../evil_out",
        "confirm": True})
    assert r.status_code == 400, r.text
    assert "out:" in r.json()["error"]


def test_ingest_restore_generation_retar_traversal_400(client):
    r = client.post("/api/ingest/restore-generation", json={
        "account": "acct", "generation": 1, "out": "restore_out/acct",
        "retar": "../evil.tar", "confirm": True})
    assert r.status_code == 400, r.text
    assert "retar:" in r.json()["error"]


# ---------------------------------------------------------------------------
# D27: snapshot name traversal (download / delete)
# ---------------------------------------------------------------------------

def test_snapshot_download_traversal_400(client, env):
    # A real snapshot-shaped file one level ABOVE the server working
    # directory: the old p.name.startswith() guard served it via `..\`.
    parent_snap = Path(env).parent / "snapshot-parent.db.zst"
    parent_snap.write_bytes(b"parent snapshot bytes")
    try:
        evil = quote("..\\snapshot-parent.db.zst", safe="")
        r = client.get(f"/api/db/snapshots/{evil}/download")
        assert r.status_code == 400, r.text
        assert "bare file name" in r.json()["error"]
        # Encoded-traversal to a NON snapshot name must also be 400, not 404.
        evil2 = quote("..\\some-other-file.db", safe="")
        r2 = client.get(f"/api/db/snapshots/{evil2}/download")
        assert r2.status_code == 400, r2.text
    finally:
        parent_snap.unlink(missing_ok=True)


def test_snapshot_download_legit_name_still_works(client):
    snap = Path("snapshot-a.db.zst")
    snap.write_bytes(b"snapshot bytes")
    r = client.get("/api/db/snapshots/snapshot-a.db.zst/download")
    assert r.status_code == 200, r.text
    assert r.content == b"snapshot bytes"


def test_snapshot_delete_traversal_400_with_confirm(client, env):
    parent_snap = Path(env).parent / "snapshot-delme.db.zst"
    parent_snap.write_bytes(b"do not delete me")
    try:
        evil = quote("..\\snapshot-delme.db.zst", safe="")
        r = client.request("DELETE", f"/api/db/snapshots/{evil}",
                           json={"confirm": True})
        assert r.status_code == 400, r.text
        assert "bare file name" in r.json()["message"]
        assert parent_snap.exists(), "the parent file must survive"
    finally:
        parent_snap.unlink(missing_ok=True)


def test_snapshot_delete_without_confirm_still_400(client):
    snap = Path("snapshot-b.db.zst")
    snap.write_bytes(b"keep me")
    r = client.delete("/api/db/snapshots/snapshot-b.db.zst")
    assert r.status_code == 400, r.text
    assert snap.exists()


def test_snapshot_delete_legit_still_works(client):
    snap = Path("snapshot-c.db.zst")
    snap.write_bytes(b"delete me")
    r = client.request("DELETE", "/api/db/snapshots/snapshot-c.db.zst",
                       json={"confirm": True})
    assert r.status_code == 200, r.text
    assert not snap.exists()


# ---------------------------------------------------------------------------
# D28: CSRF origin check (token-less app)
# ---------------------------------------------------------------------------

def test_cross_origin_post_403(client):
    # Middleware runs before routing: a cross-site POST is 403 even for a
    # path that does not exist (and harmless endpoints stay untouched).
    r = client.post("/api/__definitely_missing__",
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403, r.text


def test_cross_origin_referer_post_403(client):
    r = client.post("/api/__definitely_missing__",
                    headers={"Referer": "http://evil.example/attack"})
    assert r.status_code == 403, r.text


def test_sec_fetch_site_cross_site_post_403(client):
    r = client.post("/api/__definitely_missing__",
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403, r.text


def test_same_origin_post_allowed(client):
    r = client.post("/api/db/checkpoint",
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


def test_no_origin_post_allowed(client):
    """curl / CLI / server-to-server callers send no Origin: unaffected."""
    r = client.post("/api/db/checkpoint")
    assert r.status_code == 200, r.text


def test_sec_fetch_site_same_origin_post_allowed(client):
    r = client.post("/api/db/checkpoint",
                    headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# D28: page gate when web.token is configured
# ---------------------------------------------------------------------------

def test_page_requires_token_when_configured(env):
    client = _token_client(env)
    r = client.get("/")
    assert r.status_code == 401, r.text
    wrong = client.get("/", params={"token": TOKEN + "-wrong"})
    assert wrong.status_code == 401
    ok = client.get("/", params={"token": TOKEN})
    assert ok.status_code == 200, ok.text
    set_cookie = ok.headers.get("set-cookie", "")
    low = set_cookie.lower()
    assert "bm_token=" in low
    assert "httponly" in low
    assert "samesite=strict" in low
    assert "path=/" in low


def test_page_cookie_login(env):
    client = _token_client(env)
    # No ?token= this time: the cookie from a previous login keeps the
    # browser session alive.
    r = client.get("/", cookies={"bm_token": TOKEN})
    assert r.status_code == 200, r.text
    wrong = client.get("/", cookies={"bm_token": TOKEN + "-wrong"})
    assert wrong.status_code == 401


def test_page_token_via_header_sets_cookie(env):
    client = _token_client(env)
    r = client.get("/", headers={"X-BackupMe-Token": TOKEN})
    assert r.status_code == 200, r.text
    assert "bm_token=" in r.headers.get("set-cookie", "").lower()


def test_api_keeps_bearer_only_when_token_configured(env):
    client = _token_client(env)
    # The API does NOT accept the query-param or cookie shapes.
    assert client.get("/api/status", params={"token": TOKEN}).status_code == 401
    assert client.get("/api/status", cookies={"bm_token": TOKEN}).status_code == 401
    ok = client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200, ok.text
    assert client.get(
        "/api/status", headers={"Authorization": f"Bearer {TOKEN}-x"}).status_code == 401
    lower = client.get(
        "/api/status", headers={"Authorization": f"bearer {TOKEN}"})
    assert lower.status_code == 401

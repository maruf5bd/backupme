"""D29 + D35 fixes: credential redaction/masking/roundtrip + web blocking
regressions and ungated operations.

D29 (P2) — credential leakage and redaction roundtrip (SEC-3/4/5):
- /api/config/export redaction is word-part aware: planted secrets under
  common credential key names are never served verbatim.
- /api/backends/{name} withholds every secret-shaped config field, not
  just password.
- re-importing a redacted export is refused per-field; config.yaml on disk
  is never touched.

D35 (P2) — web blocking regressions + ungated/unsafe operations
(WEB-13/17/21, SEC-14):
- /api/db/restore, /api/db/rebuild, /api/db/replicate and /report run
  their minutes-class work off the event loop (same pinning approach as
  tests/test_web_d18b.py).
- the web ingest worker takes the same cross-component .ingest.lock the
  daemon path uses and honors the staging budget.
- /api/backup/scan delete_source needs confirm: true.
- confirm gates: /api/convert?delete=true, file delete, config/scheduler
  writes.

Everything runs against the isolated env/backends fixtures (scratch DB,
never the live database). No destructive confirm:true is ever sent against
real data — the destructive positives run on scratch paths only.
"""
import asyncio
import concurrent.futures
import gc
import io
import json
import tarfile
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.core.database import execute


# ---------------------------------------------------------------------------
# Helpers / fixtures (same pattern as tests/test_web_d18b.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _release_stray_db_locks():
    """TestClient portal threads / executor threads leave thread-local
    sqlite connections behind; gc them so the next test's fresh-DB init
    never hits a Windows file lock (same finalizer as test_web_d18b)."""
    yield
    gc.collect()


@pytest.fixture()
def app(env, backends):
    from backupme.config import load_config
    return create_app(load_config())


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _wait_scan_done(client: TestClient, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        state = client.get("/api/backup/scan/status").json()
        if state.get("completed") and not state.get("running"):
            return state
        time.sleep(0.05)
    raise AssertionError(f"scan never finished: {state}")


def _drain_ingest_stream(client: TestClient, timeout: float = 20.0) -> dict:
    """Read the ingest SSE stream to its clean end and return the final
    snapshot (robust against the worker thread's startup window, unlike
    polling /api/ingest/status which can observe the pre-start idle)."""
    def _read_frame() -> str:
        with client.stream("GET", "/api/ingest/stream") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            frames = [e for e in "".join(resp.iter_text()).split("\n\n")
                      if e.strip()]
        assert frames, "stream closed without a single event"
        return frames[-1]

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_read_frame)
        last = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        pytest.fail("ingest SSE stream never terminated")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    assert last.startswith("data: "), f"malformed SSE frame: {last[:80]!r}"
    snapshot = json.loads(last[len("data: "):])
    assert snapshot["completed"] is True and snapshot["running"] is False, snapshot
    return snapshot


def _make_cpmove(env, name: str = "cpmove-acct.tar.gz") -> Path:
    """One minimal cpmove archive in <env>/drop/acct (same shape as
    tests/test_web_ingest.py)."""
    drop = Path(env) / "drop" / "acct"
    drop.mkdir(parents=True, exist_ok=True)
    arch = drop / name
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for member, data in [("homedir/index.php", b"<?php echo 1;"),
                             ("mysql/db.sql", b"INSERT INTO x VALUES (1);")]:
            info = tarfile.TarInfo(name=member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    arch.write_bytes(buf.getvalue())
    return arch


# ---------------------------------------------------------------------------
# D29-1: export redaction is word-part aware (SEC-3)
# ---------------------------------------------------------------------------

def test_is_secret_key_matches_documented_and_used_key_names():
    """Unit contract for the word-part matcher: the documented defect list,
    camelCase/dash/numbered variants, and every secret-shaped field the
    backend types in config.py actually use — while path/structure keys
    stay clear."""
    from backupme.web.routes.common import _is_secret_key
    secret_names = (
        # documented missing list (defect D29)
        "credential", "credentials", "passwd", "webhook", "webhook_url",
        # existing coverage kept
        "password", "PASSWORD", "secret", "client_secret", "token",
        "api_key", "apikey", "passphrase", "private_key",
        # variants the substring list missed
        "apiKey", "api-key", "api_key_v2", "token2", "refresh_token",
        "access_token", "auth_token", "privateKey", "PRIVATEKEY",
        "access_key", "secret_key", "pwd",
        # fields actually used by config.py backend/web types
        "password", "api_key", "refresh_token",
        # a bare "key" entry is credential-shaped
        "key",
    )
    for name in secret_names:
        assert _is_secret_key(name), name
    clear_names = (
        "author", "tokenizer", "tokenize", "key_file", "api_version",
        "name", "path", "priority", "quota", "host", "port", "username",
        "config_path", "timeout_s", "staging_dir", "bwlimit", "folder",
    )
    for name in clear_names:
        assert not _is_secret_key(name), name


PLANTED_SECRETS = {
    "password": "hunter2secret",
    "api_key": "sk-live-9letters",
    "refresh_token": "rt-planted-123",
    "credential": "cred-planted-value",
    "passwd": "pw-planted-value",
    "webhook": "https://hooks.example.com/tok-planted",
    "apiKey": "camel-planted-value",
    "api-key": "dash-planted-value",
    "access_key": "aws-planted-value",
    "token2": "numbered-planted-value",
    "private_key": "pk-planted-value",
}


def _config_with_planted_secrets(env) -> Path:
    cfg_path = Path(env) / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    entry = raw.setdefault("backends", {}).setdefault("local", [{}])[0]
    entry.update(PLANTED_SECRETS)
    # Non-secret keys that share words with secrets must stay visible.
    entry["key_file"] = "keys/operator.key"
    entry["api_version"] = "v2beta"
    entry["author"] = "notasecret"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return cfg_path


def test_export_never_serves_planted_secret_values(client, env):
    _config_with_planted_secrets(env)
    r = client.get("/api/config/export")
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("text/yaml")
    for value in PLANTED_SECRETS.values():
        assert value not in r.text, f"planted secret leaked: {value}"
    assert r.text.count("***REDACTED***") == len(PLANTED_SECRETS)


def test_export_keeps_non_secret_structure_intact(client, env):
    """Roundtrip usability: path/key_file/api_version/author values are not
    credentials and must survive the redaction untouched."""
    _config_with_planted_secrets(env)
    r = client.get("/api/config/export")
    assert r.status_code == 200, r.text[:200]
    assert "keys/operator.key" in r.text
    assert "v2beta" in r.text
    assert "notasecret" in r.text
    assert "test_local" in r.text


# ---------------------------------------------------------------------------
# D29-2: /api/backends/{name} withholds every secret-shaped field (SEC-4)
# ---------------------------------------------------------------------------

def test_backend_detail_hides_all_secret_fields(client, env):
    _config_with_planted_secrets(env)
    r = client.get("/api/backends/test_local")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    config_view = body["config"]
    # The detail route resolves the backend through the canonical field
    # list (_be_to_dict), so the secret-shaped fields among them —
    # password / api_key / refresh_token — must all be withheld now
    # (previously only password was).
    for name in ("password", "api_key", "refresh_token"):
        assert name not in config_view, \
            f"secret-shaped field {name!r} served in plaintext"
    # Non-secret structure stays usable (the edit dialog prefills from it).
    for name in ("name", "path", "priority", "key_file"):
        assert name in config_view, name


# ---------------------------------------------------------------------------
# D29-3: re-importing a redacted export is refused, nothing written (SEC-5)
# ---------------------------------------------------------------------------

def test_import_rejects_redacted_export_and_writes_nothing(client, env):
    cfg_path = _config_with_planted_secrets(env)
    before = cfg_path.read_bytes()
    export = client.get("/api/config/export").text
    assert "***REDACTED***" in export

    body = "confirm: true\n" + export
    r = client.post("/api/config/import", content=body.encode("utf-8"),
                    headers={"Content-Type": "application/x-yaml"})
    assert r.status_code == 400, r.text
    message = r.json()["message"]
    assert "REDACTED" in message
    # The offending keys are named so the operator can pass raw values.
    for key in ("password", "api_key", "refresh_token"):
        assert key in message, f"{key} not named in the refusal: {message}"
    # Nothing was written to disk — the marker never reaches config.yaml.
    assert cfg_path.read_bytes() == before
    on_disk = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["backends"]["local"][0]["password"] == "hunter2secret"


def test_import_with_raw_values_still_writes(client, env):
    """Positive control: the same YAML with real values (no marker) imports
    exactly like before D29."""
    body = ("confirm: true\n"
            "backends:\n"
            "  local:\n"
            "  - name: imported_be\n"
            "    path: store\n"
            "    password: real-secret\n")
    r = client.post("/api/config/import", content=body.encode("utf-8"),
                    headers={"Content-Type": "application/x-yaml"})
    assert r.status_code == 200, r.text
    written = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    assert written["backends"]["local"][0]["password"] == "real-secret"


# ---------------------------------------------------------------------------
# D35-1: minutes-class handlers run off the event loop (WEB-13 / WEB-17)
# ---------------------------------------------------------------------------

def _make_off_loop_probe(calls: dict):
    """Same pinning approach as tests/test_web_d18b.py: a to_thread
    executor thread has NO running event loop, so get_running_loop()
    raising RuntimeError there proves the blocking work left the loop."""
    def probe(*args, **kwargs):
        try:
            calls["loop"] = asyncio.get_running_loop()
        except RuntimeError:
            calls["loop"] = None
        calls["thread"] = threading.get_ident()
        return {"status": "ok"}
    return probe


def test_db_restore_runs_off_event_loop(client, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr("backupme.core.recover.recover_database",
                        _make_off_loop_probe(calls))
    r = client.post("/api/db/restore", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert calls.get("loop") is None, \
        "recover_database ran inline on the event loop (D35/WEB-13 regression)"
    assert calls.get("thread") != threading.get_ident(), calls


def test_db_rebuild_runs_off_event_loop(client, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr("backupme.core.recover.rebuild_from_manifests",
                        _make_off_loop_probe(calls))
    r = client.post("/api/db/rebuild", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert calls.get("loop") is None, \
        "rebuild_from_manifests ran inline on the event loop (D35/WEB-13)"
    assert calls.get("thread") != threading.get_ident(), calls


def test_db_replicate_runs_off_event_loop(client, monkeypatch):
    calls: dict = {}

    class _FakeReplicator:
        def __init__(self, config):
            self.config = config

        def create_and_upload_snapshot(self):
            return _make_off_loop_probe(calls)()

    monkeypatch.setattr("backupme.core.replicator.Replicator", _FakeReplicator)
    r = client.post("/api/db/replicate")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}
    assert calls.get("loop") is None, \
        "snapshot replicate ran inline on the event loop (D35/WEB-13 regression)"
    assert calls.get("thread") != threading.get_ident(), calls


def test_report_page_runs_off_event_loop(client, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr("backupme.core.verify.get_report",
                        _make_off_loop_probe(calls))
    r = client.get("/report")
    assert r.status_code == 200, r.text[:200]
    assert calls.get("loop") is None, \
        "verify.get_report ran inline on the event loop (D35/WEB-17 regression)"
    assert calls.get("thread") != threading.get_ident(), calls


# ---------------------------------------------------------------------------
# D35-2: the web ingest worker honors the cross-component lock + budget
# ---------------------------------------------------------------------------

def test_ingest_scan_blocked_by_daemon_lock(client, env):
    """The web worker must take the SAME exclusive .ingest.lock the daemon
    tick takes (core ingest_tree). A held lock defers the whole scan — it
    must not race the daemon into the same archive."""
    arch = _make_cpmove(env)
    staging = Path(env) / "staging"
    staging.mkdir(exist_ok=True)
    # Simulate the daemon holding the lock (fresh mtime: not stale).
    (staging / ".ingest.lock").write_text("999999")

    r = client.post("/api/ingest/scan", json={"drop_root": str(env / "drop")})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"

    snap = _drain_ingest_stream(client)
    assert any("another ingest scan" in e for e in snap.get("errors", [])), snap
    assert snap["archives_total"] == 0
    # Nothing ingested, nothing moved: the daemon's scan stays untouched.
    assert arch.exists(), "archive must stay in the drop root"
    n = execute("SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%'"
                ).fetchone()[0]
    assert n == 0


def test_ingest_scan_honors_staging_budget(client, env):
    """Per-archive staging-budget gate (same check core ingest_tree runs):
    over-budget archives are deferred, never staged."""
    arch = _make_cpmove(env)
    staging = Path(env) / "staging"
    staging.mkdir(exist_ok=True)
    (staging / "leftover.bin").write_bytes(b"x" * 64)
    # 64 bytes staged vs a ~0.03 byte budget -> over budget.
    cfg_path = Path(env) / "config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8")
                        + "cpanel_ingest:\n  staging_budget_gb: 0.00000003\n")
    from backupme.config import load_config
    app = create_app(load_config())
    with TestClient(app, raise_server_exceptions=False) as bounded_client:
        r = bounded_client.post("/api/ingest/scan",
                                json={"drop_root": str(env / "drop")})
        assert r.status_code == 200, r.text
        snap = _drain_ingest_stream(bounded_client)
    results = snap.get("results") or []
    deferred = [x for x in results if x.get("status") == "deferred"]
    assert deferred and all(x.get("reason") == "staging budget"
                            for x in deferred), snap
    assert snap["archives_done"] == snap["archives_total"] == 1
    # Over-budget archives are never staged or moved.
    assert arch.exists(), "deferred archive must stay in the drop root"


def test_ingest_scan_still_ingests_when_lock_is_free(client, env):
    """Positive control: with no lock holder and budget headroom the worker
    still ingests (the gate must not break the happy path)."""
    _make_cpmove(env)
    r = client.post("/api/ingest/scan", json={"drop_root": str(env / "drop")})
    assert r.status_code == 200, r.text
    snap = _drain_ingest_stream(client)
    assert not [e for e in snap.get("errors", []) if "another ingest" in e], snap
    n = execute("SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'"
                ).fetchone()[0]
    assert n == 2


# ---------------------------------------------------------------------------
# D35-3: /api/backup/scan delete_source needs confirm (SEC-14)
# ---------------------------------------------------------------------------

def test_scan_delete_source_requires_confirm(client, env, backends):
    src = Path(env) / "d35_scan_delete.txt"
    src.write_bytes(b"upload then delete me")

    r = client.post("/api/backup/scan",
                    json={"path": str(src), "delete_source": True})
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["error"]
    assert src.exists(), "no delete may happen without confirm"

    r2 = client.post("/api/backup/scan",
                     json={"path": str(src), "delete_source": True,
                           "confirm": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "started"
    _wait_scan_done(client)
    assert not src.exists(), "confirmed delete_source must remove the file"
    # The upload itself succeeded before the delete (the file is in the DB).
    n = execute("SELECT COUNT(*) FROM files WHERE relative_path = ?",
                ("d35_scan_delete.txt",)).fetchone()[0]
    assert n == 1


def test_scan_without_delete_source_stays_confirm_free(client, env, backends):
    """Backward compatibility: a plain scan (no delete_source) behaves
    exactly as before — no confirm required, no deletion."""
    src = Path(env) / "d35_scan_keep.txt"
    src.write_bytes(b"keep me")
    r = client.post("/api/backup/scan", json={"path": str(src)})
    assert r.status_code == 200, r.text
    _wait_scan_done(client)
    assert src.exists()


# ---------------------------------------------------------------------------
# D35-4: confirm gates on the remaining destructive endpoints
# ---------------------------------------------------------------------------

def test_convert_delete_requires_confirm(client):
    r = client.post("/api/convert/test_local", params={"delete": "true"})
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["message"]
    # A convert without delete stays confirm-free (backward compatible).
    r2 = client.post("/api/convert/no_such_backend")
    assert r2.status_code < 500, r2.text[:200]
    assert r2.json().get("status") == "error"


def test_file_delete_requires_confirm(client, env, backends):
    from tests.helpers import backup_bytes
    result = backup_bytes(Path(env), "d35/deleteme.txt", b"delete target",
                          backends)
    file_id = result["file_id"]

    r = client.delete(f"/api/files/{file_id}")
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["message"]
    alive = execute("SELECT deleted_at FROM files WHERE id = ?",
                    (file_id,)).fetchone()
    assert alive["deleted_at"] is None, "no delete may happen without confirm"

    r404 = client.request("DELETE", "/api/files/999999", json={"confirm": True})
    assert r404.status_code == 404

    r_ok = client.request("DELETE", f"/api/files/{file_id}", json={"confirm": True})
    assert r_ok.status_code == 200, r_ok.text
    row = execute("SELECT deleted_at FROM files WHERE id = ?",
                  (file_id,)).fetchone()
    assert row["deleted_at"] is not None


def test_config_save_requires_confirm(client):
    r = client.post("/api/config", json={"retention": {"keep_days": 45}})
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["message"]
    written = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    assert "retention" not in (written or {}), "nothing written without confirm"

    r2 = client.post("/api/config",
                     json={"confirm": True, "retention": {"keep_days": 45}})
    assert r2.status_code == 200, r2.text
    written = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    assert written["retention"]["keep_days"] == 45
    # The control key never lands in config.yaml.
    assert "confirm" not in written


def test_scheduler_config_save_requires_confirm(client):
    r = client.post("/api/scheduler/config", json={"interval": 1234})
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["message"]

    r2 = client.post("/api/scheduler/config",
                     json={"confirm": True, "interval": 1234})
    assert r2.status_code == 200, r2.text
    assert client.get("/api/scheduler/config").json()["interval"] == 1234
    written = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    assert "confirm" not in (written.get("scheduler") or {})


def test_config_source_add_delete_require_confirm(client):
    r = client.post("/api/config/sources", json={"source": "J:\\d35_new_source"})
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["message"]

    rd = client.delete("/api/config/sources",
                       params={"source": "J:\\d35_new_source"})
    assert rd.status_code == 400, rd.text
    assert "confirm" in rd.json()["message"]

    r_ok = client.post("/api/config/sources",
                       json={"source": "J:\\d35_new_source", "confirm": True})
    assert r_ok.status_code == 200, r_ok.text
    rd_ok = client.request("DELETE", "/api/config/sources",
                           params={"source": "J:\\d35_new_source"},
                           json={"confirm": True})
    assert rd_ok.status_code == 200, rd_ok.text
    written = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    assert all(b.get("source") != "J:\\d35_new_source"
               for b in (written.get("backups") or []))


# ---------------------------------------------------------------------------
# D35-5: honest offline fallback states for CDN charts/icons (WEB-21)
# ---------------------------------------------------------------------------

def test_chart_icon_offline_fallback_states_exist():
    """The CDN scripts stay (no vendoring this pass), but every page that
    embeds charts/icons must carry a visible failure state: the dashboard's
    per-chart fallback, gauge fallbacks on database/report, and the
    app-level icon notice."""
    web_root = Path(__file__).resolve().parents[1] / "src" / "backupme" / "web"
    base = (web_root / "templates" / "base.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net" in base and "unpkg.com" in base, \
        "CDN scripts must stay until vendoring is approved"

    app_js = (web_root / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "Icon library failed to load" in app_js
    assert "icon-fallback-note" in app_js

    dashboard = (web_root / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "the chart library failed to load" in dashboard.lower()

    for page in ("database.html", "report.html"):
        text = (web_root / "templates" / page).read_text(encoding="utf-8")
        assert "Chart library failed to load" in text, page

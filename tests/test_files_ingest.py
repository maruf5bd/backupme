"""process-files helper (serial download -> pipeline -> store back).

Covers the core wrapper, the CLI command, and the dashboard routes:
confirm gate, single-flight, live progress state, honest completion.
"""
import json
import time
from pathlib import Path

from click.testing import CliRunner

from backupme.cli import cli as cli_group
from backupme.core.database import commit, execute
from backupme.core.files_ingest import ingest_files
from tests.helpers import load_cfg


def _seed_backend_files(env, backends, count=3):
    """Put loose unindexed files directly on the scratch backend root."""
    root = Path(load_cfg().backends["local"][0].path)
    root.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(count):
        name = f"loose_{i}.txt"
        (root / name).write_bytes(f"loose file number {i} payload ".encode() * (10 + i))
        names.append(name)
    return root, names


def test_ingest_files_processes_and_registers(env, backends):
    cfg = load_cfg()
    root, names = _seed_backend_files(env, backends)

    seen = []

    def cb(done, skipped, errors, total):
        seen.append((done, skipped, errors, total))

    result = ingest_files("test_local", cfg, progress_cb=cb)
    assert result["status"] == "ok", result
    assert result["completed"] == len(names), result
    assert result["errors"] == 0, result
    assert len(seen) >= 1

    # files are now indexed under source_root = backend name
    rows = execute(
        "SELECT count(*) AS n FROM files WHERE source_root = 'test_local' AND deleted_at IS NULL"
    ).fetchone()
    assert rows["n"] == len(names)

    # rerun: everything already indexed -> skipped
    result2 = ingest_files("test_local", cfg)
    assert result2["status"] == "ok"
    assert result2["skipped"] >= len(names)
    assert result2["completed"] == 0

    # progress file was written
    prog = Path("files_ingest_progress.json")
    assert prog.exists()
    payload = json.loads(prog.read_text(encoding="utf-8"))
    assert payload["backend"] == "test_local"
    assert payload["completed"] is True


def test_ingest_delete_flag_removes_source_after_success(env, backends):
    cfg = load_cfg()
    root, names = _seed_backend_files(env, backends, count=2)
    result = ingest_files("test_local", cfg, delete=True)
    assert result["status"] == "ok", result
    remaining = [n for n in names if (root / n).is_file()]
    assert remaining == [], remaining


def test_cli_process_files(env, backends):
    _seed_backend_files(env, backends, count=2)
    runner = CliRunner()
    r = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "process-files", "test_local", "--json"],
    )
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"
    assert payload["completed"] == 2


def test_cli_process_files_unknown_backend(env):
    runner = CliRunner()
    r = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "process-files", "nope", "--json"],
    )
    assert r.exit_code == 1
    assert json.loads(r.stdout)["status"] == "error"


def test_web_process_files_flow(env, backends):
    from fastapi.testclient import TestClient
    from backupme.web.app import create_app

    _seed_backend_files(env, backends, count=2)
    app = create_app(load_cfg())
    client = TestClient(app)

    # confirm gate: literal true required
    r = client.post("/api/backends/test_local/process-files", json={"confirm": "yes"})
    assert r.status_code == 400

    # unknown backend 404
    r = client.post("/api/backends/ghost/process-files", json={"confirm": True})
    assert r.status_code == 404

    # start
    r = client.post("/api/backends/test_local/process-files",
                    json={"confirm": True, "prefix": "", "delete": False})
    assert r.status_code == 200
    assert r.json()["status"] == "started"

    # poll until the worker thread finishes
    for _ in range(100):
        s = client.get("/api/backends/test_local/process-files/status").json()
        if not s["running"] and s["completed"]:
            break
        time.sleep(0.1)
    assert s["completed"], s
    assert s["result"]["status"] == "ok", s
    assert s["result"]["completed"] == 2
    assert s["done"] == 2

    # while idle, a second start is allowed (the old job completed)
    r = client.post("/api/backends/test_local/process-files", json={"confirm": True})
    assert r.status_code == 200
    for _ in range(100):
        s2 = client.get("/api/backends/test_local/process-files/status").json()
        if not s2["running"] and s2["completed"]:
            break
        time.sleep(0.1)
    assert s2["result"]["completed"] == 0  # all files already indexed
    assert s2["result"]["skipped"] >= 2

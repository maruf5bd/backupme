"""Plan 06 tests: rebalance / onboard / drain across two local backends."""
import json
from pathlib import Path

import pytest

from backupme.core import rebalance as rb
from backupme.core.database import execute
from tests.helpers import backup_bytes


@pytest.fixture()
def two_backend_env(env):
    """env fixture plus a second local backend named 'second'."""
    from tests.helpers import add_backend
    add_backend(env, "second", env.parent / "storage2")
    return env


def _ingest_only_on_first(env):
    """Upload with the default (first) backend only."""
    return backup_bytes(env, "rb/a.txt", b"alpha " * 50, _first_backends(env))


def _first_backends(env):
    from backupme.config import load_config
    from backupme.backends import get_all_backends
    return get_all_backends(load_config())


def _cfg(env):
    from backupme.config import load_config
    return load_config()


def test_plan_rebalance_matches_antijoin(two_backend_env):
    r = _ingest_only_on_first(two_backend_env)
    plan = rb.plan_rebalance("second")
    # Anti-join reference: same predicate, direct SQL.
    expected = execute(
        """SELECT COUNT(*) FROM blobs b
           WHERE b.blob_id NOT IN (SELECT blob_id FROM blob_storage WHERE backend_name = 'second')
             AND b.blob_id NOT LIKE 'bundle/%/%/%/%'"""
    ).fetchone()[0]
    assert plan["blobs"] == expected
    assert plan["blobs"] >= 1  # at least the uploaded blob
    assert plan["bytes"] > 0


def test_rebalance_copies_all_blobs_and_records_rows(two_backend_env):
    _ingest_only_on_first(two_backend_env)
    before_second = execute(
        "SELECT COUNT(*) FROM blob_storage WHERE backend_name='second'").fetchone()[0]
    assert before_second == 0

    out = rb.rebalance_to("second", _first_backends(two_backend_env), _cfg(two_backend_env))
    assert out["status"] == "ok"
    assert out["copied"] >= 1
    after_second = execute(
        "SELECT COUNT(*) FROM blob_storage WHERE backend_name='second'").fetchone()[0]
    assert after_second >= out["copied"]

    # Nothing left to copy.
    plan = rb.plan_rebalance("second")
    assert plan["blobs"] == 0

    # Second run is a no-op.
    again = rb.rebalance_to("second", _first_backends(two_backend_env), _cfg(two_backend_env))
    assert again["copied"] == 0

    # Checkpoint file written.
    # Checkpoint file written, namespaced per target.
    assert Path("rebalance_checkpoint_second.json").exists()
    ck = json.loads(Path("rebalance_checkpoint_second.json").read_text())
    assert ck["completed"]


def test_rebalance_checkpoint_resume_skips_completed(two_backend_env, monkeypatch):
    _ingest_only_on_first(two_backend_env)

    calls = {"n": 0}
    from backupme.mirror import replicate_to_n as real_replicate

    def counting_replicate(blob_id, targets, config):
        calls["n"] += 1
        return real_replicate(blob_id, targets, config)

    monkeypatch.setattr("backupme.mirror.replicate_to_n", counting_replicate)
    out1 = rb.rebalance_to("second", _first_backends(two_backend_env), _cfg(two_backend_env))
    first_calls = calls["n"]
    assert out1["copied"] >= 1

    # Simulated resume after "crash": completed blobs must not re-copy.
    out2 = rb.rebalance_to("second", _first_backends(two_backend_env), _cfg(two_backend_env))
    assert out2["copied"] == 0
    assert calls["n"] == first_calls  # replicate not invoked again


def test_rebalance_limit(two_backend_env):
    env = two_backend_env
    # Three distinct top-level blobs so "limit respected" is distinguishable
    # from "nothing to copy".
    for i, marker in enumerate((b"one " * 50, b"two " * 50, b"three " * 50)):
        backup_bytes(env, f"rb/lim{i}.txt", marker, _first_backends(env))

    out = rb.rebalance_to("second", _first_backends(env), _cfg(env), limit=1)
    assert out["copied"] == 1
    assert rb.plan_rebalance("second")["blobs"] == 2

    out2 = rb.rebalance_to("second", _first_backends(env), _cfg(env))
    assert out2["copied"] == 2
    assert rb.plan_rebalance("second")["blobs"] == 0


def test_drain_dry_run_reports_without_copying(two_backend_env):
    _ingest_only_on_first(two_backend_env)
    out = rb.drain_backend("test_local", _first_backends(two_backend_env),
                           _cfg(two_backend_env), dry_run=True)
    assert out["status"] == "dry-run"
    assert out["blobs"] >= 1
    assert out["bytes_to_copy"] > 0
    # Nothing copied.
    n = execute("SELECT COUNT(*) FROM blob_storage WHERE backend_name='second'").fetchone()[0]
    assert n == 0


def test_drain_copies_off_and_reports_gc_eligible(two_backend_env):
    r = _ingest_only_on_first(two_backend_env)
    out = rb.drain_backend("test_local", _first_backends(two_backend_env),
                           _cfg(two_backend_env))
    assert out["status"] == "ok"
    assert out["copied"] >= 1
    assert out["remaining_only_here_blobs"] == 0
    assert out["runbook"]

    # Data still restorable (second backend now holds it).
    from tests.helpers import restore_bytes
    assert restore_bytes(r["file_id"], two_backend_env) == b"alpha " * 50

    # Every top-level blob on test_local also exists on 'second'
    # (member rows follow their parent bundle by design).
    only_here = execute(
        """SELECT COUNT(*) FROM blob_storage bs
           WHERE bs.backend_name = 'test_local'
             AND bs.blob_id NOT LIKE 'bundle/%/%/%/%'
             AND bs.blob_id NOT IN (SELECT blob_id FROM blob_storage WHERE backend_name = 'second')"""
    ).fetchone()[0]
    assert only_here == 0


def test_drain_unknown_name_with_other_backends_is_noop(two_backend_env):
    """Unknown name: no storage rows -> nothing to copy, no error."""
    _ingest_only_on_first(two_backend_env)
    out = rb.drain_backend("does_not_exist", _first_backends(two_backend_env),
                           _cfg(two_backend_env))
    assert out["status"] == "ok"
    assert out["copied"] == 0
    assert out["remaining_only_here_blobs"] == 0


def test_drain_sole_backend_errors(two_backend_env):
    """The only backend cannot be drained: there is nowhere to receive data."""
    out = rb.drain_backend("test_local", [b for b in _first_backends(two_backend_env)
                                          if b.name == "test_local"],
                           _cfg(two_backend_env))
    assert out["status"] == "error"
    assert "no other healthy backend" in out["error"]


def test_backend_usage_rows(two_backend_env):
    _ingest_only_on_first(two_backend_env)
    usage = {u["backend_name"]: u for u in rb.backend_usage()}
    assert "test_local" in usage
    assert usage["test_local"]["stored_bytes"] > 0
    assert usage["test_local"]["blobs"] >= 1


def test_backend_cli_onboard_rebalance_and_drain(two_backend_env):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    _ingest_only_on_first(two_backend_env)
    runner = CliRunner()

    onboard = runner.invoke(cli, ["--db", str(two_backend_env / "test.db"),
                                  "backend", "onboard", "second", "--rebalance", "--json"])
    assert onboard.exit_code == 0, onboard.output
    data = json.loads(onboard.output)
    assert data["connected"] is True
    assert data["rebalance"]["copied"] >= 1

    dry = runner.invoke(cli, ["--db", str(two_backend_env / "test.db"),
                              "backend", "rebalance", "second", "--dry-run", "--json"])
    assert dry.exit_code == 0
    assert json.loads(dry.output)["blobs"] == 0

    drain = runner.invoke(cli, ["--db", str(two_backend_env / "test.db"),
                                "backend", "drain", "test_local", "--json"])
    assert drain.exit_code == 0, drain.output
    ddata = json.loads(drain.output)
    assert ddata["remaining_only_here_blobs"] == 0

    listed = runner.invoke(cli, ["--db", str(two_backend_env / "test.db"),
                                 "backend", "list", "--json"])
    assert listed.exit_code == 0
    rows = json.loads(listed.output)["backends"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["second"]["stored_bytes"] > 0


def test_backend_web_usage_and_onboard_api(two_backend_env):
    from fastapi.testclient import TestClient
    from backupme.web.app import create_app
    from backupme.config import load_config
    _ingest_only_on_first(two_backend_env)
    client = TestClient(create_app(load_config()))

    usage = client.get("/api/backends/usage")
    assert usage.status_code == 200
    rows = {b["name"]: b for b in usage.json()["backends"]}
    assert rows["test_local"]["stored_bytes"] > 0

    onboard = client.post("/api/backends/onboard", json={"name": "second"})
    assert onboard.status_code == 200
    assert onboard.json()["connected"] is True

    reb = client.post("/api/backends/second/rebalance", json={})
    assert reb.status_code == 200
    import time
    for _ in range(50):
        s = client.get("/api/backends/usage").json()
        if any(b["name"] == "second" and b["stored_bytes"] > 0 for b in s["backends"]):
            break
        time.sleep(0.2)
    rows = {b["name"]: b for b in s["backends"]}
    assert rows["second"]["stored_bytes"] > 0

    unknown = client.post("/api/backends/nope/rebalance", json={})
    assert unknown.status_code == 404

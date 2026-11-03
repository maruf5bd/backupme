"""Plan 05: ops hardening — ingest-status summary, failure notification hook,
staging budget enforcement at scale, exit codes."""
import io
import json
import tarfile
from pathlib import Path

from click.testing import CliRunner

from backupme.cli.base import cli
from backupme.core import cpanel_ingest as ci
from backupme.core.database import execute
from tests.test_cpanel_ingest import _build_tar_bytes


def _drop(env, name="cpmove-acct.tar.gz", blob=None):
    d = env / "drop" / "acct"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(blob if blob is not None else _build_tar_bytes())
    return d / name


def _cfg(env):
    from backupme.config import load_config
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(env / "drop")
    cfg.cpanel_ingest.staging_dir = str(env / "staging")
    return cfg


def test_ingest_status_summary_json(env, backends):
    _drop(env)
    ci.ingest_tree(str(env / "drop"), backends, _cfg(env))
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["accounts"] and data["accounts"][0]["source_root"] == "cpanel/acct"
    assert data["accounts"][0]["files"] == 3
    assert data["generations"] and data["generations"][0]["members"] == 3
    assert data["dedup"]["logical_bytes"] > 0
    assert data["dedup"]["unique_bytes"] > 0
    assert data["dedup"]["ratio"] >= 1.0
    assert data["storage"]["sqlite_size"] > 0
    assert isinstance(data["storage"]["parquet_partitions"], list)


def test_ingest_status_human(env, backends):
    _drop(env)
    ci.ingest_tree(str(env / "drop"), backends, _cfg(env))
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-status"])
    assert result.exit_code == 0, result.output
    assert "cpanel/acct" in result.output
    assert "dedup" in result.output
    assert "storage" in result.output


def test_failure_notification_hook_fires(env, backends):
    _drop(env, "cpmove-bad.tar.gz", blob=b"NOT A TAR FILE")
    events = []
    ci.register_failure_hook(lambda details: events.append(details))
    out = ci.ingest_tree(str(env / "drop"), backends, _cfg(env))
    assert out["status"] == "partial"
    assert len(events) == 1
    assert events[0]["archive"].endswith("cpmove-bad.tar.gz")
    assert events[0]["account"] == "acct"
    assert events[0]["error"]
    # Failure is also in op_log (the existing notifications path).
    row = execute("SELECT status FROM op_log WHERE op_type='cpanel_ingest'").fetchone()
    assert row[0] == "failed"
    ci._FAILURE_HOOKS.clear()


def test_staging_budget_defers_archives(env, backends):
    _drop(env)
    cfg = _cfg(env)
    # Staging already holds data beyond a tiny budget → archives deferred.
    staging = env / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "blob.bin").write_bytes(b"x" * 8192)
    cfg.cpanel_ingest.staging_budget_gb = 0.000001  # ~1 KB
    out = ci.ingest_tree(str(env / "drop"), backends, cfg)
    assert out["status"] == "partial"  # deferred counts as not-ok
    assert out["results"][0]["status"] == "deferred"
    assert out["results"][0]["reason"] == "staging budget"
    # Nothing was ingested.
    n = execute("SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%'").fetchone()[0]
    assert n == 0


def test_ingest_exit_codes(env, backends):
    # 0 on idle, 1 on partial.
    drop = env / "drop"
    drop.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    ok = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel", str(drop)])
    assert ok.exit_code == 0

    _drop(env, "cpmove-bad.tar.gz", blob=b"GARBAGE NOT TAR")
    partial = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel", str(drop)])
    assert partial.exit_code == 1
    ci._FAILURE_HOOKS.clear()


def test_ingest_dry_run_json(env, backends):
    _drop(env)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(env / "drop"), "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "dry-run"
    assert data["archives"] == 1
    entry = data["entries"][0]
    assert entry["account"] == "acct"
    assert entry["members"] == 3
    assert entry["bytes"] > 0
    # Nothing was ingested.
    n = execute("SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%'").fetchone()[0]
    assert n == 0


def test_ingest_progress_renders(env, backends):
    _drop(env)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(env / "drop"), "--progress"])
    assert result.exit_code == 0, result.output
    assert "indexed" in result.output

import json
import os
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli.base import cli
from backupme.cli.ingest_cmd import _human_ingest


def _make_cpmove(drop_root: Path) -> Path:
    drop_root.mkdir(parents=True, exist_ok=True)
    account_dir = drop_root / "acct"
    account_dir.mkdir(parents=True, exist_ok=True)
    arch = account_dir / "cpmove-acct.tar.gz"
    with tarfile.open(arch, "w:gz") as tar:
        for name, data in [
            ("homedir/index.php", b"<?php echo 1;"),
            ("homedir/config.php", b"<?php 1234567890"),
            ("mysql/db.sql", b"INSERT INTO x VALUES (1);"),
        ]:
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            tar.addfile(ti, __import__("io").BytesIO(data))
    return arch


def test_ingest_cpanel_empty_drop_root(env, backends):
    drop = env / "drop"
    drop.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(drop)])
    assert result.exit_code == 0, result.output
    assert "idle" in result.output.lower()


def test_ingest_cpanel_json_empty(env, backends):
    drop = env / "drop"
    drop.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(drop), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "idle"
    assert data["archives"] == 0


@pytest.mark.parametrize("as_json", [False, True])
def test_ingest_cpanel_real_archive(env, backends, as_json):
    drop = env / "drop"
    _make_cpmove(drop)
    args = ["--db", str(env / "test.db"), "ingest-cpanel", str(drop)]
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output

    from backupme.core.database import execute
    rows = execute("SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'").fetchone()
    assert rows[0] == 3

    if as_json:
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["archives"] == 1
        assert "archives" in data
    else:
        assert "indexed" in result.output.lower() or "ok" in result.output.lower()


def test_ingest_cpanel_redundancy_flag(env, backends):
    drop = env / "drop"
    _make_cpmove(drop)
    # Redundancy needs somewhere to replicate TO: a single backend can never
    # satisfy --redundancy 2.
    from tests.helpers import add_backend
    add_backend(env, "second", env.parent / "storage2")
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(drop), "--redundancy", "2"])
    assert result.exit_code == 0, result.output
    # The redundancy flag must produce real replica rows, not just exit 0.
    from backupme.core.database import execute
    blob_ids = [r[0] for r in execute(
        "SELECT DISTINCT blob_id FROM file_chunks fc "
        "JOIN files f ON f.id = fc.file_id WHERE f.source_root='cpanel/acct'")]
    assert blob_ids
    for bid in blob_ids:
        # Member blobs are byte ranges inside their parent bundle; the parent
        # carries the redundancy.
        target = bid
        if bid.startswith("bundle/") and bid.count("/") >= 4:
            target = bid.rsplit("/", 1)[0]
        n = execute(
            "SELECT COUNT(*) FROM blob_storage WHERE blob_id = ?", (target,)).fetchone()[0]
        assert n >= 2, f"blob {target} has {n} replicas, expected >= 2"


def test_restore_cpanel_missing(env, backends):
    """Unknown account/generation is an error: exit 1 with a clear message
    (QA M5 — scripts must be able to detect a failed restore)."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "restore-cpanel",
                                 "nope", "1"])
    assert "Traceback" not in result.output
    assert result.exit_code == 1
    assert "no ingested generation 1 for account 'nope'" in result.output


def test_ingest_status(env, backends):
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-status"])
    assert result.exit_code == 0, result.output


def test_ingest_cpanel_invalid_drop_root(env, backends):
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(env / "does-not-exist")])
    assert result.exit_code == 2


def test_ingest_cpanel_watch_loops_until_interrupt(env, backends, monkeypatch):
    """--watch re-scans on the interval and exits 0 with 'stopped' on Ctrl+C."""
    from backupme.core import cpanel_ingest as ci_mod

    drop = env / "drop"
    drop.mkdir(parents=True, exist_ok=True)
    calls = {"n": 0}

    def fake_tree(*a, **kw):
        calls["n"] += 1
        return {"status": "ok", "archives": 0, "results": [], "files_ignored": 0}

    monkeypatch.setattr(ci_mod, "ingest_tree", fake_tree)

    def boom(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("backupme.cli.ingest_cmd.time.sleep", boom)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(drop), "--watch", "--interval", "1"])
    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert calls["n"] == 1  # one pass ran, then the interrupt ended the loop


def test_list_json(env, backends):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "list", "--json"])
    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    assert "files" in data
    assert "count" in data


def test_stats_json(env, backends):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "stats", "--json"])
    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    assert "total_files" in data
    assert "generation" in data
    assert "ratio" in data


def test_db_check_json(env, backends):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "db", "check", "--json"])
    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    assert "integrity" in data


def test_backends_json(env, backends):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "backends", "--json"])
    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    assert "backends" in data


def test_backend_list_json(env, backends):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "backend", "list", "--json"])
    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    assert "backends" in data


def test_list_source_root_filter(env, backends):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    drop = env / "drop"
    drop.mkdir()
    import tarfile, io
    arch = drop / "acct" / "cpmove-acct.tar.gz"
    arch.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(arch, "w:gz") as tar:
        for name, data in [("homedir/index.php", b"<?php echo 1;")]:
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    runner = CliRunner()
    runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel", str(drop)])
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "list", "--source-root", "cpanel/acct", "--json"])
    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    assert all(f["source_root"] == "cpanel/acct" for f in data["files"])

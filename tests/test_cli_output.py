import json
import os
import tarfile
from pathlib import Path

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


def test_ingest_cpanel_real_archive(env, backends):
    drop = env / "drop"
    _make_cpmove(drop)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(drop)])
    assert result.exit_code == 0, result.output
    assert "indexed" in result.output.lower() or "ok" in result.output.lower()

    from backupme.core.database import execute
    rows = execute("SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'").fetchone()
    assert rows[0] == 3


def test_ingest_cpanel_json_real(env, backends):
    drop = env / "drop"
    _make_cpmove(drop)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(drop), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "ok"
    assert data["archives"] == 1
    assert "archives" in data


def test_ingest_cpanel_redundancy_flag(env, backends):
    drop = env / "drop"
    _make_cpmove(drop)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(drop), "--redundancy", "2"])
    assert result.exit_code == 0, result.output


def test_restore_cpanel_missing(env, backends):
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "restore-cpanel",
                                 "nope", "1"])
    # either nonzero or ok with 0 members; must not crash with traceback
    assert "Traceback" not in result.output
    assert result.exit_code in (0, 1)


def test_ingest_status(env, backends):
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-status"])
    assert result.exit_code == 0, result.output


def test_ingest_cpanel_invalid_drop_root(env, backends):
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel",
                                 str(env / "does-not-exist")])
    assert result.exit_code == 2


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

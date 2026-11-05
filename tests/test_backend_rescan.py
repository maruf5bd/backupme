"""Backend rescan (backlog B13 CLI half): walk-vs-DB diff incl. size mismatches."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli
from backupme.core.backend_scan import rescan_backend
from backupme.core.database import execute, commit


def _sha(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()


def _insert_row(backend_name: str, path: str, size: int):
    # FK enforcement is ON: the storage row needs its parent blobs row first
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
            (_sha(path), size))
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
        "VALUES (?, ?, ?, ?, ?)",
        (_sha(path), backend_name, path, size, _sha("stored-" + path)),
    )
    commit()


@pytest.fixture()
def local_backend(backends):
    bk = backends[0]
    base = Path(bk.base_path)
    base.mkdir(parents=True, exist_ok=True)
    (base / "objects" / "aa").mkdir(parents=True, exist_ok=True)
    (base / "objects" / "bb").mkdir(parents=True, exist_ok=True)
    (base / "objects" / "aa" / "ok.bin").write_bytes(b"x" * 11)
    (base / "objects" / "bb" / "extra.bin").write_bytes(b"y" * 7)
    return bk


def test_rescan_reports_missing_extra_and_mismatch(local_backend):
    bk = local_backend
    _insert_row(bk.name, "objects/aa/ok.bin", 11)          # present, size matches
    _insert_row(bk.name, "objects/cc/lost.bin", 5)         # in DB, not on box
    # bundle-member rows are excluded from the path diff
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, 11, 1)",
            (_sha("mem"),))
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
        "VALUES (?, ?, ?, ?, ?)",
        (_sha("mem"), bk.name, "objects/aa/ok.bin@0:11", 11, _sha("mem")),
    )
    commit()

    result = rescan_backend(bk)
    assert result["backend"] == bk.name
    assert result["missing_on_box"] == ["objects/cc/lost.bin"]
    assert result["extra_on_box"] == ["objects/bb/extra.bin"]
    assert result["size_mismatch"] == []
    assert result["missing_bytes"] == 5
    # LocalBackend does not track walk_errors yet (falsy None is acceptable)
    assert not result["walk_errors"]
    # member row must not appear in db path counts or diffs
    assert "objects/aa/ok.bin@0:11" not in result["extra_on_box"]

    # corrupt/changed file: same path, different size -> size mismatch
    (Path(bk.base_path) / "objects" / "aa" / "ok.bin").write_bytes(b"z" * 12)
    result = rescan_backend(bk)
    assert len(result["size_mismatch"]) == 1
    assert result["size_mismatch"][0]["path"] == "objects/aa/ok.bin"
    assert result["size_mismatch"][0]["db_size"] == 11
    assert result["size_mismatch"][0]["box_size"] == 12


def test_rescan_cli_json_and_exit_codes(runner, env, local_backend):
    bk = local_backend
    db_arg = ["--db", str(env / "test.db")]
    runner = CliRunner()

    # DB knows about ok.bin; extra.bin is on box only -> exit 0
    _insert_row(bk.name, "objects/aa/ok.bin", 11)
    result = runner.invoke(cli, db_arg + ["backend", "rescan", bk.name, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output[result.output.index("{"):])
    assert data["extra_on_box"] == ["objects/bb/extra.bin"]
    assert data["missing_on_box"] == []

    # DB expects a file that is not on the box -> exit 1
    _insert_row(bk.name, "objects/cc/lost.bin", 5)
    result = runner.invoke(cli, db_arg + ["backend", "rescan", bk.name, "--json"])
    assert result.exit_code == 1, result.output
    data = json.loads(result.output[result.output.index("{"):])
    assert data["missing_on_box"] == ["objects/cc/lost.bin"]


@pytest.fixture()
def runner():
    return CliRunner()

"""CLI tests for `backupme health` and `backupme verify` (backlog B10)."""
import json

import pytest
from click.testing import CliRunner

from backupme.cli import cli


@pytest.fixture()
def runner():
    return CliRunner()


def test_health_json_shape(runner, env):
    result = runner.invoke(cli, ["health", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output[result.output.index("{"):])
    s = data["summary"]
    for key in ("live_files", "no_chunks_files", "missing_blob_files", "missing_blobs",
                "backend_blobs", "same_name_groups", "same_name_files", "purge_candidates"):
        assert key in s, f"summary missing '{key}'"


def test_health_human_and_purge_listing(runner, env):
    result = runner.invoke(cli, ["health", "--purge-candidates", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "live files" in result.output
    assert "purge candidates" in result.output


def test_verify_empty_db_completes(runner, env):
    result = runner.invoke(cli, ["verify", "--count", "3"])
    assert result.exit_code == 0, result.output
    assert "integrity check" in result.output
    assert "checked=0" in result.output


def test_verify_unknown_file_id_exits_1(runner, env):
    result = runner.invoke(cli, ["verify", "--file", "999999"])
    assert result.exit_code == 1, result.output
    assert "ERROR" in result.output

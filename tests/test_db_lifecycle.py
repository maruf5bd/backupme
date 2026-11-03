"""Tests for Plan 02 DB lifecycle: cold export to Zstd Parquet, hard purge,
DuckDB history queries, vacuum and op_log pruning."""
import json
from pathlib import Path

import pytest

from backupme.core import db_lifecycle as lc
from backupme.core.database import execute, commit
from tests.helpers import backup_bytes


def _ingest_two_files(env, backends):
    r1 = backup_bytes(env, "cold/a.txt", b"alpha content", backends)
    r2 = backup_bytes(env, "cold/b.txt", b"beta content " * 10, backends)
    return r1, r2


def _soft_delete_all_past_grace():
    """Mark every live file soft-deleted far in the past."""
    execute("UPDATE files SET deleted_at = datetime('now', '-60 days') WHERE deleted_at IS NULL")
    commit()


def test_config_defaults_and_yaml_override(env):
    from backupme.config import load_config
    cfg = load_config()
    assert cfg.db_lifecycle.purge_grace_days == 14
    assert cfg.db_lifecycle.cold_export_after_days == 30
    assert cfg.db_lifecycle.parquet_dir == "parquet"
    assert cfg.db_lifecycle.parquet_compression == "zstd"
    assert cfg.db_lifecycle.parquet_compression_level == 3

    cfg_path = env / "override.yaml"
    cfg_path.write_text(
        "db_lifecycle:\n"
        "  purge_grace_days: 3\n"
        "  cold_export_after_days: 7\n"
        "  parquet_dir: cold_store\n"
        "  parquet_compression: zstd\n"
        "  parquet_compression_level: 6\n"
    )
    over = load_config(str(cfg_path))
    assert over.db_lifecycle.purge_grace_days == 3
    assert over.db_lifecycle.parquet_dir == "cold_store"
    assert over.db_lifecycle.parquet_compression_level == 6


def test_cold_export_writes_zstd_parquet_and_watermark(env, backends):
    _ingest_two_files(env, backends)
    result = lc.cold_export_to_parquet()
    assert result["status"] == "ok"
    assert result["rows"] >= 2
    pdir = Path(result["partition"]).parent if result["partition"] else None
    partition = env / "parquet" / result["partition"]
    assert partition.exists()

    import pyarrow.parquet as pq
    meta = pq.read_metadata(str(partition))
    codecs = {meta.row_group(i).column(j).compression
              for i in range(meta.num_row_groups)
              for j in range(meta.num_columns)}
    assert "ZSTD" in codecs

    manifest = json.loads((env / "parquet" / "_manifest.json").read_text())
    assert manifest["watermark"] >= result["watermark"] > 0
    assert result["partition"] in manifest["partitions"]

    # Idempotent: second export with no new rows is a no-op.
    again = lc.cold_export_to_parquet()
    assert again["rows"] == 0
    assert again["watermark"] == result["watermark"]


def test_cold_export_appends_new_rows_to_same_partition(env, backends):
    _ingest_two_files(env, backends)
    first = lc.cold_export_to_parquet()
    r3 = backup_bytes(env, "cold/c.txt", b"gamma content", backends)
    second = lc.cold_export_to_parquet()
    assert second["rows"] >= 1
    assert second["partition"] == first["partition"]
    assert second["total_rows"] > first["total_rows"]
    # Watermark strictly advanced past the new row id.
    assert second["watermark"] >= r3["file_id"]


def test_purge_only_removes_exported_past_grace_rows(env, backends):
    r1, r2 = _ingest_two_files(env, backends)
    # Nothing exported yet: purge must not remove anything.
    _soft_delete_all_past_grace()
    before = lc.hard_purge_versions(grace_days=14)
    assert before["purged"] == 0

    # Export, then purge: exported rows go away.
    export = lc.cold_export_to_parquet()
    assert export["status"] == "ok"
    ids_before = execute("SELECT COUNT(*) FROM files").fetchone()[0]
    purged = lc.hard_purge_versions(grace_days=14)
    assert purged["purged"] == ids_before
    assert execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    # Chunks and blob refs dropped too.
    assert execute("SELECT COUNT(*) FROM file_chunks").fetchone()[0] == 0
    orphans = execute("SELECT COUNT(*) FROM blobs WHERE ref_count > 0").fetchone()[0]
    assert orphans == 0


def test_purge_keeps_rows_inside_grace(env, backends):
    _ingest_two_files(env, backends)
    lc.cold_export_to_parquet()
    # Soft-delete with a recent timestamp (inside 14-day grace).
    execute("UPDATE files SET deleted_at = datetime('now', '-2 days') WHERE deleted_at IS NULL")
    commit()
    result = lc.hard_purge_versions(grace_days=14)
    assert result["purged"] == 0
    assert execute("SELECT COUNT(*) FROM files").fetchone()[0] >= 2


def test_duckdb_history_query_resolves_members(env, backends):
    pytest.importorskip("duckdb")
    r1, _ = _ingest_two_files(env, backends)
    export = lc.cold_export_to_parquet()
    assert export["status"] == "ok"

    rows = lc.query_history_duckdb(
        "SELECT relative_path, source_root FROM files WHERE relative_path LIKE '%a.txt'"
    )
    assert rows and "status" not in rows[0]
    assert any(r["relative_path"].endswith("a.txt") for r in rows)

    empty = lc.query_history_duckdb("SELECT COUNT(*) AS n FROM files WHERE id < 0")
    assert empty == [{"n": 0}]


def test_duckdb_history_error_returns_marker(env, backends):
    pytest.importorskip("duckdb")
    rows = lc.query_history_duckdb("SELECT * FROM files WHERE (")
    assert len(rows) == 1
    assert rows[0].get("status") == "error"


def test_prune_oplog_archives_to_jsonl(env, backends):
    from backupme.core import op_log as oplog_mod
    op = oplog_mod.append("test_op", {"k": "v"})
    oplog_mod.mark_committed(op["id"])
    # Age the row past the prune window.
    execute("UPDATE op_log SET completed_at = datetime('now', '-40 days') WHERE id = ?", (op["id"],))
    commit()

    result = lc.prune_oplog(days=30)
    assert result["pruned"] == 1
    out = Path(result["file"])
    assert out.exists()
    lines = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    assert any(r["id"] == op["id"] for r in lines)
    assert execute("SELECT COUNT(*) FROM op_log WHERE id = ?", (op["id"],)).fetchone()[0] == 0


def test_prune_oplog_noop_when_nothing_old(env, backends):
    from backupme.core import op_log as oplog_mod
    op = oplog_mod.append("fresh_op", {"k": "v"})
    oplog_mod.mark_committed(op["id"])
    result = lc.prune_oplog(days=30)
    assert result["pruned"] == 0


def test_vacuum_runs(env):
    result = lc.vacuum_if_needed()
    assert result["vacuumed"] is True


def test_cli_cold_export_and_history(env, backends):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    _ingest_two_files(env, backends)
    runner = CliRunner()

    dry = runner.invoke(cli, ["--db", str(env / "test.db"), "db", "cold-export", "--dry-run", "--json"])
    assert dry.exit_code == 0, dry.output
    data = json.loads(dry.output)
    assert data["status"] == "dry-run"
    assert data["eligible_rows"] >= 2

    real = runner.invoke(cli, ["--db", str(env / "test.db"), "db", "cold-export", "--json"])
    assert real.exit_code == 0, real.output
    data = json.loads(real.output)
    assert data["status"] == "ok"

    hist = runner.invoke(cli, ["--db", str(env / "test.db"), "db", "history",
                               "SELECT COUNT(*) AS n FROM files", "--json"])
    assert hist.exit_code == 0, hist.output
    data = json.loads(hist.output)
    assert data["rows"][0]["n"] >= 2

    purge = runner.invoke(cli, ["--db", str(env / "test.db"), "db", "purge", "--grace-days", "1", "--json"])
    assert purge.exit_code == 0, purge.output

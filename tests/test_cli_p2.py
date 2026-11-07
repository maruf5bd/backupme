"""Contract tests for the CLI P2 defect fixes (qa_report_cli.md P2-1, P2-2,
P2-6, P2-7, P2-10 and qa_report_core.md defect 6).

All invocations run through click's CliRunner against the isolated `env`
fixture (scratch config + scratch DB + scratch local backend), never against
the live config.yaml / backupme.db.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli
from tests.helpers import backup_bytes


@pytest.fixture()
def runner():
    return CliRunner()


def invoke(runner, env, *args, **kw):
    """Invoke the CLI pinned to the isolated env's config and DB."""
    return runner.invoke(
        cli,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"), *args],
        **kw,
    )


def parse_json(result):
    """Strict: stdout must be parseable JSON on its own."""
    return json.loads(result.stdout)


def seed_one(env, name="docs/report.txt", data=b"p2-payload-" + os.urandom(8)):
    from backupme.config import load_config
    from backupme.backends import get_all_backends
    res = backup_bytes(env, name, data, get_all_backends(load_config()))
    return res["file_id"], data


# ---------------------------------------------------------------------------
# P2-1 / P2-2: hash-lookup exit codes and FILE sha256 resolution
# ---------------------------------------------------------------------------

def test_hash_lookup_unknown_human_and_json_exit_1(runner, env):
    r1 = invoke(runner, env, "hash-lookup", "f" * 64)
    assert r1.exit_code == 1, r1.output
    assert "Hash not found" in r1.output
    r2 = invoke(runner, env, "hash-lookup", "e" * 64, "--json")
    assert r2.exit_code == 1, r2.output
    data = parse_json(r2)
    assert data["resolved"] is False
    assert data["match_type"] is None


def test_hash_lookup_resolves_file_sha256_with_chunk_mapping(runner, env):
    fid, data = seed_one(env, "docs/small.txt", b"file-sha-payload")
    sha = hashlib.sha256(data).hexdigest()
    r = invoke(runner, env, "hash-lookup", sha, "--json")
    assert r.exit_code == 0, r.output
    payload = parse_json(r)
    assert payload["resolved"] is True
    assert payload["match_type"] == "file"
    assert payload["count"] == 1
    row = payload["files"][0]
    assert row["id"] == fid
    assert row["relative_path"] == "docs/small.txt"
    assert row["sha256"] == sha
    assert row["deleted"] is False
    assert row["chunks"], "chunk->blob mapping missing"
    # Small files are bundled: the chunk blob is a bundle member.
    assert any(c["bundle_member"] for c in row["chunks"])
    # Human mode reports the same resolution.
    rh = invoke(runner, env, "hash-lookup", sha)
    assert rh.exit_code == 0, rh.output
    assert "docs/small.txt" in rh.output
    assert "bundle member" in rh.output


def test_hash_lookup_file_prefix_unique_resolves(runner, env):
    _, data = seed_one(env, "docs/uniq.txt", b"unique-prefix-payload")
    sha = hashlib.sha256(data).hexdigest()
    r = invoke(runner, env, "hash-lookup", sha[:16], "--json")
    assert r.exit_code == 0, r.output
    payload = parse_json(r)
    assert payload["resolved"] is True
    assert payload["match_type"] == "file-prefix"
    assert payload["files"][0]["sha256"] == sha


def test_hash_lookup_file_prefix_ambiguous_exits_1(runner, env):
    from backupme.core.database import execute, commit
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, mtime, "
        "chunked, version_group, version_seq) VALUES "
        "('t', 'a.txt', 'abc4567890abcdef4567890abcdef4567890abcdef4567890abcdef45678901', 1, 0, 0, 't', 1)"
    )
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, mtime, "
        "chunked, version_group, version_seq) VALUES "
        "('t', 'b.txt', 'abc4567890abcdef4567890abcdef4567890abcdef4567890abcdef45678902', 1, 0, 0, 't', 1)"
    )
    commit()
    r = invoke(runner, env, "hash-lookup", "abc4567890", "--json")
    assert r.exit_code == 1, r.output
    payload = parse_json(r)
    assert payload["resolved"] is False
    assert payload["match_type"] == "prefix-ambiguous"
    assert payload["count"] == 2
    rh = invoke(runner, env, "hash-lookup", "abc4567890")
    assert rh.exit_code == 1
    assert "not unique" in rh.output


def test_hash_lookup_file_prefix_no_match_exits_1(runner, env):
    r = invoke(runner, env, "hash-lookup", "ab0000", "--json")
    assert r.exit_code == 1
    assert parse_json(r)["resolved"] is False


def test_hash_lookup_reports_deleted_files(runner, env):
    from backupme.core.database import execute, commit
    fid, data = seed_one(env, "docs/gone.txt", b"deleted-file-payload")
    execute("UPDATE files SET deleted_at = datetime('now') WHERE id = ?", (fid,))
    commit()
    sha = hashlib.sha256(data).hexdigest()
    r = invoke(runner, env, "hash-lookup", sha, "--json")
    assert r.exit_code == 0, r.output
    row = parse_json(r)["files"][0]
    assert row["deleted"] is True
    assert row["deleted_at"]


def test_hash_lookup_blob_match_still_works(runner, env):
    # Chunked files get content-addressed chunk blob ids (64-hex shas);
    # shrink min_size in this isolated config to force the chunked path.
    cfg_path = env / "config.yaml"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8")
        + "chunking:\n  min_size: 1024\n  chunk_size: 8192\n",
        encoding="utf-8",
    )
    from backupme.backends import invalidate_cache
    invalidate_cache()
    seed_one(env, "big/blob.bin", os.urandom(20_000))
    from backupme.core.database import execute
    row = execute(
        "SELECT blob_id FROM blobs WHERE length(blob_id) = 64 "
        "AND blob_id GLOB '[0-9a-f]*' LIMIT 1"
    ).fetchone()
    assert row, "expected at least one content-addressed chunk blob"
    r = invoke(runner, env, "hash-lookup", row["blob_id"], "--json")
    assert r.exit_code == 0, r.output
    payload = parse_json(r)
    assert payload["resolved"] is True
    assert payload["match_type"] == "blob"
    assert payload["blob"]["blob_id"] == row["blob_id"]
    assert payload["blob"]["storage"], "blob storage locations missing"
    # Human mode keeps the historical raw-JSON blob contract (qa2 parses it).
    rh = invoke(runner, env, "hash-lookup", row["blob_id"])
    assert rh.exit_code == 0, rh.output
    assert json.loads(rh.stdout)["blob_id"] == row["blob_id"]


# ---------------------------------------------------------------------------
# P2-6: backend drain of an unknown name
# ---------------------------------------------------------------------------

def test_backend_drain_unknown_name_dry_run_exit_1(runner, env):
    r = invoke(runner, env, "backend", "drain", "ghost_backend", "--dry-run", "--json")
    assert r.exit_code == 1, r.output
    data = parse_json(r)
    assert data["status"] == "error"
    assert "not found" in data["error"]


def test_backend_drain_unknown_name_no_dry_run_exit_1(runner, env):
    # Errors out before any copying could start.
    r = invoke(runner, env, "backend", "drain", "ghost_backend")
    assert r.exit_code == 1, r.output
    assert "not found" in r.output


def test_backend_drain_known_single_backend_still_errors(runner, env):
    """Regression guard: a KNOWN backend with no drain target keeps the
    rebalance-level 'no other healthy backend' error (exit 1)."""
    seed_one(env)
    r = invoke(runner, env, "backend", "drain", "test_local", "--dry-run", "--json")
    assert r.exit_code == 1, r.output
    assert "no other healthy backend" in parse_json(r)["error"]


# ---------------------------------------------------------------------------
# P2-7: db history --json/--no-json
# ---------------------------------------------------------------------------

def test_db_history_default_is_json(runner, env):
    seed_one(env, "history/h1.txt", b"history-payload-1")
    r1 = invoke(runner, env, "db", "cold-export", "--json")
    assert r1.exit_code == 0, r1.output
    r2 = invoke(runner, env, "db", "history", "SELECT id, relative_path FROM files")
    assert r2.exit_code == 0, r2.output
    data = parse_json(r2)
    assert data["count"] == len(data["rows"])
    assert data["count"] >= 1


def test_db_history_no_json_human_table(runner, env):
    seed_one(env, "history/h2.txt", b"history-payload-2")
    r1 = invoke(runner, env, "db", "cold-export", "--json")
    assert r1.exit_code == 0, r1.output
    r2 = invoke(runner, env, "db", "history",
                "SELECT id, relative_path FROM files", "--no-json")
    assert r2.exit_code == 0, r2.output
    assert "relative_path" in r2.output
    assert "history/h2.txt" in r2.output
    assert "row(s)" in r2.output
    # Strict JSON parse must fail on the human output.
    with pytest.raises(json.JSONDecodeError):
        json.loads(r2.stdout)


def test_db_history_no_json_empty_result(runner, env):
    r = invoke(runner, env, "db", "history", "SELECT id FROM files WHERE id < 0", "--no-json")
    assert r.exit_code == 0, r.output
    assert "(no rows)" in r.output
    assert "0 row(s)" in r.output


# ---------------------------------------------------------------------------
# P2-10: --json on backup, download, search, convert, gc
# ---------------------------------------------------------------------------

def test_backup_json_ok_payload(runner, env):
    src1 = env / "j1.txt"
    src1.write_bytes(b"json-backup-payload-one")
    src2 = env / "j2.txt"
    src2.write_bytes(b"json-backup-payload-two")
    r = invoke(runner, env, "backup", str(src1), str(src2), "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["counts"]["ok"] == 2
    assert data["counts"]["skipped"] == 0
    assert data["counts"]["error"] == 0
    assert data["bytes"] == len(b"json-backup-payload-one") + len(b"json-backup-payload-two")
    assert len(data["files"]) == 2
    assert all(f["status"] == "ok" and "file_id" in f for f in data["files"])


def test_backup_json_skipped_file_counted(runner, env):
    skipme = env / "Thumbs.db"
    skipme.write_bytes(b"skip me")
    r = invoke(runner, env, "backup", str(skipme), "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["counts"]["skipped"] == 1
    assert data["counts"]["ok"] == 0
    assert data["sources"][0]["status"] == "skipped"
    assert data["sources"][0]["reason"]


def test_backup_json_missing_path_error_exit_1(runner, env):
    r = invoke(runner, env, "backup", str(env / "no-such-p2.txt"), "--json")
    assert r.exit_code == 1, r.output
    data = parse_json(r)
    assert data["sources"][0]["status"] == "error"
    assert "does not exist" in data["error"]


def test_backup_human_default_unchanged(runner, env):
    src = env / "h1.txt"
    src.write_bytes(b"human-default-payload")
    r = invoke(runner, env, "backup", str(src))
    assert r.exit_code == 0, r.output
    assert "file_id=" in r.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(r.stdout)


def test_download_json_roundtrip(runner, env):
    fid, data = seed_one(env, "docs/dlp2.txt", b"json-download-payload")
    out = env / "dl_out_p2"
    r = invoke(runner, env, "download", str(fid), "--output", str(out), "--json")
    assert r.exit_code == 0, r.output
    d = parse_json(r)
    assert d["status"] == "ok"
    assert d["file_id"] == fid
    assert d["size"] == len(data)
    assert Path(d["path"]).read_bytes() == data


def test_download_json_unknown_id_exit_1(runner, env):
    r = invoke(runner, env, "download", "999999", "--json")
    assert r.exit_code == 1, r.output
    d = parse_json(r)
    assert d["status"] == "error"
    assert "not found" in d["error"]


def test_search_json_rows(runner, env):
    seed_one(env, "docs/teamnotes-p2.txt")
    r = invoke(runner, env, "search", "teamnotes-p2", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["count"] >= 1
    assert any("teamnotes-p2" in row["relative_path"] for row in data["results"])
    assert data["count"] == len(data["results"])


def test_search_human_default_unchanged(runner, env):
    seed_one(env, "docs/humanfind-p2.txt")
    r = invoke(runner, env, "search", "humanfind")
    assert r.exit_code == 0, r.output
    assert "humanfind-p2.txt" in r.output


def test_convert_json_result_dict(runner, env):
    seed_one(env)
    r = invoke(runner, env, "convert", "test_local", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    for key in ("completed", "errors", "skipped"):
        assert key in data


def test_convert_json_unknown_backend_exit_1(runner, env):
    r = invoke(runner, env, "convert", "ghost_backend", "--json")
    assert r.exit_code == 1, r.output
    data = parse_json(r)
    assert data["status"] == "error"
    assert data["error"]


def test_gc_json_shape_and_totals(runner, env):
    seed_one(env)
    r = invoke(runner, env, "gc", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["apply"] is False
    assert "test_local" in data["backends"]
    assert data["total_orphans"] == sum(v["orphans"] for v in data["backends"].values())
    assert data["total_bytes"] == sum(v["bytes"] for v in data["backends"].values())


def test_gc_json_no_backends(runner, env):
    cfg2 = env / "nobackends.yaml"
    cfg2.write_text("encryption:\n  enabled: false\n", encoding="utf-8")
    r = CliRunner().invoke(
        cli, ["--config", str(cfg2), "--db", str(env / "test.db"), "gc", "--json"])
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["backends"] == {}
    assert data["total_orphans"] == 0


def test_gc_human_default_unchanged(runner, env):
    seed_one(env)
    r = invoke(runner, env, "gc")
    assert r.exit_code == 0, r.output
    assert "dry run" in r.output
    assert "total:" in r.output


# ---------------------------------------------------------------------------
# qa_report_core.md defect 6: hard_purge honest accounting
# ---------------------------------------------------------------------------

def test_hard_purge_counts_above_watermark(runner, env, backends):
    from backupme.core import db_lifecycle as lc
    from backupme.core.database import execute, commit
    seed_one(env, "purge/old.txt", b"purge-me-payload " * 5)
    export = lc.cold_export_to_parquet()
    assert export["status"] == "ok", export
    watermark = export["watermark"]

    execute("UPDATE files SET deleted_at = datetime('now', '-30 days') "
            "WHERE deleted_at IS NULL")
    # Soft-deleted + past grace but ABOVE the watermark: must be kept and counted.
    execute(
        "INSERT INTO files (id, source_root, relative_path, sha256, size, mtime, "
        "chunked, redundancy, version_group, version_seq, deleted_at) "
        "VALUES (?, 'purge', 'above.txt', 'deadbeefcafe', 1, 0, 0, 1, "
        "'p2aw', 1, datetime('now', '-30 days'))", (watermark + 77,))
    commit()

    out = lc.hard_purge_versions(grace_days=14)
    assert out["purged"] >= 1
    assert out["above_watermark"] == 1
    assert out["skipped"] == 0  # legacy key kept, always 0 now
    assert execute("SELECT COUNT(*) FROM files WHERE id = ?",
                   (watermark + 77,)).fetchone()[0] == 1

    # CLI purge reports the same accounting in human mode.
    r = invoke(runner, env, "db", "purge", "--grace-days", "14")
    assert r.exit_code == 0, r.output
    assert "above watermark" in r.output


# ---------------------------------------------------------------------------
# Help surface: every new/changed flag is documented by --help
# ---------------------------------------------------------------------------

def test_new_json_flags_in_help(runner, env):
    for path in (("backup",), ("download",), ("search",), ("hash-lookup",),
                 ("convert",), ("gc",), ("db", "history")):
        r = invoke(runner, env, *path, "--help")
        assert r.exit_code == 0, r.output
        assert "--json" in r.output, f"{' '.join(path)} --help missing --json"

"""QA agent 2 black-box CLI sweep (qa2).

Covers every command registered on the `backupme` CLI group. All invocations
run through click's CliRunner against the isolated `env` fixture (scratch
config + scratch DB + scratch local backend), never against the live
config.yaml / backupme.db. Destructive commands (gc --apply, db purge,
backend drain without --dry-run, backend remove of real data) are NOT run.

Defect-documentation tests use @pytest.mark.xfail(strict=False, reason="qa
defect N: ...") and assert the CORRECT behavior, so they start passing if the
defect is ever fixed. Plain tests assert current (correct) behavior.
"""
import gzip
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli
from tests.helpers import backup_bytes

PHANTOM_COMMANDS = ["status", "restore", "check-consistency", "scrub"]


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
    """Strict: stdout must be parseable JSON on its own (AGENTS.md rule)."""
    return json.loads(result.stdout)


def local_backend_path(env):
    from backupme.config import load_config
    cfg = load_config(str(env / "config.yaml"))
    return Path(cfg.backends["local"][0].path)


def stored_blob_paths(env, file_id):
    """Physical file(s) on the local backend that hold this file's bytes.

    Handles bundle members (storage_path ``parent.tar@offset:len`` and
    member blob ids with no blob_storage row) by resolving to the parent
    bundle tar, per the AGENTS.md bundle-member rule.
    """
    from backupme.core.database import execute
    rows = execute(
        "SELECT fc.blob_id, bs.storage_path FROM file_chunks fc "
        "LEFT JOIN blob_storage bs ON bs.blob_id = fc.blob_id WHERE fc.file_id = ?",
        (file_id,),
    ).fetchall()
    root = local_backend_path(env)
    paths = []
    for r in rows:
        sp = r["storage_path"]
        if sp:
            paths.append(root / sp.split("@", 1)[0])
            continue
        blob_id = r["blob_id"] or ""
        if blob_id.count("/") >= 4:  # bundle member -> parent bundle id
            parent_id = blob_id.rsplit("/", 1)[0]
            prow = execute(
                "SELECT storage_path FROM blob_storage WHERE blob_id = ?", (parent_id,)
            ).fetchone()
            if prow and prow["storage_path"]:
                paths.append(root / prow["storage_path"].split("@", 1)[0])
    return paths


def seed_one(env, name="docs/report.txt", data=b"qa2-payload-" + os.urandom(8)):
    from backupme.config import load_config
    from backupme.backends import get_all_backends
    res = backup_bytes(env, name, data, get_all_backends(load_config()))
    return res["file_id"], data


# ---------------------------------------------------------------------------
# 1. Help / inventory sweep
# ---------------------------------------------------------------------------

def _walk_commands(group, prefix=()):
    for name, cmd in group.commands.items():
        path = (*prefix, name)
        yield path, cmd
        if hasattr(cmd, "commands"):
            yield from _walk_commands(cmd, path)


def test_every_command_help_exit_0(runner, env):
    paths = list(_walk_commands(cli))
    assert len(paths) >= 38, f"unexpectedly few commands: {len(paths)}"
    for path, _cmd in paths:
        result = invoke(runner, env, *path, "--help")
        assert result.exit_code == 0, f"{' '.join(path)} --help failed: {result.output}"
        assert "Usage:" in result.output


def test_help_documents_key_options(runner, env):
    checks = {
        ("backup",): ["--backend", "--redundancy", "--recursive", "--move"],
        ("list",): ["--backend", "--hash", "--source-root", "--json"],
        ("download",): ["--output", "--version", "--backend"],
        ("verify",): ["--count", "--backend", "--file"],
        ("health",): ["--json", "--purge-candidates", "--limit"],
        ("gc",): ["--apply"],
        ("web",): ["--host", "--port", "--token"],
        ("db", "history",): ["--json"],
        ("backend", "rescan",): ["--json"],
        ("backend", "add",): ["--name", "--path", "--test"],
        ("ingest-cpanel",): ["--dry-run", "--json", "--watch"],
        ("ingest-status",): ["--json", "--account"],
    }
    for path, opts in checks.items():
        result = invoke(runner, env, *path, "--help")
        assert result.exit_code == 0, result.output
        for opt in opts:
            assert opt in result.output, f"{' '.join(path)} --help missing {opt}"


def test_phantom_manual_commands_do_not_exist(runner, env):
    """MANUAL.md section 10 documents these; they must at least not be
    silently implemented as something else. Today they simply do not exist
    (doc drift, qa_report_cli.md table)."""
    for name in PHANTOM_COMMANDS:
        assert name not in cli.commands, f"{name} unexpectedly exists now: update MANUAL"
        result = invoke(runner, env, name)
        assert result.exit_code == 2
        assert "No such command" in result.output


def test_unknown_command_and_option_exit_2(runner, env):
    r1 = invoke(runner, env, "definitely-not-a-command")
    assert r1.exit_code == 2
    r2 = invoke(runner, env, "health", "--no-such-option")
    assert r2.exit_code == 2


def test_missing_required_argument_exit_2(runner, env):
    r1 = invoke(runner, env, "search")
    assert r1.exit_code == 2
    r2 = invoke(runner, env, "backend", "rescan")
    assert r2.exit_code == 2


# ---------------------------------------------------------------------------
# 2. backup / list / stats / backends / search / hash-lookup / download
# ---------------------------------------------------------------------------

def test_backup_seeds_and_list_json(runner, env):
    fid, _ = seed_one(env)
    r = invoke(runner, env, "list", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    ids = [row["id"] for row in data["files"]]
    assert fid in ids
    assert data["count"] == len(data["files"])


def test_list_source_root_and_hash_filters(runner, env):
    fid, data = seed_one(env)
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    r1 = invoke(runner, env, "list", "--source-root", str(env), "--json")
    assert r1.exit_code == 0, r1.output
    assert fid in [row["id"] for row in parse_json(r1)["files"]]
    r2 = invoke(runner, env, "list", "--hash", sha, "--json")
    assert r2.exit_code == 0, r2.output
    rows = parse_json(r2)["files"]
    assert [row["id"] for row in rows] == [fid]


def test_list_limit_offset(runner, env):
    seed_one(env, "docs/a.txt")
    seed_one(env, "docs/b.txt")
    r = invoke(runner, env, "list", "--limit", "1", "--offset", "1", "--json")
    assert r.exit_code == 0, r.output
    assert len(parse_json(r)["files"]) == 1


def test_list_human_output(runner, env):
    seed_one(env)
    r = invoke(runner, env, "list")
    assert r.exit_code == 0, r.output
    assert "report" in r.output or "(none)" not in r.output


@pytest.mark.xfail(reason="qa defect 1: list --backend compares source_root to the "
                          "backend name, so it never matches real rows",
                   strict=False)
def test_list_backend_filter_matches_files_on_backend(runner, env):
    fid, _ = seed_one(env)
    r = invoke(runner, env, "list", "--backend", "test_local", "--json")
    assert r.exit_code == 0, r.output
    assert fid in [row["id"] for row in parse_json(r)["files"]]


def test_stats_json_and_human(runner, env):
    seed_one(env)
    r = invoke(runner, env, "stats", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["total_files"] >= 1
    for key in ("generation", "blobs", "raw_bytes", "stored_bytes", "ratio"):
        assert key in data
    r2 = invoke(runner, env, "stats")
    assert r2.exit_code == 0
    assert "Total files" in r2.output


def test_backends_json_lists_test_local(runner, env):
    r = invoke(runner, env, "backends", "--json")
    assert r.exit_code == 0, r.output
    rows = parse_json(r)["backends"]
    names = [row["name"] for row in rows]
    assert "test_local" in names
    assert any(row["status"] == "connected" for row in rows)


def test_search_finds_backed_up_file(runner, env):
    seed_one(env, "docs/teamnotes.txt")
    r = invoke(runner, env, "search", "teamnotes")
    assert r.exit_code == 0, r.output
    assert "teamnotes.txt" in r.output


def test_search_no_match_exit_0(runner, env):
    r = invoke(runner, env, "search", "zzzqqqzzz")
    assert r.exit_code == 0, r.output


@pytest.mark.xfail(reason="qa defect 8: search passes the raw query to FTS5 MATCH; "
                          "a hyphenated term like 'report-2026' parses as a column "
                          "filter and crashes with OperationalError", strict=False)
def test_search_hyphenated_query_clean_or_results(runner, env):
    seed_one(env, "docs/annual-report-2026.txt")
    r = invoke(runner, env, "search", "annual-report-2026")
    assert r.exit_code == 0, r.output
    assert "Traceback" not in r.output


def test_search_content_flag_runs(runner, env):
    seed_one(env, "notes.txt", b"the quarterly zebra budget")
    r = invoke(runner, env, "search", "zebra", "--content")
    assert r.exit_code == 0, r.output
    # NOTE: content_text is only populated for legacy inline rows, so a match
    # is not asserted here (see qa_report_cli.md P2 note).


def test_hash_lookup_known_and_unknown(runner, env):
    import hashlib
    # A bundle member's FILE sha is not a blob id -> "Hash not found" (P2 note).
    _, data = seed_one(env)
    file_sha = hashlib.sha256(data).hexdigest()
    r0 = invoke(runner, env, "hash-lookup", file_sha)
    assert r0.exit_code == 0, r0.output
    assert "Hash not found" in r0.output
    # Chunked files get content-addressed chunk blob ids (64-hex shas).
    # Default chunking min_size is 10 MB, so shrink it in this isolated config
    # to force the chunked path for a small file.
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
    r1 = invoke(runner, env, "hash-lookup", row["blob_id"])
    assert r1.exit_code == 0, r1.output
    payload = json.loads(r1.stdout)
    assert payload
    r2 = invoke(runner, env, "hash-lookup", "f" * 64)
    assert r2.exit_code == 0
    assert "not found" in r2.output.lower()


def test_select_backend_fuzzy(runner, env):
    r = invoke(runner, env, "select-backend", "local")
    assert r.exit_code == 0, r.output
    assert "test_local" in r.output


def test_download_roundtrip_and_output_layout(runner, env):
    fid, data = seed_one(env, "docs/dl.txt", b"download-me-bytes")
    out = env / "dl_out"
    r = invoke(runner, env, "download", str(fid), "--output", str(out))
    assert r.exit_code == 0, r.output
    assert "Downloaded to" in r.output
    got = (out / f"file_{fid}").read_bytes()
    assert got == data


@pytest.mark.xfail(reason="qa defect 2: download <unknown id> escapes as a raw "
                          "FileNotFoundError traceback instead of a clean CLI error",
                   strict=False)
def test_download_unknown_id_clean_error(runner, env):
    r = invoke(runner, env, "download", "999999")
    assert r.exit_code == 1
    assert "not found" in r.output.lower()
    assert "Traceback" not in r.output


def test_download_unknown_id_current_behavior_is_exit_1(runner, env):
    """Fixed (defect 2): exit 1 AND a clean error message, no traceback."""
    r = invoke(runner, env, "download", "999999")
    assert r.exit_code == 1
    assert "Error" in r.output and "not found" in r.output
    assert "Traceback" not in r.output


def test_upload_bundle_small_files(runner, env):
    d = env / "smalls"
    d.mkdir()
    (d / "one.bin").write_bytes(b"a" * 2048)
    (d / "two.bin").write_bytes(b"b" * 4096)
    r = invoke(runner, env, "upload-bundle", str(d))
    assert r.exit_code == 0, r.output
    assert "Uploaded 2 files" in r.output


def test_backup_move_removes_source(runner, env):
    src = env / "movable.txt"
    src.write_bytes(b"move me")
    r = invoke(runner, env, "backup", str(src), "--move")
    assert r.exit_code == 0, r.output
    assert not src.exists()
    assert "file_id=" in r.output


@pytest.mark.xfail(reason="qa defect 3: backup silently ignores a nonexistent "
                          "source path and exits 0", strict=False)
def test_backup_nonexistent_path_reports_error(runner, env):
    missing = env / "no-such-path-12345.txt"
    r = invoke(runner, env, "backup", str(missing))
    assert r.exit_code != 0 or "no such" in r.output.lower() or "not found" in r.output.lower()


def test_backup_nonexistent_path_current_behavior_silent_success(runner, env):
    """Fixed (defect 3): nonexistent path is an error (exit 1), not silence."""
    r = invoke(runner, env, "backup", str(env / "no-such-path-12345.txt"))
    assert r.exit_code == 1
    assert "does not exist" in r.output


# ---------------------------------------------------------------------------
# 3. db group
# ---------------------------------------------------------------------------

def test_db_info_json(runner, env):
    r = invoke(runner, env, "db", "info", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    for key in ("schema_version", "files", "blobs", "generation", "database_size_bytes"):
        assert key in data


def test_db_check_json_ok(runner, env):
    seed_one(env)
    r = invoke(runner, env, "db", "check", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["integrity"] == "ok"
    assert data["errors"] == []


def test_db_check_full_flag(runner, env):
    r = invoke(runner, env, "db", "check", "--full")
    assert r.exit_code == 0, r.output
    assert "ok" in r.output


def test_db_snapshot_creates_compressed_snapshot(runner, env):
    seed_one(env)
    r = invoke(runner, env, "db", "snapshot", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    snap = Path(data["snapshot"])
    assert snap.exists(), f"snapshot file missing: {snap}"


def test_db_oplog_json(runner, env):
    r = invoke(runner, env, "db", "oplog", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert "rows" in data and "stats" in data


def test_db_cold_export_dry_run_json(runner, env):
    r = invoke(runner, env, "db", "cold-export", "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["status"] == "dry-run"
    for key in ("watermark", "eligible_rows", "parquet_dir"):
        assert key in data


def test_db_cold_export_then_history_duckdb(runner, env):
    fid, data = seed_one(env, "history/hist.txt", b"cold-history-payload")
    r1 = invoke(runner, env, "db", "cold-export", "--json")
    assert r1.exit_code == 0, r1.output
    d1 = parse_json(r1)
    assert d1.get("status") == "ok", d1
    assert d1.get("rows", 0) >= 1
    r2 = invoke(runner, env, "db", "history",
                "SELECT id, relative_path FROM files WHERE id = " + str(fid))
    assert r2.exit_code == 0, r2.output
    d2 = parse_json(r2)
    assert d2["count"] == 1
    assert d2["rows"][0]["relative_path"] == "history/hist.txt"
    r3 = invoke(runner, env, "db", "history", "SELECT COUNT(*) AS n FROM files")
    assert r3.exit_code == 0, r3.output
    assert parse_json(r3)["rows"][0]["n"] >= 1


def test_db_vacuum_exit_0(runner, env):
    r = invoke(runner, env, "db", "vacuum")
    assert r.exit_code == 0, r.output
    assert "vacuumed" in r.output


def test_db_prune_oplog_json(runner, env):
    r = invoke(runner, env, "db", "prune-oplog", "--days", "0", "--json")
    assert r.exit_code == 0, r.output
    assert "pruned" in parse_json(r)


# ---------------------------------------------------------------------------
# 4. health / verify
# ---------------------------------------------------------------------------

def test_health_json_empty_db(runner, env):
    r = invoke(runner, env, "health", "--json")
    assert r.exit_code == 0, r.output
    s = parse_json(r)["summary"]
    assert s["live_files"] == 0
    for key in ("no_chunks_files", "missing_blob_files", "missing_blobs",
                "same_name_groups", "purge_candidates"):
        assert key in s


def test_health_json_seeded_strict_parse(runner, env):
    seed_one(env)
    r = invoke(runner, env, "health", "--json")
    assert r.exit_code == 0, r.output
    assert parse_json(r)["summary"]["live_files"] >= 1


def test_health_human_and_purge_candidates(runner, env):
    seed_one(env)
    r = invoke(runner, env, "health", "--purge-candidates", "--limit", "5")
    assert r.exit_code == 0, r.output
    assert "live files" in r.output
    assert "purge candidates" in r.output


def test_verify_count_empty_db_exit_0(runner, env):
    r = invoke(runner, env, "verify", "--count", "3")
    assert r.exit_code == 0, r.output
    assert "checked=0" in r.output


def test_verify_file_ok_exit_0(runner, env):
    fid, _ = seed_one(env)
    r = invoke(runner, env, "verify", "--file", str(fid))
    assert r.exit_code == 0, r.output
    assert "OK" in r.output


def test_verify_unknown_file_id_exit_1(runner, env):
    r = invoke(runner, env, "verify", "--file", "424242")
    assert r.exit_code == 1, r.output
    assert "ERROR" in r.output


def test_verify_detects_corrupt_blob_exit_1(runner, env):
    fid, _ = seed_one(env)
    paths = stored_blob_paths(env, fid)
    assert paths, "no stored blob rows for seeded file"
    for p in paths:
        p.write_bytes(b"CORRUPTED!!!")
    r = invoke(runner, env, "verify", "--file", str(fid))
    assert r.exit_code == 1, r.output
    assert "FAILED" in r.output or "ERROR" in r.output


def test_verify_backend_filter_seeded_exit_0(runner, env):
    fid, _ = seed_one(env)
    r = invoke(runner, env, "verify", "--count", "5", "--backend", "test_local")
    assert r.exit_code == 0, r.output
    assert "checked=1" in r.output


def test_verify_unknown_backend_silent_zero_checked(runner, env):
    """Fixed (P2): a typo'd backend name errors out (exit 1)."""
    r = invoke(runner, env, "verify", "--count", "5", "--backend", "no_such_backend")
    assert r.exit_code == 1, r.output
    assert "not found" in r.output


# ---------------------------------------------------------------------------
# 5. backend group
# ---------------------------------------------------------------------------

def test_backend_list_json(runner, env):
    r = invoke(runner, env, "backend", "list", "--json")
    assert r.exit_code == 0, r.output
    rows = parse_json(r)["backends"]
    assert any(row["name"] == "test_local" for row in rows)


def test_backend_rescan_clean_exit_0(runner, env):
    seed_one(env)
    r = invoke(runner, env, "backend", "rescan", "test_local", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["status"] == "ok"
    assert data["missing_on_box"] == []
    assert data["size_mismatch"] == []


def test_backend_rescan_missing_blob_exit_1(runner, env):
    fid, _ = seed_one(env)
    for p in stored_blob_paths(env, fid):
        p.unlink()
    r = invoke(runner, env, "backend", "rescan", "test_local")
    assert r.exit_code == 1, r.output
    assert "missing on box" in r.output


def test_backend_rescan_unknown_name_exit_1(runner, env):
    r = invoke(runner, env, "backend", "rescan", "ghost_backend")
    assert r.exit_code == 1, r.output
    assert "no backend named" in r.output


def test_backend_test_known_ok(runner, env):
    r = invoke(runner, env, "backend", "test", "test_local", "--json")
    assert r.exit_code == 0, r.output
    assert parse_json(r)["connected"] is True


@pytest.mark.xfail(reason="qa defect 4: backend test of an unknown backend emits "
                          "an error payload but still exits 0", strict=False)
def test_backend_test_unknown_name_exit_nonzero(runner, env):
    r = invoke(runner, env, "backend", "test", "ghost_backend", "--json")
    assert r.exit_code != 0


def test_backend_test_unknown_name_current_exit_0(runner, env):
    """Fixed (defect 4): unknown name exits 1."""
    r = invoke(runner, env, "backend", "test", "ghost_backend", "--json")
    assert r.exit_code == 1
    assert "not found" in r.output
    assert parse_json(r)["connected"] is False


def test_backend_add_non_interactive_writes_config(runner, env):
    storage2 = env.parent / "storage2"
    storage2.mkdir(exist_ok=True)
    r = invoke(runner, env, "backend", "add", "local",
               "--name", "qa2_second", "--path", str(storage2),
               input="\n")  # final "Test connection now?" confirm
    assert r.exit_code == 0, r.output
    assert "added" in r.output
    r2 = invoke(runner, env, "backend", "list", "--json")
    names = [row["name"] for row in parse_json(r2)["backends"]]
    assert "qa2_second" in names


def test_backend_add_duplicate_offers_overwrite_cancel(runner, env):
    storage2 = env.parent / "storage2"
    storage2.mkdir(exist_ok=True)
    r1 = invoke(runner, env, "backend", "add", "local",
                "--name", "qa2_dup", "--path", str(storage2), input="\n")
    assert r1.exit_code == 0, r1.output
    r2 = invoke(runner, env, "backend", "add", "local",
                "--name", "qa2_dup", "--path", str(storage2),
                input="n\n")  # decline overwrite
    assert r2.exit_code == 0, r2.output
    assert "Cancelled" in r2.output


@pytest.mark.xfail(reason="qa defect 5: backend add accepts --test/--no-test but "
                          "the body never uses it; 'Test connection now?' always "
                          "prompts, so --no-test still blocks non-interactive use",
                   strict=False)
def test_backend_add_no_test_is_fully_non_interactive(runner, env):
    storage3 = env.parent / "storage3"
    storage3.mkdir(exist_ok=True)
    r = invoke(runner, env, "backend", "add", "local",
               "--name", "qa2_nointer", "--path", str(storage3), "--no-test")
    assert r.exit_code == 0, r.output


def test_backend_add_no_test_current_behavior_still_prompts(runner, env):
    """Fixed (defect 5): --no-test is honored — no confirm, clean exit 0."""
    storage3 = env.parent / "storage3"
    storage3.mkdir(exist_ok=True)
    r = invoke(runner, env, "backend", "add", "local",
               "--name", "qa2_nointer", "--path", str(storage3), "--no-test")
    assert r.exit_code == 0, r.output
    assert "Test connection now?" not in r.output  # --no-test honored
    text = (env / "config.yaml").read_text(encoding="utf-8")
    assert "qa2_nointer" in text


def test_backend_add_interactive_type_prompt(runner, env):
    r = invoke(runner, env, "backend", "add",
               input="local\nqa2_inter\n\n\n\nn\n")
    assert r.exit_code == 0, r.output
    text = (env / "config.yaml").read_text(encoding="utf-8")
    assert "qa2_inter" in text


def test_backend_onboard_known_dry_run_exit_0(runner, env):
    r = invoke(runner, env, "backend", "onboard", "test_local", "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    assert parse_json(r)["status"] == "ok"


def test_backend_onboard_unknown_exit_2(runner, env):
    r = invoke(runner, env, "backend", "onboard", "ghost_backend", "--json")
    assert r.exit_code == 2, r.output


def test_backend_rebalance_dry_run_json(runner, env):
    seed_one(env)
    r = invoke(runner, env, "backend", "rebalance", "test_local", "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["status"] == "dry-run"
    for key in ("target", "blobs", "bytes"):
        assert key in data


def test_backend_drain_dry_run_json(runner, env):
    seed_one(env)
    from tests.helpers import add_backend
    add_backend(env, "qa2_drain_target", env.parent / "storage_drain")
    r = invoke(runner, env, "backend", "drain", "test_local", "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["status"] == "dry-run"
    assert data["blobs"] >= 1


def test_backend_drain_single_backend_exit_1(runner, env):
    """With no other backend to receive data, drain exits 1 with a clear error."""
    seed_one(env)
    r = invoke(runner, env, "backend", "drain", "test_local", "--dry-run", "--json")
    assert r.exit_code == 1, r.output
    assert "no other healthy backend" in parse_json(r)["error"]


def test_backend_drain_unknown_name_silent_noop(runner, env):
    """Documents P2: drain of an unknown backend reports an empty dry-run plan
    instead of an unknown-backend error."""
    r = invoke(runner, env, "backend", "drain", "ghost_backend", "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    assert parse_json(r)["status"] == "dry-run"


@pytest.mark.xfail(reason="qa defect 6: backend remove of an unknown name claims "
                          "'Removed backend' (its found-flag only checks that some "
                          "other backend type list is non-empty) and exits 0",
                   strict=False)
def test_backend_remove_unknown_exit_1(runner, env):
    r = invoke(runner, env, "backend", "remove", "ghost_backend")
    assert r.exit_code == 1


def test_backend_remove_unknown_current_false_success(runner, env):
    """Fixed (defect 6): unknown name exits 1 without touching config."""
    import yaml
    before = yaml.safe_load((env / "config.yaml").read_text(encoding="utf-8"))
    r = invoke(runner, env, "backend", "remove", "ghost_backend")
    assert r.exit_code == 1
    assert "No backend named" in r.output
    assert "Removed backend" not in r.output
    after = yaml.safe_load((env / "config.yaml").read_text(encoding="utf-8"))
    assert after == before  # config content unchanged


def test_backend_remove_then_list(runner, env):
    storage2 = env.parent / "storage2"
    storage2.mkdir(exist_ok=True)
    invoke(runner, env, "backend", "add", "local",
           "--name", "qa2_tmp", "--path", str(storage2), input="n\n")
    r = invoke(runner, env, "backend", "remove", "qa2_tmp")
    assert r.exit_code == 0, r.output
    r2 = invoke(runner, env, "backend", "list", "--json")
    assert "qa2_tmp" not in [row["name"] for row in parse_json(r2)["backends"]]


# ---------------------------------------------------------------------------
# 6. gc (dry run only), recompress, convert, report
# ---------------------------------------------------------------------------

def test_gc_dry_run_exit_0(runner, env):
    seed_one(env)
    r = invoke(runner, env, "gc")
    assert r.exit_code == 0, r.output
    assert "orphan" in r.output
    assert "dry run" in r.output


def test_gc_no_backends_message(runner, env):
    cfg2 = env / "nobackends.yaml"
    cfg2.write_text("encryption:\n  enabled: false\n", encoding="utf-8")
    r = CliRunner().invoke(cli, ["--config", str(cfg2), "--db", str(env / "test.db"), "gc"])
    assert r.exit_code == 0, r.output
    assert "no backends" in r.output


def test_recompress_runs_and_reports(runner, env):
    seed_one(env)
    r = invoke(runner, env, "recompress", "--level", "1")
    assert r.exit_code == 0, r.output
    assert "Recompressed" in r.output


def test_recompress_unknown_backend_silent_zero(runner, env):
    """Fixed (P2): unknown backend name errors out (exit 1)."""
    r = invoke(runner, env, "recompress", "--backend", "ghost_backend")
    assert r.exit_code == 1, r.output
    assert "not found" in r.output


def test_convert_no_files_ok(runner, env):
    r = invoke(runner, env, "convert", "test_local")
    assert r.exit_code == 0, r.output
    assert "Converted 0 files" in r.output


@pytest.mark.xfail(reason="qa defect 7: convert <unknown backend> escapes as a "
                          "raw ValueError traceback instead of a clean CLI error",
                   strict=False)
def test_convert_unknown_backend_clean_error(runner, env):
    r = invoke(runner, env, "convert", "ghost_backend")
    assert r.exit_code == 1
    assert "not found" in r.output.lower()
    assert "Traceback" not in r.output


def test_convert_unknown_backend_current_traceback(runner, env):
    """Fixed (defect 7): clean error message, no traceback."""
    r = invoke(runner, env, "convert", "ghost_backend")
    assert r.exit_code == 1
    assert "Error" in r.output
    assert "Traceback" not in r.output


def test_report_group_lifecycle(runner, env):
    seed_one(env)
    r0 = invoke(runner, env, "report", "integrity")
    assert r0.exit_code == 0, r0.output
    json.loads(r0.stdout)
    r1 = invoke(runner, env, "report", "verify", "--count", "1")
    assert r1.exit_code == 0, r1.output
    payload = json.loads(r1.stdout)
    assert "check_id" in payload
    r2 = invoke(runner, env, "report", "list")
    assert r2.exit_code == 0, r2.output
    assert "#" in r2.output
    r3 = invoke(runner, env, "report", "get", "1")
    assert r3.exit_code == 0, r3.output
    json.loads(r3.stdout)


def test_report_get_unknown_current_exit_0(runner, env):
    """Fixed (P2): unknown report id exits 1."""
    r = invoke(runner, env, "report", "get", "999999")
    assert r.exit_code == 1
    assert "Report not found" in r.output


# ---------------------------------------------------------------------------
# 7. ingest-cpanel / ingest-status / restore-cpanel
# ---------------------------------------------------------------------------

def make_drop_root(root: Path, account: str = "qa2acct") -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, payload in (
            (f"homedir/public_html/index.html", b"<h1>qa2 site</h1>"),
            (f"mysql/{account}.sql", b"CREATE TABLE qa2 (id INT);"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    drop = root / "drop"
    acct_dir = drop / account
    acct_dir.mkdir(parents=True, exist_ok=True)
    (acct_dir / f"cpmove-{account}.tar.gz").write_bytes(buf.getvalue())
    return drop


def test_ingest_cpanel_dry_run_json_smoke(runner, env):
    """AGENTS.md full-CLI-smoke rule: exit 0, stdout parses as JSON."""
    drop = make_drop_root(env)
    r = invoke(runner, env, "ingest-cpanel", str(drop), "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["status"] == "dry-run"
    assert data["archives"] == 1
    assert data["entries"][0]["account"] == "qa2acct"
    assert data["entries"][0]["members"] == 2


def test_ingest_cpanel_dry_run_empty_drop_root(runner, env):
    empty = env / "empty_drop"
    empty.mkdir()
    r = invoke(runner, env, "ingest-cpanel", str(empty), "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    assert parse_json(r)["archives"] == 0


def test_ingest_cpanel_no_backends_exit_2(runner, env):
    cfg2 = env / "nobackends.yaml"
    cfg2.write_text("encryption:\n  enabled: false\n", encoding="utf-8")
    drop = make_drop_root(env)
    r = CliRunner().invoke(
        cli, ["--config", str(cfg2), "--db", str(env / "test.db"),
              "ingest-cpanel", str(drop), "--dry-run", "--json"])
    assert r.exit_code == 2, r.output
    assert "no backends configured" in r.output


def test_ingest_cpanel_real_ingest_then_status_then_restore(runner, env):
    drop = make_drop_root(env)
    r1 = invoke(runner, env, "ingest-cpanel", str(drop), "--json")
    assert r1.exit_code == 0, r1.output
    d1 = parse_json(r1)
    assert d1["status"] == "ok", d1
    assert d1["archives"] == 1

    r2 = invoke(runner, env, "ingest-status", "--json")
    assert r2.exit_code == 0, r2.output
    d2 = parse_json(r2)
    accts = [a["source_root"] for a in d2["accounts"]]
    assert "cpanel/qa2acct" in accts
    assert d2["storage"]["sqlite_size"] > 0
    gens = d2["generations"]
    assert gens, "no generations reported"
    generation = gens[0]["generation"]

    r3 = invoke(runner, env, "restore-cpanel", "qa2acct", str(generation), "--json")
    assert r3.exit_code == 0, r3.output
    d3 = parse_json(r3)
    assert d3["status"] == "ok"
    assert d3["members"] == 2
    out = Path(d3["out"])
    restored = list(out.rglob("index.html"))
    assert restored and b"qa2 site" in restored[0].read_bytes()

    # The ingested archive moved to drop/.done/<account>/ (AGENTS.md rule), so a
    # second run finds nothing new and reports idle instead of double-ingesting.
    r4 = invoke(runner, env, "ingest-cpanel", str(drop), "--json")
    assert r4.exit_code == 0, r4.output
    d4 = parse_json(r4)
    assert d4["status"] == "idle", d4
    assert d4["archives"] == 0
    done = drop / ".done" / "qa2acct"
    assert (done / "cpmove-qa2acct.tar.gz").exists(), f"archive not in .done: {list(done.glob('*'))}"


def test_restore_cpanel_unknown_account_exit_1(runner, env):
    r = invoke(runner, env, "restore-cpanel", "ghost_acct", "1", "--json")
    assert r.exit_code == 1, r.output
    assert parse_json(r)["status"] == "error"


def test_ingest_cpanel_missing_drop_root_exit_2(runner, env):
    r = invoke(runner, env, "ingest-cpanel", str(env / "no_such_drop"), "--dry-run")
    assert r.exit_code == 2


# ---------------------------------------------------------------------------
# 8. encrypt (isolated HOME), web/daemon help-only, global options
# ---------------------------------------------------------------------------

def test_encrypt_status_and_init_isolated_home(runner, env, tmp_path, monkeypatch):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    r1 = invoke(runner, env, "encrypt", "status")
    assert r1.exit_code == 0, r1.output
    assert "No encryption key found" in r1.output
    r2 = invoke(runner, env, "encrypt", "init")
    assert r2.exit_code == 0, r2.output
    assert (fake_home / ".backupme" / "key.enc").exists()
    r3 = invoke(runner, env, "encrypt", "status")
    assert r3.exit_code == 0
    assert "exists" in r3.output


def test_web_and_daemon_help_only(runner, env):
    r1 = invoke(runner, env, "web", "--help")
    assert r1.exit_code == 0, r1.output
    for opt in ("--host", "--port", "--token"):
        assert opt in r1.output
    r2 = invoke(runner, env, "daemon", "--help")
    assert r2.exit_code == 0, r2.output
    # daemon has NO subcommands although MANUAL says start|stop|status
    assert "start" not in r2.output.split("Commands:")[-1] if "Commands:" in r2.output else True


def test_daemon_has_no_subcommands(runner, env):
    r = invoke(runner, env, "daemon", "start")
    assert r.exit_code == 2
    assert "No such command" in r.output or "Got unexpected extra argument" in r.output


def test_global_db_option_points_cli_at_named_db(runner, env):
    """--db must be honored: querying through a fresh DB path yields an empty
    index even though the env DB has rows."""
    seed_one(env)
    fresh = env / "fresh.db"
    r = CliRunner().invoke(
        cli, ["--config", str(env / "config.yaml"), "--db", str(fresh), "stats", "--json"])
    assert r.exit_code == 0, r.output
    assert parse_json(r)["total_files"] == 0
    assert fresh.exists()

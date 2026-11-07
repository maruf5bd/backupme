"""Content-index backfill: fills files.content_text for rows that predate
upload-time population, through the real restore path.

Legacy-row simulation: current uploads already populate content_text for
small strict-UTF-8 files (uploader._extract_content_text), so every seeded
row has its column explicitly NULLed after upload to reproduce a historical
row. The FTS index is deliberately left alone — files_fts is an
external-content table whose stale terms the backfill re-derives with the
FTS5 'rebuild' command.

All runs use the isolated env/enc_env fixtures (scratch config + scratch DB
+ scratch local backend), never the live config.yaml / backupme.db. The
backfill itself is non-destructive: it only UPDATEs files.content_text.
"""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.backends import get_all_backends
from backupme.cli import cli
from backupme.config import load_config
from backupme.core import index as index_mod
from backupme.core.content_backfill import backfill_content
from backupme.core.database import execute, commit
from tests.helpers import add_backend, backup_bytes


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


def seed(env, rel, data, backends, redundancy=1):
    """Upload through the real pipeline, then NULL content_text to simulate
    a row from before upload-time population existed."""
    res = backup_bytes(Path(env), rel, data, backends, redundancy=redundancy)
    file_id = res["file_id"]
    execute("UPDATE files SET content_text = NULL WHERE id = ?", (file_id,))
    commit()
    return file_id


def content_of(file_id):
    row = execute("SELECT content_text FROM files WHERE id = ?", (file_id,)).fetchone()
    return row[0] if row else "<missing>"


def chunk_copy_backends(file_id):
    rows = execute(
        "SELECT DISTINCT bs.backend_name FROM file_chunks fc "
        "JOIN blob_storage bs ON bs.blob_id = fc.blob_id WHERE fc.file_id = ?",
        (file_id,),
    ).fetchall()
    return sorted(r[0] for r in rows)


# ---------------------------------------------------------------------------
# Core module behavior
# ---------------------------------------------------------------------------

def test_backfill_populates_text_and_fts_finds_it(env, backends):
    file_id = seed(env, "docs/notes.txt", b"quarterly budget zebras 2026", backends)
    assert content_of(file_id) is None
    result = backfill_content(load_config())
    assert result["status"] == "ok"
    assert result["updated"] == 1
    assert result["errors"] == 0
    assert result["remaining"] == 0
    assert content_of(file_id) == "quarterly budget zebras 2026"
    # FTS MATCH (external-content index must have been re-derived) ...
    hits = index_mod.search_files("zebras")
    assert [h["id"] for h in hits] == [file_id]
    # ... and the plain files-table LIKE the CLI/dashboard content search uses.
    like = execute(
        "SELECT id FROM files WHERE content_text LIKE ? AND deleted_at IS NULL",
        ("%budget%",),
    ).fetchall()
    assert [r[0] for r in like] == [file_id]


def test_binary_content_skipped_stays_null(env, backends):
    file_id = seed(env, "bin/blob.dat", bytes(range(64)) + b"\xff\xfe\x81", backends)
    result = backfill_content(load_config())
    assert result["status"] == "ok"
    assert result["skipped_binary"] == 1
    assert result["updated"] == 0
    assert content_of(file_id) is None
    assert result["remaining"] == 1  # still unindexed; a re-run would retry it


def test_oversized_not_touched(env, backends):
    file_id = seed(env, "big/manual.txt", b"word " * 20000, backends)  # 100,000 B
    result = backfill_content(load_config(), max_size=65536)
    assert result["too_large"] == 1
    assert result["scanned"] == 0
    assert result["updated"] == 0
    assert result["remaining"] == 0  # remaining counts only rows within the cap
    assert content_of(file_id) is None
    # Raising the cap makes the same row a candidate again.
    result2 = backfill_content(load_config(), max_size=200000)
    assert result2["updated"] == 1
    assert content_of(file_id) == "word " * 20000


def test_dry_run_counts_candidates_without_touching(env, backends):
    file_id = seed(env, "docs/dry.txt", b"dry run candidate body", backends)
    result = backfill_content(load_config(), dry_run=True)
    assert result["status"] == "dry-run"
    assert result["candidates"] == 1
    assert result["updated"] == 0
    assert result["scanned"] == 0
    assert content_of(file_id) is None
    # The real run afterwards still finds and fills the row.
    result2 = backfill_content(load_config())
    assert result2["updated"] == 1
    assert content_of(file_id) == "dry run candidate body"


def test_limit_honored_and_resumable(env, backends):
    file_ids = [
        seed(env, f"docs/lim{i}.txt", f"limit body {i} zebra".encode(), backends)
        for i in range(3)
    ]
    result = backfill_content(load_config(), limit=2)
    assert result["scanned"] == 2
    assert result["updated"] == 2
    assert result["remaining"] == 1
    assert len([f for f in file_ids if content_of(f) is not None]) == 2
    # Resumable: the next run picks up the rest.
    result2 = backfill_content(load_config(), limit=2)
    assert result2["updated"] == 1
    assert result2["remaining"] == 0
    assert all(content_of(f) is not None for f in file_ids)


def test_backend_filter_only_fills_rows_with_copies_on_that_backend(env, backends):
    add_backend(env, "test_second", Path(env).parent / "storage_second")
    all_backends = get_all_backends(load_config())
    only_local = seed(env, "docs/local_only.txt",
                      b"local only body markeraa", all_backends, redundancy=1)
    both = seed(env, "docs/both.txt",
                b"replicated body markerbb", all_backends, redundancy=2)
    # Sanity: the first upload went to the single highest-priority backend,
    # the second is replicated to both.
    assert chunk_copy_backends(only_local) == ["test_local"]
    assert chunk_copy_backends(both) == ["test_local", "test_second"]

    result = backfill_content(load_config(), backend="test_second")
    assert result["status"] == "ok"
    assert result["updated"] == 1
    assert content_of(both) == "replicated body markerbb"
    assert content_of(only_local) is None
    # remaining is scoped to the same filter: only_local has no copy on
    # test_second, so a test_second-scoped re-run has nothing left to do.
    assert result["remaining"] == 0
    # Without the filter the remaining row is filled.
    result2 = backfill_content(load_config())
    assert result2["updated"] == 1
    assert content_of(only_local) == "local only body markeraa"


def test_encrypted_config_skipped_with_zero_updates(enc_env):
    enc_backends = get_all_backends(load_config())
    file_id = seed(enc_env, "docs/enc.txt", b"encrypted body secretzz", enc_backends)
    result = backfill_content(load_config())
    assert result["status"] == "skipped_encrypted"
    assert result["updated"] == 0
    assert result["scanned"] == 0
    assert content_of(file_id) is None


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_dry_run_then_apply_json(runner, env, backends):
    file_id = seed(env, "docs/cli.txt", b"cli backfill body", backends)
    r1 = invoke(runner, env, "db", "backfill-content", "--dry-run", "--json")
    assert r1.exit_code == 0, r1.output
    payload1 = json.loads(r1.stdout)
    assert payload1["status"] == "dry-run"
    assert payload1["candidates"] == 1
    assert content_of(file_id) is None

    r2 = invoke(runner, env, "db", "backfill-content", "--json")
    assert r2.exit_code == 0, r2.output
    payload2 = json.loads(r2.stdout)
    assert payload2["status"] == "ok"
    assert payload2["updated"] == 1
    assert payload2["remaining"] == 0
    assert content_of(file_id) == "cli backfill body"

    rh = invoke(runner, env, "db", "backfill-content")
    assert rh.exit_code == 0, rh.output
    assert "remaining: 0" in rh.output


def test_cli_help_mentions_scope_and_resume(runner, env):
    r = invoke(runner, env, "db", "backfill-content", "--help")
    assert r.exit_code == 0, r.output
    assert "encryption" in r.output
    assert "remaining" in r.output
    assert "Small text files only" in r.output


def test_cli_unknown_backend_exit_1(runner, env):
    r = invoke(runner, env, "db", "backfill-content",
               "--backend", "ghost_backend", "--json")
    assert r.exit_code == 1, r.output
    assert "Error:" in r.output
    assert "not found" in r.output
    assert "updated" not in r.output  # clean error, no result payload

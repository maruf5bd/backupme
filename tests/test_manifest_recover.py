"""``backupme db recover-from-manifests`` (archive-level vault recovery): tests.

Seeds a scratch backend through the REAL engine (sidecars on disk), then:

- full recovery into a fresh sqlite file reproduces the original row counts
  (files / file_chunks / blobs / blob_storage / archives / archive_members)
  AND the exact per-blob refcounts;
- a deleted blob object turns into a problems entry, the affected member
  claims NO files row, its archive row is still recorded, everything else
  recovers;
- --dry-run writes no file; --into an existing non-empty file refuses with
  exit 1; an unknown backend exits 1;
- the live DB is never touched (the command's own safety contract: every
  run happens against the conftest scratch DB and writes only --into).
"""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli as cli_group
from backupme.core.database import execute

from tests.test_archives import build_tar_gz, build_zip_bytes


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_cfg_with_chunking(env):
    """Scratch config with a small min_size so one member takes the chunked
    (standalone object blob) path while the rest stay bundled."""
    cfg = env / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    text += (
        "chunking:\n"
        "  min_size: 2048\n"
        "  chunk_size: 65536\n"
    )
    cfg.write_text(text, encoding="utf-8")


def _seed_archive(env, backends):
    _write_cfg_with_chunking(env)
    from backupme.config import load_config
    from backupme.core import archives as archives_mod

    inner = build_zip_bytes({"inner/one.txt": b"inner one",
                             "inner/two.bin": bytes(range(256))})
    members = {
        "small/a.txt": b"alpha member",
        "empty/zero.txt": b"",
        "big/blob.bin": b"B" * 8192,          # chunked: standalone object blob
        "nested/inner.zip": inner,            # nested archive member
    }
    src = env / "src.tar.gz"
    src.write_bytes(build_tar_gz(members))
    result = archives_mod.ingest_archive_file(str(src), backends, load_config())
    assert result["status"] == "archive_ingested", result
    return result


def _counts(db_path):
    con = sqlite3.connect(str(db_path))
    try:
        tables = ("files", "file_chunks", "blobs", "blob_storage",
                  "archives", "archive_members")
        out = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in tables}
        out["refs"] = dict(
            con.execute("SELECT blob_id, ref_count FROM blobs").fetchall())
        out["nested_members"] = con.execute(
            "SELECT COUNT(*) FROM archive_members "
            "WHERE nested_archive IS NOT NULL").fetchone()[0]
        out["archive_members_by_path"] = dict(con.execute(
            "SELECT am.relative_path, COUNT(*) FROM archive_members am "
            "GROUP BY am.relative_path").fetchall())
        return out
    finally:
        con.close()


def _run_recover(env, *args):
    runner = CliRunner()
    return runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "db", "recover-from-manifests", *args],
    )


# ---------------------------------------------------------------------------
# Happy path: full recovery reproduces the original vault map
# ---------------------------------------------------------------------------

def test_full_recovery_matches_original_db(env, backends):
    seeded = _seed_archive(env, backends)
    fp = seeded["fingerprint"]
    orig = _counts(env / "test.db")
    assert orig["archives"] == 2          # parent + nested zip
    assert orig["archive_members"] == 4 + 2

    # The sidecar really is on the backend (recovery reads ONLY sidecars).
    from backupme.config import load_config
    storage_root = load_config().backends["local"][0].path
    sidecar = Path(storage_root) / "manifests" / fp[:2] / fp[2:4] / f"{fp}.json"
    assert sidecar.exists()

    into = env / "recovered.db"
    result = _run_recover(env, "test_local", "--into", str(into),
                          "--verify", "full", "--json")
    assert result.exit_code == 0, result.output
    payload = json_ok(result.output)
    assert payload["status"] == "ok"
    assert payload["verify"] == "full"
    assert payload["problems"] == []
    assert payload["manifests_scanned"] == 2
    assert payload["archives_recovered"] == 2
    assert payload["members_recovered"] == 6
    assert payload["skipped_receipts"] >= 1   # per-file receipts ignored

    recovered = _counts(into)
    for t in ("files", "file_chunks", "blobs", "blob_storage",
              "archives", "archive_members"):
        assert recovered[t] == orig[t], (t, orig[t], recovered[t])
    assert recovered["refs"] == orig["refs"]
    assert recovered["nested_members"] == orig["nested_members"] == 1

    # The recovered members point at the same source roots and chunk layout.
    con = sqlite3.connect(str(into))
    try:
        roots = {r[0] for r in con.execute(
            "SELECT DISTINCT source_root FROM files").fetchall()}
        orig_con = sqlite3.connect(str(env / "test.db"))
        orig_roots = {r[0] for r in orig_con.execute(
            "SELECT DISTINCT source_root FROM files").fetchall()}
        orig_con.close()
        assert roots == orig_roots
        assert all(r.startswith("archive/") for r in roots)
        big_chunk = con.execute(
            "SELECT fc.pt_size FROM file_chunks fc "
            "JOIN files f ON f.id = fc.file_id "
            "WHERE f.relative_path = 'big/blob.bin'").fetchall()
        assert [(8192,)] == big_chunk        # single 8 KiB chunk, rebuilt
    finally:
        con.close()


def json_ok(output: str) -> dict:
    """Parse the emit()ed JSON block, tolerating any log noise before it."""
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("{"):
            return json.loads("\n".join(lines[i:]))
    raise AssertionError(f"no JSON object in output: {output[:400]!r}")


# ---------------------------------------------------------------------------
# Corruption: a missing blob becomes a problem, not silent loss
# ---------------------------------------------------------------------------

def test_missing_blob_object_reported_and_member_skipped(env, backends):
    seeded = _seed_archive(env, backends)
    fp = seeded["fingerprint"]

    # Locate the chunked member's standalone object on the backend storage
    # and delete it (scratch backend; the live system is never involved).
    chunk_blob = execute(
        "SELECT fc.blob_id FROM file_chunks fc "
        "JOIN files f ON f.id = fc.file_id "
        "WHERE f.source_root = ? AND f.relative_path = 'big/blob.bin'",
        (f"archive/{fp}",),
    ).fetchone()[0]
    from backupme.config import load_config
    storage_root = load_config().backends["local"][0].path
    deleted = None
    for suffix in (".zstd", ".gz", ".bz2", ".xz", ""):
        obj = Path(storage_root) / f"objects/{chunk_blob[:2]}/{chunk_blob[2:4]}/{chunk_blob}{suffix}"
        if obj.exists():
            obj.unlink()
            deleted = str(obj)
            break
    assert deleted, f"object for {chunk_blob} not found on scratch backend"

    into = env / "recovered_partial.db"
    result = _run_recover(env, "test_local", "--into", str(into),
                          "--verify", "full", "--json")
    assert result.exit_code == 0, result.output
    payload = json_ok(result.output)
    assert payload["status"] == "partial"
    assert payload["members_recovered"] == 5   # 6 minus the broken member
    member_problems = [p for p in payload["problems"] if p["kind"] == "member"]
    assert len(member_problems) == 1
    assert member_problems[0]["fingerprint"] == fp
    assert member_problems[0]["path"] == "big/blob.bin"
    assert chunk_blob in member_problems[0]["missing"]

    con = sqlite3.connect(str(into))
    try:
        broken_rows = con.execute(
            "SELECT COUNT(*) FROM files WHERE relative_path = 'big/blob.bin'"
        ).fetchone()[0]
        assert broken_rows == 0, "failed member must not claim a files row"
        # The archive rows (parent + nested) are still recorded.
        assert con.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 2
        # Nothing references the missing blob id.
        assert con.execute(
            "SELECT COUNT(*) FROM file_chunks WHERE blob_id = ?",
            (chunk_blob,)).fetchone()[0] == 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Safety rails
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(env, backends):
    _seed_archive(env, backends)
    into = env / "dry_run.db"
    result = _run_recover(env, "test_local", "--into", str(into),
                          "--verify", "full", "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    payload = json_ok(result.output)
    assert payload["status"] == "dry-run"
    assert payload["dry_run"] is True
    assert payload["members_recovered"] == 6
    assert not into.exists(), "dry-run must not write the sqlite file"


def test_existing_nonempty_into_file_refused(env, backends):
    _seed_archive(env, backends)
    occupied = env / "occupied.db"
    occupied.write_bytes(b"not a database")
    result = _run_recover(env, "test_local", "--into", str(occupied), "--json")
    assert result.exit_code == 1
    assert "refusing to overwrite existing non-empty file" in result.output
    assert occupied.read_bytes() == b"not a database"


def test_unknown_backend_exits_1(env, backends):
    _seed_archive(env, backends)
    result = _run_recover(env, "ghost_backend", "--into", str(env / "x.db"),
                          "--json")
    assert result.exit_code == 1
    assert "not found" in result.output


def test_verify_none_reports_and_recovers(env, backends):
    _seed_archive(env, backends)
    into = env / "recovered_none.db"
    result = _run_recover(env, "test_local", "--into", str(into),
                          "--verify", "none", "--json")
    assert result.exit_code == 0, result.output
    payload = json_ok(result.output)
    assert payload["verify"] == "none"
    assert payload["status"] == "ok"
    assert payload["members_recovered"] == 6
    recovered = _counts(into)
    assert recovered["archives"] == 2 and recovered["archive_members"] == 6

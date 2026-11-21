"""Defect-fix tests: D21 (cpanel_ingest partial member failure strands members)
and D22 (db_lifecycle cold-export month partition written non-atomically).

D21: a PARTIAL member failure must leave the archive in the drop root, mark
the op failed with per-member outcomes, and a re-ingest must retry ONLY the
previously failed members (no duplicate rows for the succeeded ones) — the
archive moves to .done and the op commits only when nothing remains failed.

D22: the month partition is written to a temp file and os.replace()d into
place; an existing corrupt/truncated partition fails loudly naming the file;
a simulated mid-write failure leaves the previous partition intact.
"""
import io
import json
import tarfile
from pathlib import Path

import pytest

from backupme.core import cpanel_ingest
from backupme.core import db_lifecycle as lc
from backupme.core.database import execute


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_tar(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _place_archive(env: Path, members: list[tuple[str, bytes]],
                   account: str = "acct") -> Path:
    archive = env / "drop" / account / f"cpmove-{account}.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(_build_tar(members))
    return archive


@pytest.fixture()
def ingest_cfg(env):
    from backupme.config import load_config
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(env / "drop")
    return cfg


def _ingest_ops(sha: str):
    return execute(
        "SELECT id, status, last_error, target_json FROM op_log "
        "WHERE idempotency_key = ?", (f"cpanel_ingest:{sha}",),
    ).fetchall()


# ---------------------------------------------------------------------------
# D21: partial member failure + resume
# ---------------------------------------------------------------------------

_MEMBERS = [
    ("homedir/index.php", b"<?php echo 1;"),
    ("homedir/config.php", b"<?php 1234567890"),
    ("mysql/db.sql", b"INSERT INTO x VALUES (1);"),
]


def _flaky_upload(fail_paths: set[str], attempted: list[str]):
    real_upload = cpanel_ingest.upload_bundle

    def wrapper(files, bks, cfg, redundancy=1):
        # upload_bundle's contract: exactly ONE result entry per input file,
        # each carrying local_path (D32 fixed the consumers to match results
        # by local_path instead of positional zip — so a whole-batch single
        # error result is no longer a valid mock shape).
        passing = []
        out = []
        for item in files:
            attempted.append(item["relative_path"])
            if item["relative_path"] in fail_paths:
                out.append({"local_path": item.get("local_path"),
                            "status": "error",
                            "error": "simulated backend outage"})
            else:
                passing.append(item)
        if passing:
            out.extend(real_upload(passing, bks, cfg, redundancy))
        return out

    return wrapper


def test_d21_partial_failure_stays_in_drop_root_and_resume_retries_only_failed(
        env, backends, ingest_cfg, monkeypatch):
    archive = _place_archive(env, _MEMBERS)
    sha = cpanel_ingest.hasher_mod.sha256_file(str(archive))
    fail_paths = {"homedir/config.php"}
    attempted: list[str] = []
    monkeypatch.setattr(cpanel_ingest, "upload_bundle",
                        _flaky_upload(fail_paths, attempted))

    # --- Run 1: one of three members fails -> partial ---------------------
    out = cpanel_ingest.ingest_tree(str(env / "drop"), backends, ingest_cfg)
    r = out["results"][0]
    assert r["status"] == "failed"
    assert "partial ingest" in r["error"]
    assert "homedir/config.php" in r["error"]

    # The archive stays in the drop root, NOT moved to .done.
    assert archive.exists()
    assert not (env / "drop" / ".done" / "acct" / "cpmove-acct.tar.gz").exists()

    # Op record: failed (not committed), partial state visible, resume
    # payload with per-member outcomes.
    ops = _ingest_ops(sha)
    assert len(ops) == 1
    assert ops[0]["status"] == "failed"
    assert "partial ingest" in ops[0]["last_error"]
    target = json.loads(ops[0]["target_json"])
    assert target["resume"] is True
    assert sorted(m["name"] for m in target["succeeded_members"]) == \
        ["homedir/index.php", "mysql/db.sql"]
    assert target["failed_members"] == ["homedir/config.php"]

    # DB: exactly the two succeeded member rows; nothing for the failed one.
    rows = execute(
        "SELECT relative_path FROM files WHERE source_root = 'cpanel/acct'"
    ).fetchall()
    assert sorted(row[0] for row in rows) == \
        ["homedir/index.php", "mysql/db.sql"]

    # --- Run 2: backend healed -> resume, retry ONLY the failed member ----
    fail_paths.clear()
    out2 = cpanel_ingest.ingest_tree(str(env / "drop"), backends, ingest_cfg)
    r2 = out2["results"][0]
    assert r2["status"] == "ok"
    assert r2["members"] == 3

    # Upload attempts prove the resume: succeeded members were NOT
    # re-uploaded, only the previously failed one was retried.
    assert attempted == [
        "homedir/index.php", "homedir/config.php", "mysql/db.sql",  # run 1
        "homedir/config.php",                                       # run 2
    ]

    # No duplicate rows: one version per member, all at version_seq=1.
    counts = execute(
        "SELECT relative_path, COUNT(*) FROM files "
        "WHERE source_root = 'cpanel/acct' GROUP BY relative_path"
    ).fetchall()
    assert sorted((row[0], row[1]) for row in counts) == [
        ("homedir/config.php", 1),
        ("homedir/index.php", 1),
        ("mysql/db.sql", 1),
    ]
    assert execute(
        "SELECT COUNT(*) FROM files "
        "WHERE source_root = 'cpanel/acct' AND version_seq > 1"
    ).fetchone()[0] == 0

    # Same single op row, now committed; the partial history stays visible.
    ops2 = _ingest_ops(sha)
    assert len(ops2) == 1
    assert ops2[0]["id"] == ops[0]["id"]
    assert ops2[0]["status"] == "committed"
    assert "partial ingest" in ops2[0]["last_error"]

    # Generation manifest now lists all 3 members; archive moved to .done.
    manifest_path = env / "manifest" / f"{sha}.json"
    mdata = json.loads(manifest_path.read_text())
    assert sorted(m["name"] for m in mdata["members"]) == \
        ["homedir/config.php", "homedir/index.php", "mysql/db.sql"]
    assert not archive.exists()
    assert (env / "drop" / ".done" / "acct" / "cpmove-acct.tar.gz").exists()


def test_d21_all_members_failed_op_failed_then_full_retry_succeeds(
        env, backends, ingest_cfg, monkeypatch):
    members = [("homedir/a.php", b"<?php a;"), ("mysql/b.sql", b"SELECT 1;")]
    archive = _place_archive(env, members)
    sha = cpanel_ingest.hasher_mod.sha256_file(str(archive))
    fail_paths = {"homedir/a.php", "mysql/b.sql"}
    attempted: list[str] = []
    monkeypatch.setattr(cpanel_ingest, "upload_bundle",
                        _flaky_upload(fail_paths, attempted))

    # Run 1: every member fails -> op failed, archive stays (2026-11-07
    # behavior preserved).
    out = cpanel_ingest.ingest_tree(str(env / "drop"), backends, ingest_cfg)
    r = out["results"][0]
    assert r["status"] == "failed"
    assert f"all {len(members)} member(s)" in r["error"]
    assert archive.exists()
    assert not (env / "drop" / ".done" / "acct").exists()

    ops = _ingest_ops(sha)
    assert len(ops) == 1
    assert ops[0]["status"] == "failed"
    target = json.loads(ops[0]["target_json"])
    assert target["resume"] is True
    assert target["succeeded_members"] == []
    assert sorted(target["failed_members"]) == ["homedir/a.php", "mysql/b.sql"]
    assert execute(
        "SELECT COUNT(*) FROM files WHERE source_root = 'cpanel/acct'"
    ).fetchone()[0] == 0

    # Run 2: retry (no resume skips — nothing had succeeded) -> complete.
    fail_paths.clear()
    out2 = cpanel_ingest.ingest_tree(str(env / "drop"), backends, ingest_cfg)
    assert out2["results"][0]["status"] == "ok"
    assert out2["results"][0]["members"] == 2
    rows = execute(
        "SELECT relative_path FROM files WHERE source_root = 'cpanel/acct'"
    ).fetchall()
    assert sorted(row[0] for row in rows) == ["homedir/a.php", "mysql/b.sql"]
    ops2 = _ingest_ops(sha)
    assert len(ops2) == 1 and ops2[0]["status"] == "committed"
    assert not archive.exists()
    assert (env / "drop" / ".done" / "acct" / "cpmove-acct.tar.gz").exists()


# ---------------------------------------------------------------------------
# D22: atomic cold-export partition write
# ---------------------------------------------------------------------------

def _seed_file(env, backends, rel: str, data: bytes) -> int:
    from tests.helpers import backup_bytes
    return backup_bytes(env, rel, data, backends)["file_id"]


def test_d22_cold_export_corrupt_partition_fails_loudly_naming_file(
        env, backends):
    pytest.importorskip("pyarrow")
    _seed_file(env, backends, "cold/a.txt", b"alpha content")
    _seed_file(env, backends, "cold/b.txt", b"beta content")
    first = lc.cold_export_to_parquet()
    assert first["status"] == "ok"
    pdir = env / "parquet"
    partition = pdir / first["partition"]
    assert partition.exists()

    # Simulate a truncated/corrupt partition file.
    corruption = b"CORRUPTED-NOT-A-PARQUET"
    partition.write_bytes(corruption)

    # A new row above the watermark forces a re-read of the corrupt file.
    _seed_file(env, backends, "cold/c.txt", b"gamma content")
    with pytest.raises(RuntimeError) as excinfo:
        lc.cold_export_to_parquet()
    # Clear error naming the offending file.
    assert first["partition"] in str(excinfo.value)
    assert "unreadable" in str(excinfo.value)

    # The corrupt file was NOT silently deleted or overwritten — the
    # operator decides what to do with it.
    assert partition.read_bytes() == corruption
    # Watermark manifest untouched (export aborted before advancing it).
    manifest = json.loads((pdir / "_manifest.json").read_text())
    assert manifest["watermark"] == first["watermark"]


def test_d22_cold_export_midwrite_failure_leaves_previous_partition_intact(
        env, backends, monkeypatch):
    pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    _seed_file(env, backends, "cold/a.txt", b"alpha content")
    _seed_file(env, backends, "cold/b.txt", b"beta content")
    first = lc.cold_export_to_parquet()
    assert first["status"] == "ok"
    partition = env / "parquet" / first["partition"]
    rows_before = first["total_rows"]

    # Simulate a crash mid-write: partial bytes hit the TEMP file, then die.
    def boom(table, path, *args, **kwargs):
        Path(str(path)).write_bytes(b"truncated-not-a-parquet")
        raise RuntimeError("simulated crash mid-write")

    orig_write_table = pq.write_table
    monkeypatch.setattr(pq, "write_table", boom)
    _seed_file(env, backends, "cold/c.txt", b"gamma content")
    with pytest.raises(RuntimeError, match="simulated crash mid-write"):
        lc.cold_export_to_parquet()
    # Restore ONLY write_table — monkeypatch.undo() would also revert the
    # env fixture's chdir, sending the re-run export into the wrong CWD.
    monkeypatch.setattr(pq, "write_table", orig_write_table)

    # The previous partition is intact and readable; no .tmp litter; the
    # watermark manifest was not advanced.
    assert pq.read_table(str(partition)).num_rows == rows_before
    assert list((env / "parquet").glob("*.tmp")) == []
    manifest = json.loads((env / "parquet" / "_manifest.json").read_text())
    assert manifest["watermark"] == first["watermark"]

    # Re-run after the "crash" (write_table restored): the export succeeds,
    # the previous partition content is preserved and the new row appended.
    second = lc.cold_export_to_parquet()
    assert second["status"] == "ok"
    assert second["partition"] == first["partition"]
    assert second["total_rows"] == rows_before + 1
    assert second["watermark"] > first["watermark"]
    assert pq.read_table(str(partition)).num_rows == rows_before + 1

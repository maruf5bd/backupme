"""D19/D20 regression tests: the scheduler retention pass and
migration.migrate_inline_files must run inside an explicit write_txn —
no ambient open transaction left behind on failure, all-or-nothing units
of work, and durable commits on the happy path."""
import datetime
import hashlib

import pytest

from backupme.core.database import execute, commit, get_connection
from backupme.core import dedup as dedup_mod
from tests.test_scheduler_automation import _seed_blob, _seed_file


def _past_deletable(now: datetime.datetime, days_ago: int) -> datetime.datetime:
    """A deterministic date `days_ago` back that no retention branch protects:
    not a Monday (weekly branch) and day-of-month > 7 (monthly branch)."""
    d = now - datetime.timedelta(days=days_ago)
    while d.weekday() == 0 or d.day <= 7:
        d -= datetime.timedelta(days=1)
    return d


_RETENTION_CFG = {"retention": {"enabled": True, "versions": 2,
                                "daily": 7, "weekly": 4, "monthly": 12}}


# ---------------------------------------------------------------------------
# D19: scheduler.apply_retention transactional retention pass
# ---------------------------------------------------------------------------

def test_retention_mid_pass_failure_rolls_back_all_mutations(env, monkeypatch):
    """A failure between two blob unrefs must roll back the ENTIRE pass —
    the earlier soft-delete, chunk deletion, and unref included — and leave
    no open write transaction on the pooled connection."""
    from backupme.scheduler import apply_retention
    now = datetime.datetime.now()
    old = _past_deletable(now, 40)

    _seed_blob("blob-a", 1)
    _seed_blob("blob-b", 1)
    _seed_file("vgd", 1, old, "blob-a")
    _seed_file("vgd", 2, old, "blob-b")
    _seed_file("vgd", 3, now)
    _seed_file("vgd", 4, now)
    commit()

    original_unref = dedup_mod.unref_blob
    calls = {"n": 0}

    def exploding_unref(blob_id, commit_now=True):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated DB failure mid-retention")
        return original_unref(blob_id, commit_now=commit_now)

    monkeypatch.setattr(dedup_mod, "unref_blob", exploding_unref)

    with pytest.raises(RuntimeError):
        apply_retention(_RETENTION_CFG)

    assert calls["n"] == 2
    # Neither version may appear soft-deleted: the first unref's soft-delete +
    # chunk removal rolled back with the second (single atomic pass).
    assert execute(
        "SELECT COUNT(*) FROM files WHERE version_group='vgd' "
        "AND deleted_at IS NOT NULL").fetchone()[0] == 0
    assert execute(
        "SELECT COUNT(*) FROM file_chunks WHERE blob_id IN ('blob-a','blob-b')"
    ).fetchone()[0] == 2
    for blob_id in ("blob-a", "blob-b"):
        row = execute("SELECT ref_count FROM blobs WHERE blob_id=?",
                      (blob_id,)).fetchone()
        assert row and row[0] == 1, blob_id
        assert execute("SELECT 1 FROM blob_storage WHERE blob_id=?",
                       (blob_id,)).fetchone()
    # The pooled connection must NOT be left inside an open write transaction.
    assert not get_connection().in_transaction


def test_retention_happy_path_commits_and_persists(env):
    """The retention pass must commit once atomically: soft-deletes and blob
    unrefs persist and survive a fresh connection."""
    from backupme.scheduler import apply_retention
    now = datetime.datetime.now()
    old = _past_deletable(now, 40)

    _seed_blob("blob-v1", 1)        # owned by the deleted v1 only
    _seed_blob("blob-keep", 2)      # v1 (deleted) + v2 (kept)
    _seed_file("vgh", 1, old, "blob-v1")
    execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
            "SELECT id, 1, 'blob-keep', 0, 10 FROM files "
            "WHERE version_group='vgh' AND version_seq=1")
    _seed_file("vgh", 2, now, "blob-keep")
    _seed_file("vgh", 3, now)
    commit()

    apply_retention(_RETENTION_CFG)

    assert execute(
        "SELECT COUNT(*) FROM files WHERE version_group='vgh' "
        "AND deleted_at IS NOT NULL").fetchone()[0] == 1
    assert not execute("SELECT 1 FROM blobs WHERE blob_id='blob-v1'").fetchone()
    row = execute("SELECT ref_count FROM blobs WHERE blob_id='blob-keep'").fetchone()
    assert row and row[0] == 1
    assert not get_connection().in_transaction

    # Prove the changes are durable, not just visible on the same connection.
    from backupme.core import database as dbmod
    dbmod.close_connection()
    assert not execute("SELECT 1 FROM blobs WHERE blob_id='blob-v1'").fetchone()
    row = execute("SELECT ref_count FROM blobs WHERE blob_id='blob-keep'").fetchone()
    assert row and row[0] == 1
    assert execute(
        "SELECT COUNT(*) FROM files WHERE version_group='vgh' "
        "AND deleted_at IS NOT NULL").fetchone()[0] == 1


def test_retention_tick_guard_catches_rolled_back_failure(env, monkeypatch, caplog):
    """A mid-pass failure must propagate to the _run_retention tick guard,
    which logs it — and the connection must stay usable for the next unit."""
    from backupme.scheduler import BackupScheduler, apply_retention
    now = datetime.datetime.now()
    old = _past_deletable(now, 40)

    _seed_blob("blob-t", 1)
    _seed_file("vgt", 1, old, "blob-t")
    _seed_file("vgt", 2, now)
    _seed_file("vgt", 3, now)  # 3 versions so v1 falls outside the keep set
    commit()

    original_unref = dedup_mod.unref_blob

    def exploding_unref(blob_id, commit_now=True):
        raise RuntimeError("simulated retention failure")

    monkeypatch.setattr(dedup_mod, "unref_blob", exploding_unref)

    s = BackupScheduler({"backups": [], "retention": _RETENTION_CFG["retention"]})
    with caplog.at_level("ERROR", logger="backupme.scheduler"):
        s._run_retention()  # the guard must swallow the failure

    assert any("retention pass failed" in rec.getMessage()
               for rec in caplog.records)
    assert not get_connection().in_transaction
    assert execute(
        "SELECT COUNT(*) FROM files WHERE version_group='vgt' "
        "AND deleted_at IS NOT NULL").fetchone()[0] == 0

    # The rolled-back connection stays usable for the next unit of work.
    monkeypatch.setattr(dedup_mod, "unref_blob", original_unref)
    apply_retention(_RETENTION_CFG)
    assert not execute("SELECT 1 FROM blobs WHERE blob_id='blob-t'").fetchone()


# ---------------------------------------------------------------------------
# D20: migration.migrate_inline_files transactional rows
# ---------------------------------------------------------------------------

def _seed_inline(rel: str, content_text: str, sha256: str) -> int:
    execute(
        """INSERT INTO files (source_root, relative_path, sha256, size, version_group, content_text)
           VALUES ('r', ?, ?, ?, 'g', ?)""",
        (rel, sha256, len(content_text), content_text),
    )
    commit()
    return execute("SELECT last_insert_rowid()").fetchone()[0]


def test_migrate_inline_files_failure_rolls_back_row_mutations(env, backends,
                                                               monkeypatch):
    """A failure in a row's chunk replacement must roll back that row's
    ensure_blob, blob_storage, chunk-mapping, and files mutations; previously
    migrated rows keep their committed per-row transaction."""
    from backupme.core import migration
    from backupme.core import uploader as uploader_mod

    text1 = "first inline row content " * 5
    text2 = "second inline row content " * 5
    fid1 = _seed_inline("one.txt", text1, "c" * 64)
    fid2 = _seed_inline("two.txt", text2, "d" * 64)
    sha2 = hashlib.sha256(text2.encode("utf-8")).hexdigest()

    original_replace = uploader_mod._replace_file_chunks
    calls = {"n": 0}

    def exploding_replace(file_id, desired):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated DB failure mid-migration")
        return original_replace(file_id, desired)

    monkeypatch.setattr(uploader_mod, "_replace_file_chunks", exploding_replace)

    with pytest.raises(RuntimeError):
        migration.migrate_inline_files(backends)

    assert calls["n"] == 2
    # Row 1's completed unit committed (per-row transaction).
    row1 = execute("SELECT migrated, content_text FROM files WHERE id=?",
                   (fid1,)).fetchone()
    assert row1["migrated"] == 1 and row1["content_text"] is None
    # Row 2's ensure_blob / storage rows / chunk mapping / UPDATE rolled back.
    assert execute("SELECT 1 FROM blobs WHERE blob_id=?", (sha2,)).fetchone() is None
    assert execute("SELECT 1 FROM blob_storage WHERE blob_id=?",
                   (sha2,)).fetchone() is None
    assert execute("SELECT 1 FROM file_chunks WHERE file_id=?", (fid2,)).fetchone() is None
    row2 = execute("SELECT content_text, migrated, sha256 FROM files WHERE id=?",
                   (fid2,)).fetchone()
    assert row2["content_text"] == text2
    assert row2["migrated"] == 0
    assert row2["sha256"] == "d" * 64
    # The pooled connection must NOT be left inside an open write transaction.
    assert not get_connection().in_transaction

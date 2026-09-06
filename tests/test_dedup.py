import pytest

from backupme.core import dedup
from backupme.core.database import execute, commit


def _chunk_ref(file_id, blob_id, seq=0):
    execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, 0, 1)",
            (file_id, seq, blob_id))


def _file(label) -> int:
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, version_group) VALUES ('r', ?, ?, 1, 'g')",
        (f"f{label}", "x" * 64),
    )
    commit()
    return execute("SELECT last_insert_rowid()").fetchone()[0]


def test_ensure_blob_is_idempotent_and_does_not_ref():
    dedup.ensure_blob("b1", 10)
    dedup.ensure_blob("b1", 10)
    row = execute("SELECT ref_count FROM blobs WHERE blob_id = 'b1'").fetchone()
    assert row[0] == 0


def test_add_ref_and_unref_floor_at_zero():
    dedup.ensure_blob("b2", 5)
    dedup.add_ref("b2")
    dedup.add_ref("b2")
    assert dedup.unref_blob("b2") == 1
    assert dedup.unref_blob("b2") == 0
    # Blob deleted at zero...
    assert execute("SELECT COUNT(*) FROM blobs WHERE blob_id = 'b2'").fetchone()[0] == 0
    # ...and further unrefs are safe no-ops.
    assert dedup.unref_blob("b2") == 0


def test_unref_cascades_to_parent_bundle_blob():
    dedup.ensure_blob("bundle/aa/bb/cc", 100)
    member = "bundle/aa/bb/cc/member1"
    dedup.ensure_blob(member, 10)
    dedup.add_ref("bundle/aa/bb/cc")
    fid = _file(1)
    _chunk_ref(fid, member)
    dedup.add_ref(member)
    # Refcounts are consistent (bundle: 1 member-ref; member: 1 chunk-ref)
    assert dedup.reconcile_refcounts() == 0
    # Prune the file -> member unref -> member deleted -> cascade unrefs bundle
    execute("DELETE FROM file_chunks WHERE file_id = ?", (fid,))
    commit()
    dedup.unref_blob(member)
    assert execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0


def test_reconcile_fixes_drift():
    dedup.ensure_blob("b3", 7)
    fid = _file(2)
    _chunk_ref(fid, "b3")
    commit()
    execute("UPDATE blobs SET ref_count = 99 WHERE blob_id = 'b3'")
    commit()
    assert dedup.reconcile_refcounts() == 1
    assert execute("SELECT ref_count FROM blobs WHERE blob_id = 'b3'").fetchone()[0] == 1


def test_find_unrecoverable_lists_missing_blob_rows():
    from backupme.core.database import get_connection
    fid = _file(3)
    # Legacy broken rows exist because constraints were bypassed historically;
    # reproduce that here.
    conn = get_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    _chunk_ref(fid, "ghost-blob")
    conn.execute("PRAGMA foreign_keys = ON")
    commit()
    out = dedup.find_unrecoverable()
    assert out and out[0]["file_id"] == fid and out[0]["blob_id"] == "ghost-blob"

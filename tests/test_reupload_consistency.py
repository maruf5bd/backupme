"""Re-upload consistency (P0): a changed file re-backed through the real
pipeline must leave files.sha256 and file_chunks in agreement.

The old behavior hit INSERT OR IGNORE at every chunk-write site: index_file
updates the row in place (it matches on source_root+relative_path+version_seq
and callers always pass version_seq=1), so the new chunk row conflicted with
the stale one and was silently dropped — restores produced the PREVIOUS
content under the NEW sha ("Integrity mismatch"), and the new blob carried a
phantom ref (add_ref without a chunk row).
"""
from pathlib import Path

import pytest

from backupme.core import dedup as dedup_mod
from backupme.core.database import execute
from backupme.core.verify import run_integrity_check
from tests.helpers import backup_bytes, restore_bytes


def _refcount(blob_id):
    row = execute("SELECT ref_count FROM blobs WHERE blob_id = ?", (blob_id,)).fetchone()
    return row[0] if row else None


def test_changed_reupload_restores_new_content(env, backends):
    v1 = backup_bytes(Path(env), "seed/doc.txt", b"version one", backends)
    v2 = backup_bytes(Path(env), "seed/doc.txt", b"version two!", backends)
    assert v1["file_id"] == v2["file_id"]  # same (source_root, path, version_seq) row
    assert restore_bytes(v2["file_id"], Path(env)) == b"version two!"

    # The displaced v1 blob must not linger with a phantom reference.
    assert dedup_mod.reconcile_refcounts() == 0
    rep = run_integrity_check(count=5)
    assert rep["failed"] == 0, rep


def test_identical_reupload_no_refcount_drift(env, backends):
    data = b"same content " * 20
    r1 = backup_bytes(Path(env), "seed/same.txt", data, backends)
    r2 = backup_bytes(Path(env), "seed/same.txt", data, backends)
    assert r1["file_id"] == r2["file_id"]
    assert restore_bytes(r2["file_id"], Path(env)) == data
    assert dedup_mod.reconcile_refcounts() == 0
    blob_id = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ?", (r2["file_id"],)
    ).fetchone()[0]
    assert _refcount(blob_id) == 1  # not inflated by the second pass


def test_changed_chunked_reupload_restores_new_content(env, backends):
    big1 = bytes(range(256)) * (12 * 1024 * 1024 // 256)
    big2 = bytes((range(256)[::-1])) * (12 * 1024 * 1024 // 256)
    v1 = backup_bytes(Path(env), "seed/large.bin", big1, backends)
    v2 = backup_bytes(Path(env), "seed/large.bin", big2, backends)
    assert v1["file_id"] == v2["file_id"]
    assert restore_bytes(v2["file_id"], Path(env)) == big2
    assert dedup_mod.reconcile_refcounts() == 0


def test_donor_path_replaces_stale_chunks(env, backends):
    x = b"content X" * 30
    y = b"content Y" * 30
    backup_bytes(Path(env), "seed/a.txt", x, backends)
    b1 = backup_bytes(Path(env), "seed/b.txt", y, backends)
    # b.txt now matches a.txt's content: the dedup donor path serves the new
    # chunk mapping, and b's stale Y-chunks must not survive it.
    b2 = backup_bytes(Path(env), "seed/b.txt", x, backends)
    assert b1["file_id"] == b2["file_id"]
    assert restore_bytes(b2["file_id"], Path(env)) == x
    assert dedup_mod.reconcile_refcounts() == 0

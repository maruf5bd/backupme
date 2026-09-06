import pytest

import backupme.core.uploader as uploader_mod
from backupme.core import dedup
from backupme.core.database import execute, commit
from tests.helpers import backup_bytes, restore_bytes, add_backend, load_cfg
from backupme.core.uploader import upload_bundle


def test_failed_uploads_leave_no_trace(env, backends, monkeypatch):
    real_upload = uploader_mod._do_upload
    backup_bytes(env, "seed.txt", b"seed " * 100, backends)
    files_before = execute("SELECT COUNT(*) FROM files").fetchone()[0]
    blobs_before = execute("SELECT COUNT(*) FROM blobs").fetchone()[0]

    monkeypatch.setattr(uploader_mod, "_do_upload", lambda bk, lp, rp: False)
    r = backup_bytes(env, "doomed.txt", b"doomed " * 100, backends)
    assert r["status"] == "error" and "error" in r
    assert execute("SELECT COUNT(*) FROM files").fetchone()[0] == files_before
    assert execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == blobs_before

    monkeypatch.setattr(uploader_mod, "_do_upload", real_upload)
    r2 = backup_bytes(env, "doomed.txt", b"doomed " * 100, backends)
    assert r2["status"] == "ok"
    assert restore_bytes(r2["file_id"], env) == b"doomed " * 100


def test_partial_redundancy_reported_and_restorable(env, backends, monkeypatch, tmp_path):
    add_backend(env, "second", tmp_path / "storage2")
    from backupme.backends import get_all_backends
    backends2 = get_all_backends(load_cfg())
    real = uploader_mod._do_upload

    def flaky(bk, lp, rp):
        return False if bk.name == "second" else real(bk, lp, rp)

    monkeypatch.setattr(uploader_mod, "_do_upload", flaky)
    r = backup_bytes(env, "part.txt", b"partial " * 100, backends2, redundancy=2)
    assert r["status"] == "partial"
    assert r["failed_backends"] == ["second"]
    monkeypatch.setattr(uploader_mod, "_do_upload", real)
    assert restore_bytes(r["file_id"], env) == b"partial " * 100


def test_every_file_gets_exactly_one_result(env, backends):
    from backupme.core.uploader import upload_bundle
    infos = []
    for i in range(4):
        p = env / f"m{i}.txt"
        p.write_bytes(b"x" * (50 + i))
        infos.append({"local_path": str(p), "source_root": str(env), "relative_path": f"m{i}.txt"})
    results = upload_bundle(infos, backends, load_cfg(), 1)
    assert len(results) == 4
    assert all(("file_id" in r) or (r.get("status") == "error") for r in results)


def test_manifest_written_once_per_backend(env, backends):
    backup_bytes(env, "b1.txt", b"b1 " * 50, backends)
    backup_bytes(env, "b2.txt", b"b2 " * 50, backends)
    # One backend, two bundled files -> exactly 2 manifests + 2 pointers on disk
    storage = env / "storage"
    manifests = [p for p in storage.rglob("*.manifest.json")]
    pointers = [p for p in storage.rglob("*.backupme")]
    assert len(manifests) == 2
    assert len(pointers) == 2


def test_refcount_invariant_holds_after_backup(env, backends):
    backup_bytes(env, "i1.txt", b"invariant " * 100, backends)
    backup_bytes(env, "i2.txt", bytes(range(64)) * 30, backends)
    assert dedup.reconcile_refcounts() == 0


def test_prune_original_keeps_dedup_copy_restorable(env, backends):
    data = b"prunable " * 300
    r1 = backup_bytes(env, "orig.txt", data, backends)
    r2 = backup_bytes(env, "copy.txt", data, backends)
    assert r2.get("dedup") is True
    # Simulate retention pruning of the original: remove its chunk rows, unref blobs.
    chunks = execute("SELECT blob_id FROM file_chunks WHERE file_id = ?", (r1["file_id"],)).fetchall()
    execute("DELETE FROM file_chunks WHERE file_id = ?", (r1["file_id"],))
    commit()
    for (blob_id,) in chunks:
        dedup.unref_blob(blob_id)
    assert restore_bytes(r2["file_id"], env) == data

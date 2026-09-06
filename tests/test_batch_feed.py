import dataclasses

import pytest

from backupme.core.database import execute
from backupme.core import dedup, uploader as uploader_mod
from tests.helpers import load_cfg


def _scheduler_config(env):
    cfg = dataclasses.asdict(load_cfg())
    cfg["backups"] = [{"source": str(env)}]
    return cfg


def test_run_backups_uses_one_bundle_for_many_small_files(env, backends):
    from backupme.scheduler import BackupScheduler
    for i in range(300):
        (env / f"s{i:03}.txt").write_bytes(b"batch " * 20)
    sched = BackupScheduler(_scheduler_config(env))
    sched._run_backups()
    drift = execute(
        """SELECT blob_id, ref_count,
                  (SELECT COUNT(*) FROM file_chunks WHERE file_chunks.blob_id = blobs.blob_id) AS c,
                  (SELECT COUNT(*) FROM blobs m WHERE m.blob_id LIKE blobs.blob_id || '/%') AS m
           FROM blobs
           WHERE ref_count != (SELECT COUNT(*) FROM file_chunks WHERE file_chunks.blob_id = blobs.blob_id)
                            + (SELECT COUNT(*) FROM blobs m WHERE m.blob_id LIKE blobs.blob_id || '/%')"""
    ).fetchall()
    print("DEBUG DRIFT:", [dict(r) for r in drift])
    # Bundle blobs are 'bundle/aa/bb/<sha>' (3 segments); members have 4.
    bundles = execute(
        "SELECT COUNT(*) FROM blobs WHERE blob_id LIKE 'bundle/%' AND blob_id NOT LIKE 'bundle/%/%/%/%'"
    ).fetchone()[0]
    assert 1 <= bundles <= 3
    assert dedup.reconcile_refcounts() == 0


def test_run_backups_second_run_is_noop(env, backends):
    from backupme.scheduler import BackupScheduler
    (env / "once.txt").write_bytes(b"once " * 100)
    sched = BackupScheduler(_scheduler_config(env))
    sched._run_backups()
    files_before = execute("SELECT COUNT(*) FROM files").fetchone()[0]
    results_seen = []
    real = uploader_mod.upload_bundle
    import backupme.scheduler as sched_mod

    def spy(infos, *a, **kw):
        results_seen.append(list(infos))
        return real(infos, *a, **kw)

    sched_mod.uploader_mod.upload_bundle = spy
    try:
        sched._run_backups()
    finally:
        sched_mod.uploader_mod.upload_bundle = real
    assert results_seen == [] or all(len(b) == 0 for b in results_seen)
    assert execute("SELECT COUNT(*) FROM files").fetchone()[0] == files_before


def test_run_backups_survives_batch_failure(env, backends, monkeypatch):
    from backupme.scheduler import BackupScheduler
    for i in range(5):
        (env / f"f{i}.txt").write_bytes(b"data " * 10)
    sched = BackupScheduler(_scheduler_config(env))
    monkeypatch.setattr(uploader_mod, "upload_bundle",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    sched._run_backups()  # must not raise

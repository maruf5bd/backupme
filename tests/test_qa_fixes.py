"""Regression tests for QA concurrency findings: cross-process locks for cold
export and ingest scans, per-target rebalance checkpoints, daemon loop guard."""
import io
import json
import os
import tarfile
import time
from pathlib import Path

from backupme.core import cpanel_ingest as ci
from backupme.core import db_lifecycle as lc
from backupme.core import rebalance as rb
from backupme.core import state as state_mod
from backupme.core.database import execute
from backupme.core.flock import path_lock
from tests.helpers import backup_bytes
from tests.test_cpanel_ingest import _build_tar_bytes


def _two_backends(env):
    from tests.helpers import add_backend
    add_backend(env, "second", env.parent / "storage2")
    from backupme.config import load_config
    from backupme.backends import get_all_backends
    return get_all_backends(load_config())


def _drop(env, blob=None):
    d = env / "drop" / "acct"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cpmove-acct.tar.gz").write_bytes(blob or _build_tar_bytes())
    return d / "cpmove-acct.tar.gz"


def _cfg(env):
    from backupme.config import load_config
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(env / "drop")
    cfg.cpanel_ingest.staging_dir = str(env / "staging")
    return cfg


def test_cold_export_busy_when_lock_held(env, backends):
    backup_bytes(env, "lx/a.txt", b"lock probe", backends)
    pdir = lc.parquet_dir_for(_cfg(env))
    with path_lock(pdir / "_export.lock"):
        result = lc.cold_export_to_parquet()
    assert result["status"] == "busy"
    # Watermark untouched.
    assert lc.load_watermark(pdir) == 0


def test_cold_export_takes_over_stale_lock(env, backends):
    backup_bytes(env, "lx/b.txt", b"stale probe", backends)
    pdir = lc.parquet_dir_for(_cfg(env))
    pdir.mkdir(parents=True, exist_ok=True)
    lock = pdir / "_export.lock"
    lock.write_text("999999")
    two_hours_ago = time.time() - 7200
    os.utime(lock, (two_hours_ago, two_hours_ago))
    result = lc.cold_export_to_parquet()
    assert result["status"] == "ok" and result["rows"] >= 1


def test_cold_export_releases_lock_after_run(env, backends):
    backup_bytes(env, "lx/c.txt", b"release probe", backends)
    lc.cold_export_to_parquet()
    assert not (lc.parquet_dir_for(_cfg(env)) / "_export.lock").exists()


def test_ingest_tree_busy_when_lock_held(env, backends):
    _drop(env)
    staging = env / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    with path_lock(staging / ".ingest.lock"):
        out = ci.ingest_tree(str(env / "drop"), backends, _cfg(env))
    assert out["status"] == "busy"
    n = execute("SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%'").fetchone()[0]
    assert n == 0


def test_ingest_tree_lock_released_after_run(env, backends):
    _drop(env)
    out = ci.ingest_tree(str(env / "drop"), backends, _cfg(env))
    assert out["status"] == "ok"
    assert not (env / "staging" / ".ingest.lock").exists()


def test_rebalance_checkpoints_are_per_target(env):
    """Blobs completed for target A must not be skipped when rebalancing to B."""
    backends = _two_backends(env)
    backup_bytes(env, "ck/x.txt", b"per-target probe", backends)
    out_a = rb.rebalance_to("second", backends, _cfg(env))
    assert out_a["status"] == "ok"
    ck_a = json.loads(Path("rebalance_checkpoint_second.json").read_text())
    assert ck_a["completed"]
    # Rebalancing to a different target uses its own checkpoint (none yet).
    out_b = rb.rebalance_to("test_local", backends, _cfg(env))
    assert out_b["status"] == "ok"
    assert Path("rebalance_checkpoint_test_local.json").exists()
    assert not Path("rebalance_checkpoint.json").exists()


def test_rebalance_busy_when_lock_held(env):
    backends = _two_backends(env)
    backup_bytes(env, "ck/y.txt", b"busy probe", backends)
    lock = Path("rebalance_checkpoint_second.lock")
    with path_lock(lock):
        out = rb.rebalance_to("second", backends, _cfg(env))
    assert out["status"] == "busy"


def test_scheduler_loop_survives_tick_crash(env):
    """H5: an exception inside one pass must not kill the daemon thread."""
    from backupme.scheduler import BackupScheduler
    s = BackupScheduler({"backends": {}})

    calls = {"passes": 0}
    original_backups = BackupScheduler._run_backups

    def exploding_backups(self):
        calls["passes"] += 1
        raise RuntimeError("simulated crash")

    BackupScheduler._run_backups = exploding_backups
    try:
        # Patched wait: first call -> run another pass; second -> stop.
        n = {"wait": 0}

        def wait_once(x):
            n["wait"] += 1
            if n["wait"] >= 2:
                s._stop.set()
            return False

        s._stop.wait = wait_once
        BackupScheduler._periodic_loop(s)
    finally:
        BackupScheduler._run_backups = original_backups
        s._stop.set()
    assert calls["passes"] == 2  # crashed once, loop continued

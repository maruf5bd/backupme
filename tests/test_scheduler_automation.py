"""Tests for Plan 03 daemon automation: cadence ticks, single-flight guards,
staging budget gate, and scheduler config parsing."""
import tarfile
import time
from pathlib import Path

from backupme.core import cpanel_ingest as ci
from backupme.core.database import execute, commit
from tests.test_cpanel_ingest import _build_tar_bytes


def _make_cfg(env, **ingest_overrides):
    """Build a dict config (as the daemon passes to BackupScheduler) with a
    local backend and cpanel_ingest enabled against env/drop."""
    from backupme.config import load_config
    import dataclasses
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(env / "drop")
    for k, v in ingest_overrides.items():
        setattr(cfg.cpanel_ingest, k, v)
    return dataclasses.asdict(cfg)


def _drop_archive(env):
    drop = env / "drop" / "acct"
    drop.mkdir(parents=True, exist_ok=True)
    arch = drop / "cpmove-acct.tar.gz"
    with open(arch, "wb") as f:
        f.write(_build_tar_bytes())
    return arch


def test_sched_defaults_and_overrides(env):
    from backupme.scheduler import BackupScheduler
    s = BackupScheduler({"backends": {}})
    sc = s._sched()
    assert sc["interval"] == 3600
    assert sc["ingest_interval"] == 300
    assert sc["gc_interval_days"] == 7
    assert sc["verify_interval_days"] == 1
    assert sc["purge_grace_days"] == 14
    assert sc["cold_export_after_days"] == 30

    s2 = BackupScheduler({
        "scheduler": {"interval": 60, "ingest_interval": 30,
                      "gc_interval_days": 2, "verify_interval_days": 3},
        "db_lifecycle": {"purge_grace_days": 5, "cold_export_after_days": 10},
    })
    sc2 = s2._sched()
    assert sc2["interval"] == 60
    assert sc2["ingest_interval"] == 30
    assert sc2["gc_interval_days"] == 2
    assert sc2["verify_interval_days"] == 3
    assert sc2["purge_grace_days"] == 5
    assert sc2["cold_export_after_days"] == 10

    # Garbage values fall back to defaults.
    s3 = BackupScheduler({"scheduler": {"interval": "abc"}})
    assert s3._sched()["interval"] == 3600


def test_run_ingest_processes_drop_root(env, backends):
    from backupme.scheduler import BackupScheduler
    cfg = _make_cfg(env)
    _drop_archive(env)
    s = BackupScheduler(cfg)
    s._run_ingest()
    n = execute("SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'").fetchone()[0]
    assert n == 3


def test_run_ingest_noop_when_disabled(env, backends):
    from backupme.scheduler import BackupScheduler
    import dataclasses
    cfg = _make_cfg(env)
    cfg["cpanel_ingest"]["enabled"] = False
    _drop_archive(env)
    s = BackupScheduler(cfg)
    s._run_ingest()
    n = execute("SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%'").fetchone()[0]
    assert n == 0


def test_ingest_single_flight_skips_when_locked(env, backends):
    from backupme.scheduler import BackupScheduler
    cfg = _make_cfg(env)
    _drop_archive(env)
    s = BackupScheduler(cfg)
    # Hold the ingest lock: a concurrent tick must skip, not block or error.
    with s._ingest_lock:
        s._run_ingest()
    n = execute("SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%'").fetchone()[0]
    assert n == 0
    # Once released, the tick proceeds.
    s._run_ingest()
    n = execute("SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'").fetchone()[0]
    assert n == 3


def test_purge_and_export_ticks_gated_and_single_flight(env, backends):
    from backupme.scheduler import BackupScheduler
    cfg = _make_cfg(env)
    s = BackupScheduler(cfg)

    calls = {"purge": 0, "export": 0}
    import backupme.core.db_lifecycle as lc

    def fake_purge(config=None):
        calls["purge"] += 1
        return {"purged": 0, "skipped": 0, "watermark": 0, "grace_days": 14}

    def fake_export(config=None):
        calls["export"] += 1
        return {"status": "ok", "rows": 0, "partition": None, "watermark": 0}

    orig_purge, orig_export = lc.hard_purge_versions, lc.cold_export_to_parquet
    try:
        lc.hard_purge_versions, lc.cold_export_to_parquet = fake_purge, fake_export
        s._run_purge()
        s._run_cold_export()
        assert calls == {"purge": 1, "export": 1}
        # Locked → skipped.
        with s._purge_lock:
            s._run_purge()
        with s._export_lock:
            s._run_cold_export()
        assert calls == {"purge": 1, "export": 1}
        # Disabled → skipped entirely.
        cfg2 = _make_cfg(env)
        cfg2["cpanel_ingest"]["enabled"] = False
        s2 = BackupScheduler(cfg2)
        s2._run_purge()
        s2._run_cold_export()
        assert calls == {"purge": 1, "export": 1}
    finally:
        lc.hard_purge_versions, lc.cold_export_to_parquet = orig_purge, orig_export


def test_periodic_loop_runs_all_ticks_once(env, backends):
    """With all cadences at 0 elapsed, one loop pass fires every tick."""
    from backupme.scheduler import BackupScheduler
    cfg = _make_cfg(env)
    _drop_archive(env)
    s = BackupScheduler(cfg)

    ran = {"gc": False, "verify": False}
    import backupme.core.gc as gc_mod
    import backupme.core.verify as verify_mod

    def fake_gc(backends_list, apply=False):
        ran["gc"] = True
        return {}

    def fake_verify(check_type="random", count=5, backend_name=None):
        ran["verify"] = True
        return {"status": "ok"}

    orig_gc, orig_verify = gc_mod.run_gc, verify_mod.run_integrity_check
    try:
        gc_mod.run_gc, verify_mod.run_integrity_check = fake_gc, fake_verify
        # Force all cadences due.
        s._last_ingest = s._last_purge = s._last_export = 0.0
        s._last_gc = s._last_verify = 0.0
        # Run exactly one loop iteration: the patched wait() sets stop.
        s._stop.wait = lambda x: (s._stop.set(), True)[1]
        BackupScheduler._periodic_loop(s)
    finally:
        gc_mod.run_gc, verify_mod.run_integrity_check = orig_gc, orig_verify
        s._stop.set()

    n = execute("SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'").fetchone()[0]
    assert n == 3
    assert ran == {"gc": True, "verify": True}


def test_check_staging_budget(env):
    import dataclasses
    from backupme.config import load_config
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    staging = env / "staging_test"
    cfg.cpanel_ingest.staging_dir = str(staging)
    cfg.cpanel_ingest.staging_budget_gb = 0.000001  # ~1KB
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "blob.bin").write_bytes(b"x" * 4096)
    assert ci.check_staging_budget(dataclasses.asdict(cfg)) is False

    cfg.cpanel_ingest.staging_budget_gb = 10.0
    assert ci.check_staging_budget(dataclasses.asdict(cfg)) is True

    # Nonexistent staging dir → fine.
    cfg.cpanel_ingest.staging_dir = str(env / "nope")
    assert ci.check_staging_budget(dataclasses.asdict(cfg)) is True


def test_scheduler_config_yaml(env):
    from backupme.config import load_config
    p = env / "sched.yaml"
    p.write_text(
        "scheduler:\n"
        "  interval: 120\n"
        "  ingest_interval: 45\n"
        "  gc_interval_days: 2\n"
        "  verify_interval_days: 3\n"
    )
    cfg = load_config(str(p))
    assert cfg.scheduler.interval == 120
    assert cfg.scheduler.ingest_interval == 45
    assert cfg.scheduler.gc_interval_days == 2
    assert cfg.scheduler.verify_interval_days == 3

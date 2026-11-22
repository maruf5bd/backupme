"""Tests for Plan 03 daemon automation: cadence ticks, single-flight guards,
staging budget gate, and scheduler config parsing."""
import datetime
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
        return {"purged": 0, "skipped": 0, "above_watermark": 0,
                "watermark": 0, "grace_days": 14}

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


def test_scheduled_gc_default_is_report_only(env, backends):
    """The scheduled gc tick must NEVER delete unless scheduler.gc_apply is
    explicitly true — destructive remote deletions require operator opt-in
    (purge-approval rule; a daemon silently applying gc was a latent
    policy violation)."""
    from backupme.scheduler import BackupScheduler
    import backupme.core.gc as gc_mod

    calls = []

    def fake_gc(backends_list, apply=False):
        calls.append(apply)
        return {}

    orig = gc_mod.run_gc
    try:
        gc_mod.run_gc = fake_gc

        cfg = _make_cfg(env)
        assert cfg["scheduler"]["gc_apply"] is False, \
            "the default config must be report-only"
        s = BackupScheduler(cfg)
        s._run_scheduled_gc()
        assert calls == [False], "default scheduled gc must not apply deletes"

        cfg2 = _make_cfg(env)
        cfg2["scheduler"]["gc_apply"] = True
        s2 = BackupScheduler(cfg2)
        s2._run_scheduled_gc()
        assert calls == [False, True], "gc_apply: true must opt in to deletes"
    finally:
        gc_mod.run_gc = orig


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


# ---------------------------------------------------------------------------
# apply_retention math (QA coverage H4)
# ---------------------------------------------------------------------------

def _fmt(d: datetime.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _non_monday(d: datetime.datetime) -> datetime.datetime:
    while d.weekday() == 0:  # Monday
        d -= datetime.timedelta(days=1)
    return d


def _past_non_monday_over_day7(now: datetime.datetime, start_days: int,
                               max_days: int) -> datetime.datetime:
    """A deterministic date `start_days`..`max_days` ago that is not a Monday
    and has day-of-month > 7 (so no retention branch protects it)."""
    for extra in range(0, max_days - start_days + 1, 7):
        d = _non_monday(now - datetime.timedelta(days=start_days + extra))
        if d.day > 7:
            return d
    raise AssertionError("no deterministic candidate date found")


def _seed_file(vg: str, seq: int, created: datetime.datetime,
               blob_id: str | None = None) -> int:
    """Insert one alive files row; optionally attach one chunk on blob_id."""
    stamp = _fmt(created)
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, "
        "version_group, version_seq, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"seed/{vg}", f"f{seq}.txt", f"sha-{vg}-{seq}", 10 + seq, vg, seq,
         stamp, stamp),
    )
    fid = execute("SELECT id FROM files WHERE version_group=? AND version_seq=?",
                  (vg, seq)).fetchone()[0]
    if blob_id:
        execute(
            "INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
            "VALUES (?, 0, ?, 0, ?)", (fid, blob_id, 10 + seq))
    return fid


def _seed_blob(blob_id: str, ref_count: int):
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, ?)",
            (blob_id, 100, ref_count))
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, "
        "stored_size, stored_sha256) VALUES (?, 'test_local', ?, 100, ?)",
        (blob_id, f"{blob_id}.bin", f"sha-{blob_id}"))


def test_apply_retention_keeps_policy_versions_soft_deletes_rest(env):
    from backupme.scheduler import apply_retention
    now = datetime.datetime.now()

    # Deterministic seed dates (blind `now - timedelta(N)` breaks when
    # now.day <= N+7: the monthly branch would then keep those rows).
    old_delete = _past_non_monday_over_day7(now, 40, 360)     # beyond weekly, day>7
    monthly_keep = None                                        # prev month, day<=7
    first_this = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    prev_last = first_this - datetime.timedelta(days=1)
    for day in (7, 6, 5, 4, 3, 2, 1):
        d = prev_last.replace(day=day)
        if d.weekday() != 0:
            monthly_keep = d
            break
    weekly_delete = _past_non_monday_over_day7(now, 14, 28)   # weekly window, not Monday
    weekly_keep = now - datetime.timedelta(days=now.weekday() + 7)  # last week's Monday
    daily_keep_a = now - datetime.timedelta(days=3)
    daily_keep_b = now - datetime.timedelta(days=1)
    keep_a = now - datetime.timedelta(hours=2)
    keep_b = now

    _seed_blob("blob-v1", 1)        # owned by the deleted v1 only
    _seed_blob("blob-shared", 2)    # v1 + v3, both deleted -> purged
    _seed_blob("blob-shared-keep", 2)  # v1 deleted, v8 kept -> survives at 1

    _seed_file("vg1", 1, old_delete, "blob-v1")
    execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
            "SELECT id, 1, 'blob-shared', 0, 10 FROM files WHERE version_group='vg1' AND version_seq=1")
    execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
            "SELECT id, 2, 'blob-shared-keep', 0, 10 FROM files WHERE version_group='vg1' AND version_seq=1")
    _seed_file("vg1", 2, monthly_keep)
    _seed_file("vg1", 3, weekly_delete)
    execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
            "SELECT id, 0, 'blob-shared', 0, 13 FROM files WHERE version_group='vg1' AND version_seq=3")
    _seed_file("vg1", 4, weekly_keep)
    _seed_file("vg1", 5, daily_keep_a)
    _seed_file("vg1", 6, daily_keep_b)
    _seed_file("vg1", 7, keep_a)
    _seed_file("vg1", 8, keep_b, "blob-shared-keep")

    # A group within the version budget must be left entirely alone.
    _seed_file("vg2", 1, old_delete)
    _seed_file("vg2", 2, now)

    apply_retention({"retention": {"enabled": True, "versions": 2,
                                   "daily": 7, "weekly": 4, "monthly": 12}})

    alive = {r[0] for r in execute(
        "SELECT version_seq FROM files WHERE version_group='vg1' "
        "AND deleted_at IS NULL").fetchall()}
    # newest 2 (keep set) + daily-window pair + Monday snapshot + early-month
    # snapshot; the two unprotected old rows are soft-deleted.
    assert alive == {2, 4, 5, 6, 7, 8}, alive
    dead = {r[0] for r in execute(
        "SELECT version_seq FROM files WHERE version_group='vg1' "
        "AND deleted_at IS NOT NULL").fetchall()}
    assert dead == {1, 3}

    # Chunks of deleted versions are gone; chunks of kept versions remain.
    dead_ids = [r[0] for r in execute(
        "SELECT id FROM files WHERE version_group='vg1' AND version_seq IN (1,3)")]
    assert dead_ids and execute(
        "SELECT COUNT(*) FROM file_chunks WHERE file_id IN (%s)"
        % ",".join("?" * len(dead_ids)), dead_ids).fetchone()[0] == 0
    kept_ids = [r[0] for r in execute(
        "SELECT id FROM files WHERE version_group='vg1' AND deleted_at IS NULL")]
    assert execute(
        "SELECT COUNT(*) FROM file_chunks WHERE file_id IN (%s)"
        % ",".join("?" * len(kept_ids)), kept_ids).fetchone()[0] > 0

    # unref_blob ran: sole-owner blob purged, shared blob of two dead
    # versions purged, blob shared with a kept version survives at ref 1.
    assert not execute("SELECT 1 FROM blobs WHERE blob_id='blob-v1'").fetchone()
    assert not execute("SELECT 1 FROM blob_storage WHERE blob_id='blob-v1'").fetchone()
    assert not execute("SELECT 1 FROM blobs WHERE blob_id='blob-shared'").fetchone()
    row = execute("SELECT ref_count FROM blobs WHERE blob_id='blob-shared-keep'").fetchone()
    assert row and row[0] == 1

    # The under-budget group is untouched.
    assert execute(
        "SELECT COUNT(*) FROM files WHERE version_group='vg2' "
        "AND deleted_at IS NULL").fetchone()[0] == 2


def test_apply_retention_disabled_noop(env):
    from backupme.scheduler import apply_retention
    now = datetime.datetime.now()
    _seed_blob("blob-x", 1)
    _seed_file("vgx", 1, now - datetime.timedelta(days=400), "blob-x")
    _seed_file("vgx", 2, now)
    _seed_file("vgx", 3, now)

    apply_retention({"retention": {"enabled": False}})

    assert execute(
        "SELECT COUNT(*) FROM files WHERE version_group='vgx' "
        "AND deleted_at IS NULL").fetchone()[0] == 3
    assert execute("SELECT 1 FROM blobs WHERE blob_id='blob-x'").fetchone()


def test_apply_retention_monthly_window_is_calendar_accurate(env):
    """D34: the monthly window anchors on calendar months, not monthly*30
    days. Early-month anchors from the last `monthly` calendar months stay;
    older ones go. `monthly * 30` = 360 days deleted a 12-calendar-month-old
    window's edge anchor (up to ~365 days old) and drifted on leap years."""
    import calendar as cal_mod
    from backupme.scheduler import apply_retention

    now = datetime.datetime.now()
    vg = "vgc"

    def _anchor(months_back: int) -> datetime.datetime:
        # The early-month snapshot (day <= 7) `months_back` calendar months ago.
        year, month = now.year, now.month - months_back
        while month <= 0:
            month += 12
            year -= 1
        day = min(5, cal_mod.monthrange(year, month)[1])
        return datetime.datetime(year, month, day, 12, 0, 0)

    # seq 14 = months_back 0 (newest seq + newest date, kept by the version
    # budget); seq s = 14 - months_back for months_back 1..13.
    for months_back in range(1, 14):
        _seed_file(vg, 14 - months_back, _anchor(months_back))
    _seed_file(vg, 14, _anchor(0))

    apply_retention({"retention": {"enabled": True, "versions": 1,
                                   "daily": 7, "weekly": 4, "monthly": 12}})

    alive = {r[0] for r in execute(
        "SELECT version_seq FROM files WHERE version_group=? AND deleted_at IS NULL",
        (vg,)).fetchall()}
    # 12 calendar monthly anchors survive (months_back 0..11 = seqs 3..14);
    # months_back 12 and 13 (seqs 2 and 1) fall outside the window.
    assert alive == set(range(3, 15)), alive


def test_apply_retention_survives_malformed_created_at(env):
    """D34: one malformed created_at used to raise out of the whole pass
    (clean rollback, identical failure every run — retention stalled forever).
    Now the row is skipped with a warning + honest counter and the pass still
    processes every healthy group."""
    from backupme.scheduler import apply_retention

    now = datetime.datetime.now()
    old = _past_non_monday_over_day7(now, 40, 360)  # unprotected date
    for vg in ("vgm", "vgn", "vgh"):
        _seed_file(vg, 1, old)
        _seed_file(vg, 2, now)
    execute("UPDATE files SET created_at='not-a-date' WHERE version_group='vgm' AND version_seq=1")
    execute("UPDATE files SET created_at='' WHERE version_group='vgn' AND version_seq=1")

    result = apply_retention({"retention": {"enabled": True, "versions": 1,
                                            "daily": 7, "weekly": 4, "monthly": 12}})
    assert result == {"deleted": 1, "skipped_malformed": 2}
    # Malformed rows stay alive (never deleted on a stamp we cannot read).
    assert execute("SELECT 1 FROM files WHERE version_group='vgm' "
                   "AND version_seq=1 AND deleted_at IS NULL").fetchone()
    assert execute("SELECT 1 FROM files WHERE version_group='vgn' "
                   "AND version_seq=1 AND deleted_at IS NULL").fetchone()
    # The healthy group was still processed — the pass did not stall.
    assert not execute("SELECT 1 FROM files WHERE version_group='vgh' "
                       "AND version_seq=1 AND deleted_at IS NULL").fetchone()


# ---------------------------------------------------------------------------
# D34: scheduler.enabled, cold-export guard, startup stagger, verify log
# ---------------------------------------------------------------------------

def test_periodic_loop_skips_all_ticks_when_scheduler_disabled(env, backends):
    from backupme.scheduler import BackupScheduler
    import backupme.core.gc as gc_mod

    ran = {"gc": False}

    def fake_gc(backends_list, apply=False):
        ran["gc"] = True
        return {}

    orig_gc = gc_mod.run_gc
    s = None
    try:
        gc_mod.run_gc = fake_gc
        cfg = _make_cfg(env)
        _drop_archive(env)
        cfg["scheduler"]["enabled"] = False
        s = BackupScheduler(cfg)
        # Run exactly one loop iteration: the patched wait() sets stop.
        s._stop.wait = lambda x: (s._stop.set(), True)[1]
        BackupScheduler._periodic_loop(s)
    finally:
        gc_mod.run_gc = orig_gc
        if s:
            s._stop.set()

    assert ran == {"gc": False}, "gc tick must not run when scheduler.enabled is false"
    assert execute(
        "SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%'").fetchone()[0] == 0, \
        "ingest tick must not run when scheduler.enabled is false"


def test_cold_export_zero_or_negative_disables_tick(env, backends):
    from backupme.scheduler import BackupScheduler
    import backupme.core.db_lifecycle as lc

    calls = {"export": 0}

    def fake_export(config=None):
        calls["export"] += 1
        return {"status": "ok", "rows": 0, "partition": None, "watermark": 0}

    orig_export = lc.cold_export_to_parquet
    schedulers = []
    try:
        lc.cold_export_to_parquet = fake_export
        for days in (0, -5):
            cfg = _make_cfg(env)
            cfg["db_lifecycle"]["cold_export_after_days"] = days
            s = BackupScheduler(cfg)
            schedulers.append(s)
            # Markers at 0 make every cadence due; only the guard decides.
            s._stop.wait = lambda x: (s._stop.set(), True)[1]
            BackupScheduler._periodic_loop(s)
        assert calls == {"export": 0}, "cold_export_after_days <= 0 must disable the tick"
    finally:
        lc.cold_export_to_parquet = orig_export
        for s in schedulers:
            s._stop.set()


def test_start_staggers_cadence_markers(env):
    """D34: start() initializes every cadence marker at 'now', so the first
    pass cannot fire ingest/purge/export/gc/verify/guard all at once."""
    import threading
    import time
    from backupme.scheduler import BackupScheduler

    s = BackupScheduler(_make_cfg(env))
    assert s._last_ingest == 0.0, "constructor still starts markers at 0"

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            pass
        def is_alive(self):
            return False
        def join(self, timeout=None):
            pass

    orig_thread = threading.Thread
    orig_rep_start, orig_rep_stop = s._replicator.start, s._replicator.stop
    s._replicator.start = lambda: None
    s._replicator.stop = lambda: None
    threading.Thread = _DummyThread
    try:
        before = time.time()
        s.start()
    finally:
        threading.Thread = orig_thread
        s._replicator.start, s._replicator.stop = orig_rep_start, orig_rep_stop

    for marker in ("_last_ingest", "_last_purge", "_last_export",
                   "_last_gc", "_last_verify", "_last_guard"):
        assert getattr(s, marker) >= before, f"{marker} must be initialized at start"


def test_verify_tick_logs_real_failed_count(env, backends, caplog):
    """D34: the verify tick logged result.get('failed_files') — a key the
    check result never carries — so every tick logged failed=None."""
    import logging
    from backupme.scheduler import BackupScheduler
    import backupme.core.verify as verify_mod

    def fake_check(check_type="random", count=5, backend_name=None, auto_heal=False):
        return {"check_id": 1, "status": "failed", "checked": 3, "failed": 2}

    orig = verify_mod.run_integrity_check
    s = BackupScheduler(_make_cfg(env))
    try:
        verify_mod.run_integrity_check = fake_check
        with caplog.at_level(logging.INFO, logger="backupme.scheduler"):
            s._run_scheduled_verify()
    finally:
        verify_mod.run_integrity_check = orig

    verify_lines = [r.getMessage() for r in caplog.records
                    if "verify tick" in r.getMessage()]
    assert verify_lines and "failed=2" in verify_lines[0], verify_lines

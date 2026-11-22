"""D33-SCH-2 tests: the strictly serial tick dispatch is gone — cadence
ticks dispatch to a small worker pool while every safety property stays.

Proven here:
- a slow ingest tick no longer blocks the next retention pass (the defect),
- a tick whose previous instance still runs is skipped (single-flight),
- scheduler.enabled=false still skips everything and the pool still shuts
  down cleanly,
- an exception escaping a tick guard is retrieved and logged by the future
  done-callback.
"""
import concurrent.futures
import logging
import threading
import time

from backupme.core.database import execute

LOG_NAME = "backupme.scheduler"  # get_logger prefixes the package


def _make_cfg(env, scheduler_overrides=None):
    """Dict config as the daemon passes to BackupScheduler: local backend,
    cpanel_ingest enabled against env/drop, 1s loop interval for the tests."""
    import dataclasses
    from backupme.config import load_config
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(env / "drop")
    cfg.scheduler.interval = 1
    for key, value in (scheduler_overrides or {}).items():
        setattr(cfg.scheduler, key, value)
    return dataclasses.asdict(cfg)


def _run_loop_in_thread(s):
    """Run the real periodic loop in a daemon thread (like the daemon does,
    minus start(): the env fixture already initialized the scratch DB)."""
    loop = threading.Thread(target=s._periodic_loop, daemon=True)
    loop.start()
    return loop


def test_slow_ingest_does_not_block_next_pass(env, backends, monkeypatch):
    """The defect: a multi-hour cpmove ingest used to run INSIDE the loop
    pass, suspending backups/retention/everything behind it. Now the ingest
    tick runs in the pool and the next pass's retention fires while it is
    still running."""
    import backupme.scheduler as scheduler_mod
    from backupme.core import cpanel_ingest as ci_mod
    from backupme.scheduler import BackupScheduler

    cfg = _make_cfg(env)
    (env / "drop").mkdir()

    ingest_started = threading.Event()
    ingest_may_finish = threading.Event()
    ingest_finished = threading.Event()

    def slow_ingest_tree(drop_root, backends_list, config):
        ingest_started.set()
        ingest_may_finish.wait(30)
        ingest_finished.set()
        return {"status": "ok", "archives": {}}

    monkeypatch.setattr(ci_mod, "ingest_tree", slow_ingest_tree)

    retention_lock = threading.Lock()
    retention_passes = {"n": 0}

    def counting_retention(config):
        with retention_lock:
            retention_passes["n"] += 1
        return {"deleted": 0, "skipped_malformed": 0}

    monkeypatch.setattr(scheduler_mod, "apply_retention", counting_retention)

    s = BackupScheduler(cfg)
    s._last_ingest = 0.0  # ingest due on the first pass
    loop = _run_loop_in_thread(s)
    try:
        assert ingest_started.wait(10), "slow ingest tick never started"
        # The loop interval is 1s: the next pass must run retention again
        # while the ingest tick is still running (the old serial loop could
        # never do that — the ingest tick held the pass for hours).
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with retention_lock:
                if retention_passes["n"] >= 2:
                    break
            time.sleep(0.05)
        with retention_lock:
            passes_while_ingest_running = retention_passes["n"]
        assert passes_while_ingest_running >= 2, (
            "retention did not fire again while the slow ingest tick was "
            "still running — ticks are still dispatched serially")
        assert not ingest_finished.is_set(), \
            "the slow ingest tick should still be running at this point"
    finally:
        ingest_may_finish.set()
        s._stop.set()
        loop.join(timeout=15)

    assert ingest_finished.is_set(), "the slow ingest tick never finished"


def test_tick_skipped_while_previous_instance_still_runs(env, backends, monkeypatch, caplog):
    """Single-flight under concurrent dispatch: while one ingest tick still
    runs, later passes must skip it (the per-tick non-blocking lock), never
    run a second instance."""
    import backupme.core.cpanel_ingest as ci_mod
    from backupme.scheduler import BackupScheduler

    cfg = _make_cfg(env, {"ingest_interval": 1})  # ingest due on every pass
    (env / "drop").mkdir()

    ingest_started = threading.Event()
    ingest_may_finish = threading.Event()
    ingest_finished = threading.Event()
    counter_lock = threading.Lock()
    ingest_calls = {"n": 0}

    def slow_ingest_tree(drop_root, backends_list, config):
        with counter_lock:
            ingest_calls["n"] += 1
        ingest_started.set()
        ingest_may_finish.wait(30)
        ingest_finished.set()
        return {"status": "ok", "archives": {}}

    monkeypatch.setattr(ci_mod, "ingest_tree", slow_ingest_tree)

    s = BackupScheduler(cfg)
    s._last_ingest = 0.0  # ingest due on the first pass
    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        loop = _run_loop_in_thread(s)
        try:
            assert ingest_started.wait(10), "slow ingest tick never started"
            # Give the loop at least three passes (interval 1s) while the
            # first ingest instance is still running.
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                time.sleep(0.05)
            with counter_lock:
                assert ingest_calls["n"] == 1, \
                    "the ingest tick ran concurrently with itself"
            assert not ingest_finished.is_set(), \
                "the first ingest instance should still be running here"
            skip_logs = [r for r in caplog.records
                         if "previous ingest still running" in r.getMessage()]
            assert skip_logs, "later passes must log the single-flight skip"
        finally:
            ingest_may_finish.set()
            s._stop.set()
            loop.join(timeout=15)

    assert ingest_finished.is_set(), "the slow ingest tick never finished"


def test_scheduler_disabled_skips_ticks_and_shuts_pool_cleanly(env, backends, monkeypatch):
    """scheduler.enabled=false still runs no ticks (D34 gate preserved under
    the pool), and the loop still exits cleanly with the pool shut down."""
    import backupme.core.cpanel_ingest as ci_mod
    import backupme.core.gc as gc_mod
    from backupme.scheduler import BackupScheduler

    calls = {"gc": 0, "ingest": 0}

    def fake_gc(backends_list, apply=False):
        calls["gc"] += 1
        return {}

    def fake_ingest(drop_root, backends_list, config):
        calls["ingest"] += 1
        return {"status": "ok", "archives": {}}

    monkeypatch.setattr(gc_mod, "run_gc", fake_gc)
    monkeypatch.setattr(ci_mod, "ingest_tree", fake_ingest)

    cfg = _make_cfg(env)
    cfg["scheduler"]["enabled"] = False
    s = BackupScheduler(cfg)
    # Every cadence due; only the enabled gate may decide.
    s._last_ingest = s._last_purge = s._last_export = 0.0
    s._last_gc = s._last_verify = s._last_guard = 0.0
    # Run exactly one loop iteration: the patched wait() sets stop.
    s._stop.wait = lambda x: (s._stop.set(), True)[1]
    BackupScheduler._periodic_loop(s)  # must return cleanly

    assert calls == {"gc": 0, "ingest": 0}, \
        "no tick may run when scheduler.enabled is false"
    assert execute(
        "SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%'"
    ).fetchone()[0] == 0, "the ingest tick must not touch the DB"


def test_future_exception_escaping_guard_is_logged(env, backends, caplog):
    """An uncaught exception in a pool future must be retrieved and logged —
    the done-callback is the safety net for anything that escapes a tick's
    own exception guard."""
    from backupme.scheduler import BackupScheduler

    s = BackupScheduler(_make_cfg(env))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    pending: list = []

    def boom():
        raise RuntimeError("tick escaped its guard")

    with caplog.at_level(logging.ERROR, logger=LOG_NAME):
        s._dispatch_tick(pool, pending, boom)
        future = pending[0]
        deadline = time.monotonic() + 5
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert future.done(), "the dispatch future never completed"
    pool.shutdown(wait=True)

    escaped = [r for r in caplog.records
               if r.levelno == logging.ERROR
               and "outside its guard" in r.getMessage()]
    assert escaped, "an exception escaping the tick guard must be logged"

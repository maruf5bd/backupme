"""Tests for the off-site DB snapshot guard (the DB-loss insurance layer).

Covers ``ship_snapshot`` (ok / partial / failed, guarded remote path, size
verification incl. the exists() fallback), the ``db snapshot --offsite``
CLI flag, and the daily scheduler guard tick (gated + single-flight).
Everything runs against the isolated ``env``/``backends`` fixtures —
never the live DB or config.yaml.
"""
import dataclasses
import json
import re

import pytest
from click.testing import CliRunner

from backupme.backends.local import LocalBackend
from backupme.cli import cli
from backupme.config import load_config


class FailingBackend(LocalBackend):
    """A backend that connects fine but fails every upload."""

    def __init__(self, name, config):
        super().__init__(name, config)
        self.upload_calls = 0

    def upload(self, local_path, remote_rel_path):
        self.upload_calls += 1
        raise RuntimeError("backend down")


class NoListBackend(LocalBackend):
    """A backend whose directory listing is unavailable (verification
    must degrade to the exists() fallback)."""

    def list_files(self, prefix: str = "") -> list[dict]:
        raise RuntimeError("no listing for you")


@pytest.fixture()
def runner():
    return CliRunner()


def invoke(runner, env, *args, **kw):
    """Invoke the CLI pinned to the isolated env's config and DB."""
    return runner.invoke(
        cli,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"), *args],
        **kw,
    )


_GUARD_PATH_RE = re.compile(r"^guard/snapshots/\d{8}T\d{6}Z-gen\d+\.db\.zst$")


def _guarded_copy(env, remote_path: str):
    return env.parent / "storage" / "guard" / "snapshots" / remote_path.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# ship_snapshot
# ---------------------------------------------------------------------------

def test_ship_snapshot_ok_single_backend(env, backends):
    from backupme.core import offsite_guard
    result = offsite_guard.ship_snapshot(load_config(), backends=backends)

    assert result["status"] == "ok"
    snap = result["snapshot"]
    assert snap is not None
    local = env / snap["snapshot"]
    # The snapshot file remains in the working dir, exactly as before.
    assert local.exists()
    assert local.stat().st_size == snap["size"]

    entries = result["backends"]
    assert set(entries) == {"test_local"}
    entry = entries["test_local"]
    assert entry["status"] == "ok"
    assert _GUARD_PATH_RE.match(entry["remote_path"]), entry["remote_path"]
    assert entry["size"] == local.stat().st_size

    # The guarded copy really exists on the backend with the same byte size.
    remote = _guarded_copy(env, entry["remote_path"])
    assert remote.exists()
    assert remote.stat().st_size == local.stat().st_size


def test_ship_snapshot_listed_under_guarded_prefix(env, backends):
    from backupme.core import offsite_guard
    result = offsite_guard.ship_snapshot(load_config(), backends=backends)
    remote_path = result["backends"]["test_local"]["remote_path"]
    # The guarded object is findable through the backend's own listing
    # (this is exactly what the size verification relies on).
    listed = {e["path"] for e in backends[0].list_files("guard/snapshots")}
    assert remote_path in listed
    assert remote_path.rsplit("/", 1)[0] == "guard/snapshots"


def test_remote_name_scheme_is_stable_and_sortable(monkeypatch):
    """Fixed-width UTC stamp + generation: lexicographic == chronological."""
    import datetime as dtmod
    from types import SimpleNamespace

    import backupme.core.offsite_guard as og

    class _FakeDateTime(dtmod.datetime):
        _current = dtmod.datetime(2026, 11, 8, 9, 30, 15, tzinfo=dtmod.timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls._current

    monkeypatch.setattr(og, "datetime",
                        SimpleNamespace(datetime=_FakeDateTime, timezone=dtmod.timezone))
    n1 = og._remote_name(3)
    _FakeDateTime._current = _FakeDateTime._current + dtmod.timedelta(seconds=1)
    n2 = og._remote_name(3)
    assert n1 == "20261108T093015Z-gen3.db.zst"
    assert n2 == "20261108T093016Z-gen3.db.zst"
    assert n1 < n2


def test_ship_snapshot_partial_when_one_backend_fails(env, backends):
    from backupme.core import offsite_guard
    bad = FailingBackend("bad_backend", {"path": str(env / "bad_store")})
    bad.connect()
    good = backends[0]
    result = offsite_guard.ship_snapshot(load_config(), backends=[good, bad])

    assert result["status"] == "partial"
    assert bad.upload_calls == 1
    entries = result["backends"]
    assert entries[good.name]["status"] == "ok"
    assert entries[good.name]["size"] == result["snapshot"]["size"]
    assert entries["bad_backend"]["status"] == "error"
    assert "backend down" in entries["bad_backend"]["error"]
    # The good backend still received the guarded copy.
    assert _guarded_copy(env, entries[good.name]["remote_path"]).exists()


def test_ship_snapshot_verifies_via_exists_when_list_fails(env):
    from backupme.core import offsite_guard
    bk = NoListBackend("nolist", {"path": str(env / "nolist_store")})
    bk.connect()
    result = offsite_guard.ship_snapshot(load_config(), backends=[bk])
    entry = result["backends"]["nolist"]
    assert entry["status"] == "ok"
    assert entry["verify"] == "exists"
    assert entry["size"] == result["snapshot"]["size"]


def test_ship_snapshot_reuses_provided_snapshot(env, backends):
    from backupme.core import offsite_guard
    from backupme.core.snapshot import create_snapshot
    first = create_snapshot()
    result = offsite_guard.ship_snapshot(load_config(), backends=backends,
                                         snapshot=first)
    assert result["status"] == "ok"
    assert result["snapshot"]["snapshot"] == first["snapshot"]
    # The ship call must not create a second local snapshot.
    snaps = sorted(p.name for p in env.glob("snapshot-*.db.zst"))
    assert snaps == [first["snapshot"]]


def test_ship_snapshot_no_backends_failed_without_raising(env):
    from backupme.core import offsite_guard
    # Explicit empty subset.
    result = offsite_guard.ship_snapshot(load_config(), backends=[])
    assert result["status"] == "failed"
    assert result["reason"]
    assert result["backends"] == {}
    # Fatal path must not even create a snapshot file.
    assert not list(env.glob("snapshot-*.db.zst"))

    # Nothing discoverable either (and never raises).
    result2 = offsite_guard.ship_snapshot({"backends": {}})
    assert result2["status"] == "failed"
    assert result2["snapshot"] is None


# ---------------------------------------------------------------------------
# db snapshot --offsite CLI
# ---------------------------------------------------------------------------

def test_db_snapshot_offsite_flag_ships_and_emits(runner, env):
    r = invoke(runner, env, "db", "snapshot", "--offsite", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["snapshot"].endswith(".db.zst")
    ship = data["offsite"]
    assert ship["status"] == "ok"
    entry = ship["backends"]["test_local"]
    assert entry["status"] == "ok"
    assert _GUARD_PATH_RE.match(entry["remote_path"])
    # The guarded copy exists with the same size as the local snapshot.
    local = env / data["snapshot"]
    remote = _guarded_copy(env, entry["remote_path"])
    assert remote.exists()
    assert remote.stat().st_size == local.stat().st_size


def test_db_snapshot_offsite_flag_human_output(runner, env):
    r = invoke(runner, env, "db", "snapshot", "--offsite")
    assert r.exit_code == 0, r.output
    assert "Snapshot: " in r.stdout
    assert "Off-site: ok" in r.stdout
    assert "test_local: ok " in r.stdout


def test_db_snapshot_without_flag_unchanged(runner, env):
    r = invoke(runner, env, "db", "snapshot", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert "offsite" not in data
    # No backend writes at all: the guarded prefix must not exist in storage.
    assert not (env.parent / "storage" / "guard").exists()
    # Local snapshot still produced in the working dir, as today.
    assert list(env.glob("snapshot-*.db.zst"))


def test_db_snapshot_without_flag_human_line_identical(runner, env):
    r = invoke(runner, env, "db", "snapshot")
    assert r.exit_code == 0, r.output
    assert r.stdout.startswith("Snapshot: snapshot-")
    assert "(gen=" in r.stdout
    assert "Off-site" not in r.stdout


# ---------------------------------------------------------------------------
# scheduler guard tick
# ---------------------------------------------------------------------------

def _spy_ship(monkeypatch, calls):
    import backupme.core.offsite_guard as og

    def fake_ship(config=None, backends=None, snapshot=None):
        calls["n"] += 1
        return {"status": "ok", "snapshot": None, "backends": {}}

    monkeypatch.setattr(og, "ship_snapshot", fake_ship)


def test_scheduler_guard_tick_runs_and_single_flight(env, backends, monkeypatch):
    from backupme.scheduler import BackupScheduler
    cfg = dataclasses.asdict(load_config())
    s = BackupScheduler(cfg)

    calls = {"n": 0}
    _spy_ship(monkeypatch, calls)
    s._run_offsite_guard()
    assert calls["n"] == 1
    # Held lock -> skipped, not blocked, no error.
    with s._guard_lock:
        s._run_offsite_guard()
    assert calls["n"] == 1
    # Released -> runs again.
    s._run_offsite_guard()
    assert calls["n"] == 2


def test_scheduler_guard_tick_disabled_never_ships(env, backends, monkeypatch):
    from backupme.scheduler import BackupScheduler
    cfg = dataclasses.asdict(load_config())
    cfg["guard"]["offsite_snapshots"] = False
    s = BackupScheduler(cfg)

    calls = {"n": 0}
    _spy_ship(monkeypatch, calls)
    s._run_offsite_guard()
    assert calls["n"] == 0


def test_periodic_loop_fires_guard_tick_once_daily(env, backends, monkeypatch):
    import backupme.core.gc as gc_mod
    import backupme.core.verify as verify_mod
    import backupme.core.offsite_guard as og
    from backupme.scheduler import BackupScheduler
    cfg = dataclasses.asdict(load_config())
    s = BackupScheduler(cfg)

    calls = {"n": 0}

    def fake_ship(config=None, backends=None, snapshot=None):
        calls["n"] += 1
        return {"status": "ok", "snapshot": None, "backends": {}}

    monkeypatch.setattr(og, "ship_snapshot", fake_ship)
    monkeypatch.setattr(gc_mod, "run_gc", lambda backends_list, apply=False: {})
    monkeypatch.setattr(verify_mod, "run_integrity_check",
                        lambda check_type="random", count=5, backend_name=None: {"status": "ok"})
    s._last_guard = 0.0
    # Run exactly one loop iteration: the patched wait() sets stop.
    s._stop.wait = lambda x: (s._stop.set(), True)[1]
    BackupScheduler._periodic_loop(s)
    assert calls["n"] == 1
    assert s._last_guard > 0.0


def test_guard_tick_ship_failure_never_escapes(env, backends, monkeypatch):
    import backupme.core.offsite_guard as og
    from backupme.scheduler import BackupScheduler
    cfg = dataclasses.asdict(load_config())
    s = BackupScheduler(cfg)

    def exploding_ship(config=None, backends=None, snapshot=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(og, "ship_snapshot", exploding_ship)
    s._run_offsite_guard()  # must not raise
    # Lock released despite the crash: the next tick can proceed.
    s._run_offsite_guard()


# ---------------------------------------------------------------------------
# config wiring
# ---------------------------------------------------------------------------

def test_guard_config_defaults_and_yaml_parsing(env, caplog):
    from backupme.config import load_config
    # env config.yaml has no guard section -> dataclass defaults apply.
    cfg = load_config()
    assert cfg.guard.offsite_snapshots is True
    assert cfg.guard.keep_local_snapshots == 14

    p = env / "config.yaml"
    p.write_text(p.read_text() +
                 "guard:\n"
                 "  offsite_snapshots: false\n"
                 "  keep_local_snapshots: 7\n"
                 "  bogus_key: 1\n")
    import logging
    with caplog.at_level(logging.WARNING):
        cfg2 = load_config()
    assert cfg2.guard.offsite_snapshots is False
    assert cfg2.guard.keep_local_snapshots == 7
    # Unknown keys warn, never crash.
    assert any("bogus_key" in rec.message for rec in caplog.records)

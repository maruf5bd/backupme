import json

import pytest

from backupme.core.gc import find_orphans, run_gc
from tests.helpers import backup_bytes, restore_bytes


def test_gc_dry_run_and_apply(env, backends):
    r = backup_bytes(env, "keep.txt", b"keep " * 100, backends)
    storage = env.parent / "storage"
    orphans = [
        storage / "objects" / "zz" / "yy" / "orphan1",
        storage / "bundle" / "orphan-bundle",
    ]
    for p in orphans:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"junk")

    # Sanity: the real upload produced at least one referenced bundle file.
    referenced = list((storage / "bundle").glob("*"))
    assert referenced

    found = find_orphans(backends, min_age_seconds=0)
    names = [f["path"] for f in found["test_local"]]
    assert any("orphan1" in n for n in names)
    assert any("orphan-bundle" in n for n in names)
    # Referenced files and manifests/pointers are never candidates.
    assert not any(".manifest.json" in n or n.endswith(".backupme") for n in names)
    real_bundle = [f["path"] for f in found["test_local"] if f["path"].startswith("bundle/")
                   and "orphan" not in f["path"]]
    assert real_bundle == []

    out2 = run_gc(backends, apply=True, min_age_seconds=0)
    assert out2["test_local"]["deleted"] == 2
    for p in orphans:
        assert not p.exists()
    # Referenced data still restorable after gc.
    assert restore_bytes(r["file_id"], env) == b"keep " * 100


def test_gc_cli_dry_run(env, backends):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    backup_bytes(env, "cli.txt", b"cli " * 100, backends)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "gc"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output


def test_gc_skips_young_orphans_by_default(env, backends):
    """QA round D: an object uploaded but not yet indexed (young mtime) must
    never be a gc candidate — deleting it would corrupt the in-flight upload."""
    import time
    backup_bytes(env, "grace.txt", b"grace " * 100, backends)
    storage = env.parent / "storage"
    young = storage / "objects" / "zz" / "yy" / "young-orphan"
    young.parent.mkdir(parents=True, exist_ok=True)
    young.write_bytes(b"junk")

    # Default grace: young orphan is invisible to gc.
    found = find_orphans(backends)
    assert not any("young-orphan" in f["path"] for f in found["test_local"])

    # Past the grace window it becomes a candidate (backdate the mtime).
    old = time.time() - 7200
    import os
    os.utime(young, (old, old))
    found2 = find_orphans(backends)
    assert any("young-orphan" in f["path"] for f in found2["test_local"])

    # min_age_seconds=0 disables the grace entirely (test harness mode).
    found3 = find_orphans(backends, min_age_seconds=0)
    assert any("young-orphan" in f["path"] for f in found3["test_local"])


def test_gc_skips_entries_without_mtime(env, backends):
    """A walk entry with no mtime is treated as too young to judge: a leak is
    recoverable, a deleted live blob is not."""
    class NoMtimeBackend:
        name = "test_local"

        def walk(self, prefix):
            if prefix == "objects":
                return [{"path": "objects/aa/bb/mystery", "size": 5}]
            return []

        def delete(self, path):
            raise AssertionError("must never be called while grace applies")

    backup_bytes(env, "nomtime.txt", b"nm " * 100, backends)
    found = find_orphans([NoMtimeBackend()])
    assert found["test_local"] == []


# ---------------------------------------------------------------------------
# P1-4: `gc --apply` is destructive and must leave an op_log audit row
# ---------------------------------------------------------------------------

def _gc_ops():
    from backupme.core.database import execute
    return execute("SELECT * FROM op_log WHERE op_type = 'gc'").fetchall()


def _seed_one_orphan(env):
    storage = env.parent / "storage"
    orphan = storage / "objects" / "zz" / "yy" / "orphan-oplog"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"junk")
    return orphan


def test_gc_dry_run_writes_no_op_log(env, backends):
    backup_bytes(env, "oplog-dry.txt", b"oplog " * 100, backends)
    _seed_one_orphan(env)
    run_gc(backends, apply=False, min_age_seconds=0)
    assert _gc_ops() == []


def test_gc_apply_writes_committed_op_log(env, backends):
    backup_bytes(env, "oplog.txt", b"oplog " * 100, backends)
    orphan = _seed_one_orphan(env)

    out = run_gc(backends, apply=True, min_age_seconds=0)
    assert out["test_local"]["deleted"] == 1
    assert not orphan.exists()

    ops = _gc_ops()
    assert len(ops) == 1
    row = ops[0]
    assert row["status"] == "committed"
    assert row["idempotency_key"].startswith("gc:")
    assert row["started_at"] is not None
    assert row["completed_at"] is not None
    target = json.loads(row["target_json"])
    assert target["planned"] == {"test_local": 1}

    # A follow-up apply with nothing orphaned writes no new entry.
    run_gc(backends, apply=True, min_age_seconds=0)
    assert len(_gc_ops()) == 1


def test_gc_apply_per_file_delete_failure_stays_committed(env, backends, monkeypatch):
    """A single object that refuses to be deleted is logged, counted, and the
    op still commits (mirror of phantom_prune's partial semantics)."""
    backup_bytes(env, "oplog-partial.txt", b"oplog " * 100, backends)
    _seed_one_orphan(env)

    def refuses(path):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(backends[0], "delete", refuses)
    out = run_gc(backends, apply=True, min_age_seconds=0)
    assert out["test_local"]["deleted"] == 0

    ops = _gc_ops()
    assert len(ops) == 1
    assert ops[0]["status"] == "committed"


def test_gc_apply_marks_op_log_failed_on_error(env, backends, monkeypatch):
    """If the delete phase blows up outside the per-file guard, the op_log row
    must transition to 'failed' and the error must propagate."""
    from backupme.core import op_log as op_log_mod
    backup_bytes(env, "oplog-fail.txt", b"oplog " * 100, backends)
    _seed_one_orphan(env)

    def broken_mark_committed(op_id):
        raise RuntimeError("bookkeeping exploded")

    monkeypatch.setattr(op_log_mod, "mark_committed", broken_mark_committed)
    with pytest.raises(RuntimeError, match="bookkeeping exploded"):
        run_gc(backends, apply=True, min_age_seconds=0)

    ops = _gc_ops()
    assert len(ops) == 1
    assert ops[0]["status"] == "failed"
    assert "bookkeeping exploded" in ops[0]["last_error"]

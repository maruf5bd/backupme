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

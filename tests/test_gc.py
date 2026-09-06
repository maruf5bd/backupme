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

    found = find_orphans(backends)
    names = [f["path"] for f in found["test_local"]]
    assert any("orphan1" in n for n in names)
    assert any("orphan-bundle" in n for n in names)
    # Referenced files and manifests/pointers are never candidates.
    assert not any(".manifest.json" in n or n.endswith(".backupme") for n in names)
    real_bundle = [f["path"] for f in found["test_local"] if f["path"].startswith("bundle/")
                   and "orphan" not in f["path"]]
    assert real_bundle == []

    out2 = run_gc(backends, apply=True)
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

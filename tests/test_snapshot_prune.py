import time

from backupme.core.snapshot import prune_snapshots


def test_prune_removes_old_snapshots(env):
    now = int(time.time())
    names = [
        "snapshot-1-1000000000.db.zst",   # ancient
        "snapshot-1-1500000000.db.zst",   # old
        f"snapshot-1-{now}.db.zst",       # fresh
    ]
    for n in names:
        (env / n).write_bytes(b"x")
    prune_snapshots(retention_days=7, min_keep=1)
    remaining = sorted(p.name for p in env.glob("snapshot-*.db.zst"))
    assert remaining == [f"snapshot-1-{now}.db.zst"]

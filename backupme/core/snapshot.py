import hashlib
import os
import random
import re
import sys
import tempfile
from pathlib import Path

from backupme.core.database import (
    get_connection, execute, commit, get_generation, wal_checkpoint,
)
from backupme.core import hasher as hasher_mod
from backupme.core.downloader import download_file
from backupme.backends import get_all_backends


def create_snapshot(retention_days: int = 7) -> dict:
    gen = get_generation()
    import time as tmod
    ts = int(tmod.time())
    snap_name = f"snapshot-{gen}-{ts}.db"

    conn = get_connection()
    wal_checkpoint()
    conn.execute(f"VACUUM INTO '{snap_name}'")
    commit()

    import zstandard as zstd
    compressed = Path(snap_name).with_suffix(".db.zst")
    cctx = zstd.ZstdCompressor(level=3)
    with open(snap_name, "rb") as fin:
        with open(compressed, "wb") as fout:
            with cctx.stream_writer(fout) as compressor:
                while True:
                    chunk = fin.read(1024 * 1024)
                    if not chunk:
                        break
                    compressor.write(chunk)
    Path(snap_name).unlink(missing_ok=True)

    return {"snapshot": compressed.name, "generation": gen}


_SNAPSHOT_NAME_RE = re.compile(r"^snapshot-(\d+)-(\d+)\.db\.zst$")


def prune_snapshots(retention_days: int = 7, min_keep: int = 3):
    snaps = sorted(Path(".").glob("snapshot-*.db.zst"))
    if len(snaps) <= min_keep:
        return
    import datetime
    cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)
    for s in snaps[:-min_keep]:
        m = _SNAPSHOT_NAME_RE.match(s.name)
        if not m:
            print(f"warning: unrecognized snapshot name, not pruning: {s.name}", file=sys.stderr)
            continue
        ts = int(m.group(2))
        if ts < cutoff.timestamp():
            s.unlink(missing_ok=True)


def auto_backup(config: dict):
    backends = get_all_backends(config)
    gen = get_generation()
    snap = create_snapshot()
    for bk in backends:
        try:
            bk.db_upload_chunk(snap["snapshot"], f"__backupme_db__/snapshots/{snap['snapshot']}")
            sha_path = snap["snapshot"] + ".sha256"
            sha = hashlib.sha256()
            with open(snap["snapshot"], "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    sha.update(chunk)
            Path(sha_path).write_text(sha.hexdigest())
            bk.db_upload_chunk(sha_path, f"__backupme_db__/snapshots/{sha_path}")
            Path(sha_path).unlink(missing_ok=True)
        except Exception:
            import traceback
            traceback.print_exc()
    Path(snap["snapshot"]).unlink(missing_ok=True)
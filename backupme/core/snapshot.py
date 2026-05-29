import hashlib
import os
import random
import tempfile
from pathlib import Path

from backupme.core.database import (
    DB_PATH, get_connection, execute, commit, get_generation, wal_checkpoint,
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
    with open(compressed, "wb") as fout:
        with cctx.stream_writer(fout) as compressor:
            compressor.write(Path(snap_name).read_bytes())
    Path(snap_name).unlink()

    return {"snapshot": compressed.name, "generation": gen}


def prune_snapshots(retention_days: int = 7, min_keep: int = 3):
    snaps = sorted(Path(".").glob("snapshot-*.db.zst"))
    if len(snaps) <= min_keep:
        return
    import datetime
    cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)
    for s in snaps[:-min_keep]:
        try:
            parts = s.stem.split("-")
            ts = int(parts[2]) if len(parts) > 2 else 0
            if ts < cutoff.timestamp():
                s.unlink()
        except (ValueError, IndexError):
            continue


def auto_backup(config: dict):
    backends = get_all_backends(config)
    gen = get_generation()
    snap = create_snapshot()
    for bk in backends:
        try:
            bk.db_upload_chunk(snap["snapshot"], f"__backupme_db__/snapshots/{snap['snapshot']}")
            sha_path = snap["snapshot"] + ".sha256"
            Path(sha_path).write_text(hashlib.sha256(Path(snap["snapshot"]).read_bytes()).hexdigest())
            bk.db_upload_chunk(sha_path, f"__backupme_db__/snapshots/{sha_path}")
            Path(sha_path).unlink()
        except Exception:
            import traceback
            traceback.print_exc()
    Path(snap["snapshot"]).unlink()
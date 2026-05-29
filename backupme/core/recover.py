import hashlib
import os
import tempfile
from pathlib import Path

from backupme.core.database import (
    DB_PATH, get_connection, get_generation, get_instance_id,
    execute, commit, initialize, check_integrity, wal_checkpoint,
)
from backupme.core.schema import SQL_SCHEMA, SQL_INDEXES, apply_migrations
from backupme.backends import get_all_backends


def find_replicas(backends: list) -> list[dict]:
    replicas = []
    for bk in backends:
        try:
            snaps = bk.db_list("snapshots")
            wals = bk.db_list("wal")
            replicas.append({
                "backend": bk.name,
                "snapshots": snaps,
                "wal_bundles": wals,
            })
        except Exception:
            pass
    return replicas


def recover_database(backends: list) -> dict:
    integrity = check_integrity()
    if not integrity:
        return {"status": "ok", "message": "Primary DB is healthy"}

    _preserve_corrupt_copy()

    candidates = []
    for bk in backends:
        try:
            candidate = _reconstruct_from_backend(bk)
            if candidate:
                candidates.append(candidate)
        except Exception:
            continue

    if not candidates:
        return rebuild_from_manifests()

    best = max(candidates, key=lambda c: c["generation"])
    src_path = best["path"]
    if src_path.exists():
        import shutil
        shutil.copy2(str(src_path), str(DB_PATH))
        wal_checkpoint()
        execute("INSERT INTO recovery_events (event_type, status, details) VALUES (?, ?, ?)",
                ("db_restore", "ok", f"Restored from backend {best['backend']} gen={best['generation']}"))
        commit()
        return {"status": "restored", "source": best["backend"], "generation": best["generation"]}

    return {"status": "failed", "message": "No recoverable replica found"}


def _preserve_corrupt_copy():
    if DB_PATH.exists():
        import shutil
        from datetime import datetime
        corrupt_path = DB_PATH.with_suffix(f".db.corrupt.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(str(DB_PATH), str(corrupt_path))


def _reconstruct_from_backend(bk) -> dict | None:
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        snaps = bk.db_list("snapshots")
        if not snaps:
            return None

        latest_snap = sorted(snaps)[-1]
        snap_local = tmp_dir / "snapshot.db.zst"
        bk.db_download(latest_snap, str(snap_local))

        import zstandard as zstd
        decompressed = tmp_dir / "snapshot.db"
        with open(snap_local, "rb") as f:
            data = f.read()
        dctx = zstd.ZstdDecompressor()
        decompressed.write_bytes(dctx.decompress(data))

        wal_paths = sorted(bk.db_list("wal"))
        db_path = tmp_dir / "backupme.db"
        import shutil
        shutil.copy2(str(decompressed), str(db_path))

        conn = get_connection(str(db_path))
        for wal_name in wal_paths:
            wal_local = tmp_dir / wal_name
            bk.db_download(wal_name, str(wal_local))
            with open(wal_local, "rb") as f:
                wal_data = f.read()
            try:
                wal_data = dctx.decompress(wal_data)
            except Exception:
                pass
            wal_local.write_bytes(wal_data)
            conn.execute(f"PRAGMA wal_checkpoint")
            conn.execute(f"PRAGMA wal_auto_apply")

        row = conn.execute("SELECT generation FROM db_meta WHERE id = 1").fetchone()
        gen = row[0] if row else 0
        conn.close()

        return {"backend": bk.name, "path": db_path, "generation": gen}
    except Exception:
        import traceback
        traceback.print_exc()
        return None
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def rebuild_from_manifests() -> dict:
    from backupme.core.manifest import rebuild_database_from_manifests

    backends = get_all_backends(None)
    result = rebuild_database_from_manifests(backends)

    execute("INSERT INTO recovery_events (event_type, status, details) VALUES (?, ?, ?)",
            ("rebuild", "ok", f"Rebuilt from manifests: {result.get('files_rebuilt', 0)} files"))
    commit()

    return {"status": "rebuilt", "files": result.get("files_rebuilt", 0)}


def restore_from_majority(backends: list) -> dict:
    return recover_database(backends)
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from backupme.log import get_logger

log = get_logger("recover")
from backupme.core import database as _dbmod
from backupme.core.database import (
    get_connection, get_generation, get_instance_id,
    execute, commit, initialize, check_integrity, wal_checkpoint,
    close_connection, _create_connection,
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
        close_connection()  # drop cached handles before replacing the file
        # Read DB_PATH through the module: the CLI rebinds it per --db-path,
        # so a from-import here would restore over a stale location.
        shutil.copy2(str(src_path), str(_dbmod.DB_PATH))
        initialize()
        wal_checkpoint()
        execute("INSERT INTO recovery_events (event_type, status, details) VALUES (?, ?, ?)",
                ("db_restore", "ok", f"Restored from backend {best['backend']} gen={best['generation']}"))
        commit()
        return {"status": "restored", "source": best["backend"], "generation": best["generation"]}

    return {"status": "failed", "message": "No recoverable replica found"}


def _preserve_corrupt_copy():
    if _dbmod.DB_PATH.exists():
        from datetime import datetime
        corrupt_path = _dbmod.DB_PATH.with_suffix(f".db.corrupt.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(str(_dbmod.DB_PATH), str(corrupt_path))


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
        dctx = zstd.ZstdDecompressor()
        with open(snap_local, "rb") as fin:
            with open(decompressed, "wb") as fout:
                with dctx.stream_reader(fin) as reader:
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        fout.write(chunk)

        wal_paths = sorted(bk.db_list("wal"))
        best_db = decompressed
        best_gen = -1
        for i, wal_name in enumerate(wal_paths):
            work = tmp_dir / f"replay_{i}"
            work.mkdir()
            shutil.copy2(str(decompressed), str(work / "backupme.db"))
            wal_local = work / "bundle.zst"
            bk.db_download(wal_name, str(wal_local))
            wal_dest = work / "backupme.db-wal"
            try:
                with open(wal_local, "rb") as fin, open(wal_dest, "wb") as fout:
                    with dctx.stream_reader(fin) as reader:
                        while True:
                            chunk = reader.read(1024 * 1024)
                            if not chunk:
                                break
                            fout.write(chunk)
            except Exception:
                shutil.copy2(str(wal_local), str(wal_dest))
            try:
                conn = _create_connection(str(work / "backupme.db"))
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                row = conn.execute("SELECT generation FROM db_meta WHERE id = 1").fetchone()
                gen = row[0] if row else 0
                conn.close()
            except Exception:
                gen = -1
            if gen >= best_gen:
                best_gen = gen
                best_db = work / "backupme.db"

        conn = _create_connection(str(best_db))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = conn.execute("SELECT generation FROM db_meta WHERE id = 1").fetchone()
        gen = row[0] if row else 0
        conn.close()

        # Copy DB out of tmp_dir so caller can use it after cleanup
        persist_dir = Path(tempfile.mkdtemp())
        persist_db = persist_dir / "backupme.db"
        shutil.copy2(str(best_db), str(persist_db))

        return {"backend": bk.name, "path": persist_db, "generation": gen}
    except Exception:
        log.exception("replica reconstruction from backend failed")
        return None
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def rebuild_from_manifests() -> dict:
    from backupme.core.manifest import rebuild_database_from_manifests
    from backupme.config import load_config
    from backupme.backends import get_all_backends

    config = load_config()
    backends = get_all_backends(config)
    result = rebuild_database_from_manifests(backends)

    execute("INSERT INTO recovery_events (event_type, status, details) VALUES (?, ?, ?)",
            ("rebuild", "ok", f"Rebuilt from manifests: {result.get('files_rebuilt', 0)} files"))
    commit()

    return {"status": "rebuilt", "files": result.get("files_rebuilt", 0)}


def restore_from_majority(backends: list) -> dict:
    return recover_database(backends)
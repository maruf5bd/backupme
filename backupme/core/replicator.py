import hashlib
import os
import time
import threading
from pathlib import Path

from backupme.core.database import get_connection, get_generation, wal_checkpoint, commit
from backupme.backends import get_all_backends


class Replicator:
    def __init__(self, config: dict, interval_snapshot: int = 3600, interval_flush: int = 60,
                 max_ops: int = 1000, max_queue_mb: int = 64):
        self.config = config
        self.interval_snapshot = interval_snapshot
        self.interval_flush = interval_flush
        self.max_ops = max_ops
        self.max_queue_bytes = max_queue_mb * 1024 * 1024
        self._pending_wal_dir = Path("pending_wal")
        self._pending_wal_dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self):
        last_flush = time.time()
        last_snapshot = time.time()
        while not self._stop.is_set():
            now = time.time()
            if now - last_flush >= self.interval_flush:
                self.flush_wal_bundle()
                last_flush = now
            if now - last_snapshot >= self.interval_snapshot:
                self.create_and_upload_snapshot()
                last_snapshot = now
            self._stop.wait(5)

    def enqueue_wal_frames(self):
        conn = get_connection()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        wal_path = Path(conn.execute("PRAGMA wal_checkpoint").fetchone()[0])
        if not wal_path or not Path(str(wal_path)).exists():
            return
        gen = get_generation()
        import shutil
        with self._lock:
            dest = self._pending_wal_dir / f"wal-{gen}.tmp"
            try:
                shutil.copy2(str(wal_path), str(dest))
            except Exception:
                import traceback
                traceback.print_exc()

    def flush_wal_bundle(self):
        with self._lock:
            files = sorted(self._pending_wal_dir.glob("wal-*.tmp"))
            if not files:
                files = sorted(self._pending_wal_dir.glob("wal-*.zst"))
            if not files:
                return
            gen = get_generation()
            bundle = self._pending_wal_dir / f"bundle-{gen}.zst"
            import zstandard as zstd
            cctx = zstd.ZstdCompressor(level=3)
            with open(bundle, "wb") as fout:
                with cctx.stream_writer(fout) as compressor:
                    for f in files:
                        compressor.write(f.read_bytes())
                        f.unlink(missing_ok=True)

            backends = get_all_backends(self.config)
            for bk in backends:
                try:
                    bk.db_upload_chunk(str(bundle), f"__backupme_db__/wal/{bundle.name}")
                except Exception:
                    import traceback
                    traceback.print_exc()
            bundle.unlink(missing_ok=True)
            sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest() if bundle.exists() else "unknown"

    def create_and_upload_snapshot(self):
        gen = get_generation()
        import time as tmod
        ts = int(tmod.time())
        snapshot_name = f"snap-{gen}-{ts}.db"
        snapshot_path = Path(snapshot_name)

        conn = get_connection()
        wal_checkpoint()
        conn.execute(f"VACUUM INTO '{snapshot_name}'")
        commit()

        import zstandard as zstd
        compressed_path = snapshot_path.with_suffix(".db.zst")
        cctx = zstd.ZstdCompressor(level=3)
        with open(compressed_path, "wb") as fout:
            with cctx.stream_writer(fout) as compressor:
                compressor.write(snapshot_path.read_bytes())
        snapshot_path.unlink()

        hash_path = compressed_path.with_suffix(compressed_path.suffix + ".sha256")
        hash_path.write_text(hashlib.sha256(compressed_path.read_bytes()).hexdigest())

        backends = get_all_backends(self.config)
        for bk in backends:
            try:
                bk.db_upload_chunk(str(compressed_path), f"__backupme_db__/snapshots/{compressed_path.name}")
                bk.db_upload_chunk(str(hash_path), f"__backupme_db__/snapshots/{hash_path.name}")
            except Exception:
                import traceback
                traceback.print_exc()

        compressed_path.unlink()
        hash_path.unlink()

        self._prune_old_wal(backends)

    def _prune_old_wal(self, backends: list):
        for bk in backends:
            try:
                wals = bk.db_list("wal")
                snaps = bk.db_list("snapshots")
                snaps.sort(reverse=True)
                if len(snaps) >= 2:
                    keep_snaps = set(snaps[:2])
                    for wal in sorted(wals):
                        gen_from_to = Path(wal).stem.replace("bundle-", "")
                        if gen_from_to:
                            parts = gen_from_to.split("-")
                            if len(parts) >= 2:
                                try:
                                    wal_gen = int(parts[0])
                                    snap_gen = max(
                                        (int(Path(s).stem.split("-")[1]) for s in snaps if Path(s).stem.split("-")[1].isdigit()),
                                        default=0,
                                    )
                                    if wal_gen < snap_gen:
                                        bk.delete(wal)
                                except (ValueError, IndexError):
                                    pass
            except Exception:
                import traceback
                traceback.print_exc()

    def verify_replica_chain(self, backends: list) -> dict:
        results = {}
        for bk in backends:
            try:
                snaps = bk.db_list("snapshots")
                wals = bk.db_list("wal")
                results[bk.name] = {"snapshots": len(snaps), "wals": len(wals), "status": "ok"}
            except Exception as e:
                results[bk.name] = {"status": "error", "error": str(e)}
        return results
import os
import threading
import time
from pathlib import Path

from backupme.core.database import initialize, get_generation, bump_generation, commit, execute
from backupme.core import snapshot as snapmod
from backupme.core import replicator
from backupme.core import retry as retry_mod
from backupme.core.skip import should_skip, should_skip_dir
from backupme.backends import get_all_backends


def apply_retention(config: dict):
    retention = config.get("retention", {})
    if not retention.get("enabled", True):
        return

    versions = retention.get("versions", 5)
    daily = retention.get("daily", 7)
    weekly = retention.get("weekly", 4)
    monthly = retention.get("monthly", 12)

    import datetime

    groups = execute(
        "SELECT DISTINCT version_group FROM files WHERE deleted_at IS NULL"
    ).fetchall()

    for (vg,) in groups:
        rows = execute(
            "SELECT id, version_seq, created_at FROM files WHERE version_group = ? AND deleted_at IS NULL ORDER BY version_seq DESC",
            (vg,),
        ).fetchall()

        if len(rows) <= versions:
            continue

        keep = set()
        for r in rows[:versions]:
            keep.add(r["id"])

        now = datetime.datetime.now()
        for r in rows:
            if r["id"] in keep:
                continue
            created = datetime.datetime.fromisoformat(r["created_at"]) if "T" in r["created_at"] else datetime.datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
            days_ago = (now - created).days

            if days_ago <= daily:
                continue
            if days_ago <= weekly * 7 and created.weekday() == 0:
                continue
            if days_ago <= monthly * 30 and created.day <= 7:
                continue

            execute("UPDATE files SET deleted_at = datetime('now') WHERE id = ?", (r["id"],))
            chunks = execute("SELECT blob_id FROM file_chunks WHERE file_id = ?", (r["id"],)).fetchall()
            for (blob_id,) in chunks:
                execute("UPDATE blobs SET ref_count = ref_count - 1 WHERE blob_id = ?", (blob_id,))
    commit()


class BackupScheduler:
    def __init__(self, config: dict):
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._replicator = replicator.Replicator(
            config,
            interval_snapshot=config.get("database", {}).get("snapshots", {}).get("interval", 3600),
        )

    def start(self):
        initialize()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._periodic_loop, daemon=True)
        self._thread.start()
        self._replicator.start()

    def stop(self):
        self._stop.set()
        self._replicator.stop()
        if self._thread:
            self._thread.join(timeout=10)

    def _periodic_loop(self):
        while not self._stop.is_set():
            self._run_backups()
            self._run_retention()
            self._stop.wait(self.config.get("scheduler", {}).get("interval", 3600))

    def _run_backups(self):
        for entry in self.config.get("backups", []):
            source = entry.get("source", "")
            target = entry.get("target", "")
            redundancy = entry.get("redundancy", 1)
            excludes = entry.get("exclude", [])

            if not source or not os.path.exists(source):
                continue

            import fnmatch
            from backupme.core.uploader import upload_file
            from backupme.backends import get_backend

            backends = get_all_backends(self.config)
            if target:
                backends = [b for b in backends if b.name == target]
            if not backends:
                continue

            src_path = Path(source)
            if src_path.is_file():
                skip, reason2 = should_skip(str(src_path))
                if skip:
                    continue
                files_to_backup = [src_path]
            else:
                files_to_backup = []
                for dirpath, dirnames, filenames in os.walk(str(src_path)):
                    dirnames[:] = [d for d in dirnames if not should_skip_dir(os.path.join(dirpath, d))]
                    for fname in filenames:
                        files_to_backup.append(Path(dirpath) / fname)

            for fp in files_to_backup:
                if not fp.is_file():
                    continue
                rel = str(fp.relative_to(src_path))
                if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
                    continue
                skip, _ = should_skip(str(fp))
                if skip:
                    continue

                try:
                    result = upload_file(
                        source_root=str(src_path),
                        relative_path=rel,
                        local_path=str(fp),
                        backends=backends,
                        config=self.config,
                        redundancy=redundancy,
                    )
                    if entry.get("move", False) and result and "file_id" in result:
                        try:
                            os.remove(str(fp))
                        except OSError:
                            pass
                except Exception:
                    import traceback
                    traceback.print_exc()

    def _run_retention(self):
        try:
            apply_retention(self.config)
        except Exception:
            import traceback
            traceback.print_exc()
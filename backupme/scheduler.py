import os
import threading
import time
from pathlib import Path

from backupme.core.database import initialize, get_generation, bump_generation, commit, execute
from backupme.core import uploader as uploader_mod
from backupme.core import snapshot as snapmod
from backupme.core import replicator
from backupme.core import retry as retry_mod
from backupme.core.skip import should_skip, should_skip_dir
from backupme.backends import get_all_backends


def collect_changes(
    source_root: str,
    excludes: list[str],
    force: bool = False,
    no_skip: bool = False,
) -> tuple[list, int]:
    """Walk a source directory and split files into backup candidates vs unchanged.

    A file is skipped only when the index has a live, fully-backed-up row
    (chunk rows exist) for the same relative path with identical size and mtime.
    """
    import fnmatch

    from backupme.core.skip import should_skip, should_skip_dir

    src_path = Path(source_root)
    from backupme.core import database as dbmod
    db_files = {
        os.path.abspath(str(dbmod.DB_PATH)),
        os.path.abspath(str(dbmod.DB_PATH)) + "-wal",
        os.path.abspath(str(dbmod.DB_PATH)) + "-shm",
    }
    rows = execute(
        """SELECT f.relative_path, f.size, f.mtime,
                  EXISTS(SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id) AS has_chunks
           FROM files f
           WHERE f.source_root = ? AND f.deleted_at IS NULL
             AND f.id = (SELECT MAX(g.id) FROM files g
                         WHERE g.source_root = f.source_root
                           AND g.relative_path = f.relative_path
                           AND g.deleted_at IS NULL)""",
        (str(src_path),),
    ).fetchall()
    known = {os.path.normpath(r["relative_path"]): r for r in rows}

    candidates: list[Path] = []
    skipped = 0

    def consider(fp: Path, rel: str) -> None:
        nonlocal skipped
        if os.path.abspath(str(fp)) in db_files:
            return  # never back up our own live database files
        if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
            return
        if not no_skip:
            skip, _ = should_skip(str(fp))
            if skip:
                return
        if not force:
            entry = known.get(os.path.normpath(rel))
            if entry is not None and entry["has_chunks"]:
                st = fp.stat()
                if entry["size"] == st.st_size and entry["mtime"] == st.st_mtime:
                    skipped += 1
                    return
        candidates.append(fp)

    if src_path.is_file():
        consider(src_path, src_path.name)
        return candidates, skipped

    for dirpath, dirnames, filenames in os.walk(str(src_path)):
        if not no_skip:
            dirnames[:] = [d for d in dirnames if not should_skip_dir(os.path.join(dirpath, d))]
        for fname in filenames:
            fp = Path(dirpath) / fname
            consider(fp, os.path.relpath(str(fp), str(src_path)))
    return candidates, skipped


def apply_retention(config: dict):
    from backupme.core import dedup as dedup_mod

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
            execute("DELETE FROM file_chunks WHERE file_id = ?", (r["id"],))
            for (blob_id,) in chunks:
                dedup_mod.unref_blob(blob_id)
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
            # "backend" is the real field name; "target" kept as a deprecated alias.
            target = entry.get("backend") or entry.get("target") or ""
            redundancy = entry.get("redundancy", 1)

            if not source or not os.path.exists(source):
                continue

            from backupme.backends import get_backend

            backends = get_all_backends(self.config)
            if target:
                backends = [b for b in backends if b.name == target]
            if not backends:
                continue

            src_path = Path(source)
            if src_path.is_file():
                skip, _ = should_skip(str(src_path))
                if skip:
                    continue
                candidates = [src_path]
                skipped = 0
            else:
                candidates, skipped = collect_changes(
                    str(src_path), entry.get("exclude", []))

            if skipped:
                print(f"{source}: {skipped} unchanged (skipped)")

            batch: list[dict] = []
            batch_bytes = 0

            def flush() -> None:
                nonlocal batch, batch_bytes
                if not batch:
                    return
                try:
                    results = uploader_mod.upload_bundle(batch, backends, self.config, redundancy)
                    for r in results:
                        if entry.get("move") and r.get("file_id") and r.get("local_path"):
                            try:
                                os.remove(r["local_path"])
                            except OSError:
                                pass
                except Exception:
                    import traceback
                    traceback.print_exc()
                batch.clear()
                batch_bytes = 0

            for fp in candidates:
                st = fp.stat()
                batch.append({
                    "local_path": str(fp),
                    "source_root": str(src_path),
                    "relative_path": os.path.relpath(str(fp), str(src_path)),
                })
                batch_bytes += st.st_size
                if len(batch) >= 200 or batch_bytes >= 64 * 1024 * 1024:
                    flush()
            flush()

    def _run_retention(self):
        try:
            apply_retention(self.config)
        except Exception:
            import traceback
            traceback.print_exc()
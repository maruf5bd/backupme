import os
import threading
import time
from pathlib import Path

from backupme.log import get_logger

log = get_logger("scheduler")
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


_DAY_SECONDS = 86400


class BackupScheduler:
    def __init__(self, config):
        import dataclasses
        if dataclasses.is_dataclass(config) and not isinstance(config, type):
            config = dataclasses.asdict(config)
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._replicator = replicator.Replicator(
            config,
            interval_snapshot=config.get("database", {}).get("snapshots", {}).get("interval", 3600),
        )
        # Cadence state: per-task last-run timestamps + single-flight locks.
        self._last_ingest = 0.0
        self._last_purge = 0.0
        self._last_export = 0.0
        self._last_gc = 0.0
        self._last_verify = 0.0
        self._last_guard = 0.0
        self._last_health: dict | None = None
        self._ingest_lock = threading.Lock()
        self._purge_lock = threading.Lock()
        self._export_lock = threading.Lock()
        self._gc_lock = threading.Lock()
        self._verify_lock = threading.Lock()
        self._guard_lock = threading.Lock()

    def _sched(self) -> dict:
        sc = self.config.get("scheduler", {}) or {}
        def _int(key: str, default: int) -> int:
            try:
                return max(1, int(sc.get(key, default)))
            except (TypeError, ValueError):
                return default
        lc = self.config.get("db_lifecycle", {}) or {}
        try:
            purge_grace = int(lc.get("purge_grace_days", 14))
        except (TypeError, ValueError):
            purge_grace = 14
        try:
            export_after = int(lc.get("cold_export_after_days", 30))
        except (TypeError, ValueError):
            export_after = 30
        return {
            "interval": _int("interval", 3600),
            "ingest_interval": _int("ingest_interval", 300),
            "gc_interval_days": _int("gc_interval_days", 7),
            "verify_interval_days": _int("verify_interval_days", 1),
            "purge_grace_days": purge_grace,
            "cold_export_after_days": export_after,
        }

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
        sc = self._sched()
        while not self._stop.is_set():
            try:
                self._run_backups()
                self._run_retention()
                now = time.time()
                if now - self._last_ingest >= sc["ingest_interval"]:
                    self._last_ingest = now
                    self._run_ingest()
                if now - self._last_purge >= _DAY_SECONDS:
                    self._last_purge = now
                    self._run_purge()
                if now - self._last_export >= sc["cold_export_after_days"] * _DAY_SECONDS:
                    self._last_export = now
                    self._run_cold_export()
                if now - self._last_gc >= sc["gc_interval_days"] * _DAY_SECONDS:
                    self._last_gc = now
                    self._run_scheduled_gc()
                if now - self._last_verify >= sc["verify_interval_days"] * _DAY_SECONDS:
                    self._last_verify = now
                    self._run_scheduled_verify()
                if now - self._last_guard >= _DAY_SECONDS:
                    self._last_guard = now
                    self._run_offsite_guard()
            except Exception:
                # One failed pass must never kill the daemon thread silently.
                log.exception("scheduler pass crashed — continuing")
            self._stop.wait(sc["interval"])

    def _ingest_enabled(self) -> bool:
        ci = self.config.get("cpanel_ingest", {}) or {}
        return bool(ci.get("enabled", False))

    def _guard_enabled(self) -> bool:
        g = self.config.get("guard", {}) or {}
        return bool(g.get("offsite_snapshots", True))

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
                log.info("%s: %d unchanged (skipped)", source, skipped)

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
                    log.exception("batch upload failed")
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
            log.exception("retention pass failed")

    def _run_ingest(self):
        """Ingest tick: scan the drop root for new cpmove archives."""
        if not self._ingest_enabled():
            return
        if not self._ingest_lock.acquire(blocking=False):
            log.info("ingest tick skipped — previous ingest still running")
            return
        try:
            ci = self.config.get("cpanel_ingest", {}) or {}
            drop_root = ci.get("drop_root", "")
            if not drop_root or not os.path.isdir(drop_root):
                return
            from backupme.core import cpanel_ingest as ci_mod
            backends = get_all_backends(self.config)
            result = ci_mod.ingest_tree(drop_root, backends, self.config)
            log.info("ingest tick: status=%s archives=%s",
                     result.get("status"), result.get("archives"))
        except Exception:
            log.exception("ingest tick failed")
        finally:
            self._ingest_lock.release()

    def _run_purge(self):
        """Purge tick: hard-delete exported, grace-expired rows."""
        if not self._ingest_enabled():
            return
        if not self._purge_lock.acquire(blocking=False):
            log.info("purge tick skipped — previous purge still running")
            return
        try:
            from backupme.core import db_lifecycle as lc
            result = lc.hard_purge_versions(config=self.config)
            log.info("purge tick: purged=%s above_watermark=%s",
                     result.get("purged"), result.get("above_watermark"))
        except Exception:
            log.exception("purge tick failed")
        finally:
            self._purge_lock.release()

    def _run_cold_export(self):
        """Cold-export tick: write new files rows to monthly Parquet partitions."""
        if not self._ingest_enabled():
            return
        if not self._export_lock.acquire(blocking=False):
            log.info("cold-export tick skipped — previous export still running")
            return
        try:
            from backupme.core import db_lifecycle as lc
            result = lc.cold_export_to_parquet(config=self.config)
            log.info("cold-export tick: status=%s rows=%s",
                     result.get("status"), result.get("rows"))
        except Exception:
            log.exception("cold-export tick failed")
        finally:
            self._export_lock.release()

    def _run_offsite_guard(self):
        """Off-site guard tick: ship a fresh DB snapshot to every backend.

        The DB is the only map of the vault; this daily tick is the insurance
        copy. Gated on guard.offsite_snapshots; never deletes anything.
        """
        if not self._guard_enabled():
            return
        if not self._guard_lock.acquire(blocking=False):
            log.info("offsite-guard tick skipped — previous guard still running")
            return
        try:
            from backupme.core import offsite_guard
            result = offsite_guard.ship_snapshot(self.config)
            log.info("offsite-guard tick: status=%s backends=%s",
                     result.get("status"), sorted(result.get("backends", {})))
        except Exception:
            log.exception("offsite-guard tick failed")
        finally:
            self._guard_lock.release()

    def _run_scheduled_gc(self):
        if not self._gc_lock.acquire(blocking=False):
            log.info("gc tick skipped — previous gc still running")
            return
        try:
            from backupme.core import gc as gc_mod
            backends = get_all_backends(self.config)
            result = gc_mod.run_gc(backends, apply=True)
            log.info("gc tick: %s", {k: v for k, v in result.items()
                                     if isinstance(v, (int, str))})
        except Exception:
            log.exception("gc tick failed")
        finally:
            self._gc_lock.release()

    def _run_scheduled_verify(self):
        if not self._verify_lock.acquire(blocking=False):
            log.info("verify tick skipped — previous verify still running")
            return
        try:
            from backupme.core import verify as verify_mod
            result = verify_mod.run_integrity_check(count=5)
            log.info("verify tick: status=%s failed=%s",
                     result.get("status"), result.get("failed_files"))
        except Exception:
            log.exception("verify tick failed")
        try:
            self._log_health()
        except Exception:
            log.exception("health tick failed")
        finally:
            self._verify_lock.release()

    def _log_health(self):
        """Log the data-health summary each verify tick; warn on regressions."""
        from backupme.core import data_health

        s = data_health.summary()
        tracked = ("live_files", "no_chunks_files", "missing_blob_files",
                   "missing_blobs", "same_name_groups", "purge_candidates")
        log.info("health tick: live=%s no_chunks=%s missing_blob_files=%s "
                 "missing_blobs=%s same_name_groups=%s purge_candidates=%s",
                 s["live_files"], *(s[k] for k in tracked[1:]))
        regressions = []
        if self._last_health:
            regressions = [
                f"{k}: {self._last_health[k]} -> {s[k]}"
                for k in tracked[1:]
                if s[k] > self._last_health.get(k, 0)
            ]
        self._last_health = {k: s[k] for k in tracked}
        if regressions:
            log.warning("data health regression: %s", "; ".join(regressions))
            from backupme.core import notify
            notify.notify_failure("Data health regression",
                                  "; ".join(regressions), self.config)
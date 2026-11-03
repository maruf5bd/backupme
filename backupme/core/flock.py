"""Cross-process advisory locks via O_CREAT|O_EXCL lock files.

Windows-safe (no fcntl/portalocker dependency). Used to serialize
multi-process maintenance operations: cold export, rebalance, ingest scans.
Stale locks (crashed holder) are taken over after ``stale_seconds``.
"""
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path


def safe_name(name: str) -> str:
    """Filesystem-safe component for lock/checkpoint file names."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "unnamed"


@contextmanager
def path_lock(lock_path, stale_seconds: float = 1800.0, wait: float = 0.0,
              poll: float = 0.1):
    """Acquire an exclusive lock file.

    Raises TimeoutError immediately when ``wait`` elapses without success
    (``wait=0`` = non-blocking). A lock file older than ``stale_seconds`` is
    considered abandoned and taken over.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(0.0, wait)
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > stale_seconds:
                # Abandoned by a crashed holder — take over.
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"lock busy: {lock_path}")
            time.sleep(poll)
    try:
        try:
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            pass
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass

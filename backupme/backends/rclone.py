import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .base import StorageBackend
from ..log import get_logger

log = get_logger("rclone_backend")

_STDERR_TAIL_CHARS = 300
_LIST_TIMEOUT_CAP_S = 4 * 3600   # listings must never pin a worker for longer than this
_SHORT_TIMEOUT_S = 60            # single-object probes (exists/delete) stay fast


class RcloneBackend(StorageBackend):
    """Storage backend driving the rclone binary (50+ remote types).

    Hardened after a 10-hour real-world SFTP operation where paramiko's
    parallel sessions were refused by the server-side limiter while rclone
    sailed through — but the old short per-op timeouts would have killed
    multi-GB transfers (single stream measured 3.17 MB/s on a 208 ms RTT
    link, so a 300 s budget covers barely 1 GB). Therefore:

    - every op runs with ``timeout_s`` (default 21600 = 6 h, from config);
    - ``--config <config_path>`` is passed on EVERY call when set, so ops
      never silently fall back to the default rclone.conf;
    - ``extra_args`` (e.g. ``--sftp-set-modtime=false``) is appended to every
      call;
    - every op logs remote path + duration + rc at INFO; failures log the
      stderr tail (truncated) at WARNING/ERROR — a silent empty listing has
      cost hours of debugging before, so list failures are loud.
    """

    @property
    def type(self) -> str:
        return "rclone"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.remote = config.get("remote", "")
        self.bwlimit = config.get("bwlimit", "")
        self.config_path = str(config.get("config_path") or "")
        extra_args = config.get("extra_args") or []
        if isinstance(extra_args, str):
            extra_args = extra_args.split()
        self.extra_args = [str(flag) for flag in extra_args]
        try:
            self.timeout_s = int(config.get("timeout_s", 21600))
        except (TypeError, ValueError):
            log.warning("[%s] bad timeout_s %r; using default 21600",
                        name, config.get("timeout_s"))
            self.timeout_s = 21600
        if self.timeout_s <= 0:
            log.warning("[%s] non-positive timeout_s %s; using default 21600",
                        name, self.timeout_s)
            self.timeout_s = 21600

    def _args(self) -> list[str]:
        cmd = ["rclone"]
        if self.config_path:
            cmd.extend(["--config", self.config_path])
        cmd.extend(self.extra_args)
        if self.bwlimit:
            cmd.extend(["--bwlimit", self.bwlimit])
        return cmd

    def _dest(self, rel_path: str = "") -> str:
        dest = self.remote
        if rel_path:
            dest = f"{dest}/{rel_path}"
        return dest

    @staticmethod
    def _stderr_tail(text) -> str:
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        tail = (text or "").strip()
        return tail[-_STDERR_TAIL_CHARS:] if len(tail) > _STDERR_TAIL_CHARS else tail

    def _run(self, op_args: list[str], *, op: str, target: str, timeout: float,
             failure_level: int = logging.WARNING) -> subprocess.CompletedProcess:
        """Run one rclone invocation and log remote path, duration and rc.

        Success -> INFO; non-zero rc -> ``failure_level`` with a stderr tail
        (truncated); timeout -> ERROR + IOError (callers decide recovery).
        """
        cmd = self._args() + op_args
        started = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            tail = self._stderr_tail(exc.stderr)
            log.error("[%s] rclone %s TIMEOUT after %.0fs (budget %.0fs): %s stderr=%s",
                      self.name, op, duration, timeout, target, tail)
            raise IOError(
                f"rclone {op} timed out after {duration:.0f}s (budget {timeout:.0f}s): "
                f"{target} stderr={tail}"
            ) from exc
        duration = time.monotonic() - started
        if result.returncode == 0:
            log.info("[%s] rclone %s rc=0: %s (%.2fs)",
                     self.name, op, target, duration)
        else:
            log.log(failure_level,
                    "[%s] rclone %s rc=%s: %s (%.2fs) stderr=%s",
                    self.name, op, result.returncode, target, duration,
                    self._stderr_tail(result.stderr))
        return result

    def connect(self) -> bool:
        dest = self._dest()
        try:
            result = self._run(["lsf", dest], op="connect", target=dest, timeout=15)
        except (IOError, OSError) as exc:
            log.warning("[%s] rclone connect probe errored: %s", self.name, exc)
            self._connected = False
            return self._connected
        self._connected = result.returncode == 0
        return self._connected

    def disconnect(self):
        self._connected = False

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        dest = self._dest(remote_rel_path)
        result = self._run(["copyto", local_path, dest],
                           op="upload", target=dest, timeout=self.timeout_s)
        if result.returncode != 0:
            raise IOError(f"rclone upload failed rc={result.returncode}: "
                          f"{self._stderr_tail(result.stderr)}")
        sha = hashlib.sha256()
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        dest = self._dest(remote_rel_path)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        result = self._run(["copyto", dest, local_path],
                           op="download", target=dest, timeout=self.timeout_s)
        if result.returncode != 0:
            raise IOError(f"rclone download failed rc={result.returncode}: "
                          f"{self._stderr_tail(result.stderr)}")

    def delete(self, remote_rel_path: str):
        dest = self._dest(remote_rel_path)
        result = self._run(["delete", dest],
                           op="delete", target=dest, timeout=_SHORT_TIMEOUT_S)
        if result.returncode != 0:
            raise IOError(f"rclone delete failed rc={result.returncode}: "
                          f"{self._stderr_tail(result.stderr)}")

    def exists(self, remote_rel_path: str) -> bool:
        dest = self._dest(remote_rel_path)
        try:
            result = self._run(["lsf", dest], op="exists", target=dest,
                               timeout=_SHORT_TIMEOUT_S, failure_level=logging.DEBUG)
            return result.returncode == 0
        except (IOError, OSError):
            # A missing path is a normal negative answer in verify/dedup/namespace
            # flows (logged at DEBUG by _run, not WARNING — it would flood);
            # timeouts and other hard errors are already logged by _run.
            return False

    def list_files(self, prefix: str = "") -> list[dict]:
        target = self._dest(prefix)
        timeout = min(self.timeout_s, _LIST_TIMEOUT_CAP_S)
        try:
            result = self._run(["lsjson", "-R", "--files-only", target],
                               op="list", target=target, timeout=timeout)
        except (IOError, OSError) as exc:
            log.warning("[%s] rclone list of %s failed (%s); returning EMPTY listing",
                        self.name, target, exc)
            return []
        if result.returncode != 0:
            log.warning("[%s] rclone list of %s failed rc=%s; returning EMPTY listing",
                        self.name, target, result.returncode)
            return []
        try:
            items = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.warning("[%s] rclone list of %s returned unparseable JSON; returning EMPTY listing",
                        self.name, target)
            return []
        out = []
        for item in items:
            if item.get("IsDir"):
                continue
            path = item.get("Path", "")
            if prefix:
                path = f"{prefix}/{path}"
            out.append({
                "path": path,
                "size": item.get("Size", 0),
                "mtime": item.get("ModTime", ""),
            })
        return out

    def walk(self, prefix: str = "") -> list[dict]:
        return self.list_files(prefix)

    def _about(self) -> dict | None:
        result = self._run(["about", self._dest(), "--json"], op="about",
                           target=self._dest(), timeout=30,
                           failure_level=logging.DEBUG)
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            log.debug("[%s] rclone about returned unparseable JSON", self.name)
            return None

    def free_space(self) -> int | None:
        data = self._about()
        if data is None:
            return None
        try:
            return int(data.get("free", 0) or 0)
        except (TypeError, ValueError):
            return None

    def total_space(self) -> int | None:
        data = self._about()
        if data is None:
            return None
        try:
            return int(data.get("total", 0) or 0)
        except (TypeError, ValueError):
            return None

    def hash(self, remote_rel_path: str) -> str:
        dest = self._dest(remote_rel_path)
        # NOTE: on SFTP remotes `rclone hashsum SHA256` DOWNLOADS the file
        # (most SFTP servers have no native SHA256) — that is the correct
        # verify semantics, but it means hashing a multi-GB blob over a slow
        # link needs the full timeout_s budget, not a short per-op one.
        result = self._run(["hashsum", "SHA256", dest],
                           op="hash", target=dest, timeout=self.timeout_s)
        if result.returncode != 0:
            raise FileNotFoundError(
                f"Could not hash {remote_rel_path}: {self._stderr_tail(result.stderr)}")
        parts = result.stdout.strip().split()
        if not parts:
            raise FileNotFoundError(
                f"Could not hash {remote_rel_path}: hashsum returned no output")
        return parts[0]

    def health(self) -> bool:
        try:
            return self.connect()
        except Exception:
            return False

    def fingerprint(self, remote_rel_path: str) -> dict:
        target = self._dest(remote_rel_path)
        info = self._run(["lsjson", target], op="stat", target=target,
                         timeout=30, failure_level=logging.DEBUG)
        size = 0
        mtime = None
        if info.returncode == 0:
            try:
                items = json.loads(info.stdout)
                if items:
                    size = items[0].get("Size", 0)
                    mtime = items[0].get("ModTime", None)
            except json.JSONDecodeError:
                pass
        if size == 0:
            return {"size": 0, "mtime": mtime, "head_tail_sha256": ""}
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp")
        os.close(fd)
        try:
            result = self._run(["copyto", target, tmp_path],
                               op="fingerprint-download", target=target,
                               timeout=self.timeout_s)
            if result.returncode != 0:
                return {"size": size, "mtime": mtime, "head_tail_sha256": ""}
            p = Path(tmp_path)
            if p.exists():
                with open(tmp_path, "rb") as f:
                    head = f.read(4096)
                    if size > 4096:
                        f.seek(-4096, 2)
                        tail = f.read(4096)
                    else:
                        tail = head
                h = hashlib.sha256(head + tail).hexdigest()
                return {"size": size, "mtime": mtime, "head_tail_sha256": h}
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return {"size": size, "mtime": mtime, "head_tail_sha256": ""}

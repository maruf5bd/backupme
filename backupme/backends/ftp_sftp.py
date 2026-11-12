import hashlib
import io
import os
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

from .base import StorageBackend
from backupme.log import get_logger

log = get_logger("backends.ftp_sftp")


class FTPBackend(StorageBackend):
    @property
    def type(self) -> str:
        return "ftp"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.host = config.get("host", "localhost")
        self.port = int(config.get("port", 21))
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.base_path = config.get("path", "/")
        self._ftp = None

    def connect(self) -> bool:
        try:
            from ftplib import FTP
            ftp = FTP()
            ftp.connect(self.host, self.port, timeout=30)
            ftp.login(self.username, self.password)
            ftp.cwd(self.base_path)
            self._ftp = ftp
            self._connected = True
            return True
        except Exception:
            return False

    def disconnect(self):
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:
                self._ftp.close()
            self._ftp = None
        self._connected = False

    def _reconnect(self):
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")
                return
            except Exception:
                self.disconnect()
        self.connect()

    def _ensure_dir(self, path: str):
        parts = [p for p in path.split("/") if p]
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            try:
                self._ftp.cwd(sub)
            except Exception:
                self._ftp.mkd(sub)
        self._ftp.cwd(self.base_path)

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        self._reconnect()
        dir_part = remote_rel_path.rsplit("/", 1)[0] if "/" in remote_rel_path else ""
        if dir_part:
            self._ensure_dir(dir_part)
        sha = hashlib.sha256()
        with open(local_path, "rb") as f:
            self._ftp.storbinary(f"STOR {remote_rel_path}", f, callback=sha.update)
        return sha.hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        self._reconnect()
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "wb") as f:
            self._ftp.retrbinary(f"RETR {remote_rel_path}", f.write)

    def delete(self, remote_rel_path: str):
        self._reconnect()
        try:
            self._ftp.delete(remote_rel_path)
        except FileNotFoundError:
            pass

    def exists(self, remote_rel_path: str) -> bool:
        self._reconnect()
        try:
            self._ftp.size(remote_rel_path)
            return True
        except Exception:
            return False

    def list_files(self, prefix: str = "") -> list[dict]:
        self._reconnect()
        result = []
        try:
            lines = []
            self._ftp.retrlines(f"LIST {prefix}", lines.append)
            for line in lines:
                parsed = self._parse_list(line)
                if parsed:
                    result.append(parsed)
        except Exception:
            pass
        return result

    def walk(self, prefix: str = "") -> list[dict]:
        return self._walk_recursive(prefix)

    def _walk_recursive(self, prefix: str = "") -> list[dict]:
        self._reconnect()
        items = []
        try:
            lines = []
            self._ftp.retrlines(f"LIST {prefix}", lines.append)
            for line in lines:
                parsed = self._parse_list(line)
                if parsed is None:
                    continue
                rel_path = f"{prefix}/{parsed['path']}" if prefix else parsed["path"]
                if parsed.get("is_dir"):
                    items.extend(self._walk_recursive(rel_path))
                else:
                    items.append({"path": rel_path, "size": parsed["size"], "mtime": parsed["mtime"]})
        except Exception as e:
            log.warning("ftp walk: skipping %s: %s", prefix or "/", e)
        return items

    @staticmethod
    def _parse_list(line: str) -> dict | None:
        if not line or line.startswith("total"):
            return None
        parts = line.split()
        if len(parts) < 9:
            return None
        is_dir = parts[0].startswith("d")
        try:
            size = int(parts[4])
        except ValueError:
            size = 0
        return {"path": " ".join(parts[8:]), "size": size, "mtime": None, "is_dir": is_dir}

    def free_space(self) -> int | None:
        self._reconnect()
        try:
            resp = self._ftp.voidcmd("STAT .")
            for line in resp.splitlines():
                if "free" in line.lower() or "avail" in line.lower():
                    import re
                    nums = re.findall(r"\d+", line)
                    if nums:
                        return int(nums[-1])
        except Exception:
            pass
        return None

    def total_space(self) -> int | None:
        return None

    def hash(self, remote_rel_path: str) -> str:
        self._reconnect()
        sha = hashlib.sha256()
        def _hash_cb(data):
            sha.update(data)
        self._ftp.retrbinary(f"RETR {remote_rel_path}", _hash_cb)
        return sha.hexdigest()

    def health(self) -> bool:
        try:
            self._reconnect()
            self._ftp.voidcmd("NOOP")
            return True
        except Exception:
            return False

    def fingerprint(self, remote_rel_path: str) -> dict:
        self._reconnect()
        size = 0
        try:
            size = self._ftp.size(remote_rel_path)
        except Exception:
            pass
        head = b""
        tail_buf = bytearray()
        total_read = 0
        _FP_CHUNK = 4096

        def _fp_cb(data):
            nonlocal head, total_read
            total_read += len(data)
            if len(head) < _FP_CHUNK:
                need = _FP_CHUNK - len(head)
                head += data[:need]
                data = data[need:]
            if data:
                tail_buf.extend(data)
                if len(tail_buf) > _FP_CHUNK:
                    del tail_buf[:len(tail_buf) - _FP_CHUNK]

        self._ftp.retrbinary(f"RETR {remote_rel_path}", _fp_cb)
        tail = bytes(tail_buf) if tail_buf else head
        h = hashlib.sha256(head + tail).hexdigest()
        return {"size": size, "mtime": None, "head_tail_sha256": h}


class SFTPBackend(StorageBackend):
    @property
    def type(self) -> str:
        return "sftp"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.host = config.get("host", "localhost")
        self.port = int(config.get("port", 22))
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.key_file = config.get("key_file", "")
        self.base_path = config.get("path", "/")
        self._sftp = None
        self._transport = None
        # Single-flight connect: concurrent callers (dashboard poll + upload
        # worker) used to stampede the server with parallel SSH sessions —
        # that session churn was the root cause of the Storage Box
        # rate-limit outage (defect D10).
        self._connect_lock = threading.Lock()

    def connect(self) -> bool:
        """Connect to the SFTP server.

        Tries key-based authentication first (if ``key_file`` is set), then
        password authentication. Hetzner Storage Boxes rate-limit new SSH
        sessions, so the connect path is hardened (defect D10 follow-up):
        - a per-instance lock single-flights concurrent connects, and an
          already-connected instance is reused as-is;
        - TRANSIENT errors (paramiko.SSHException such as "Unable to open
          channel", EOFError, socket.timeout, generic OSError) retry 3
          attempts total with 0.5s then 1.5s backoff;
        - DEFINITIVE failures fail immediately — retrying cannot succeed and
          only hammers the host: AuthenticationException / BadHostKeyException
          (bad credentials), ConnectionRefusedError (a reachable peer
          authoritatively declined the port — not a rate limit), and
          socket.gaierror (bad host name).
        """
        with self._connect_lock:
            if self._connected and self._sftp is not None:
                return True
            import paramiko
            # Order matters: AuthenticationException / BadHostKeyException
            # subclass SSHException, so they are matched first (no retry).
            immediate_fail = (paramiko.AuthenticationException,
                              paramiko.BadHostKeyException,
                              ConnectionRefusedError, socket.gaierror)
            transient = (paramiko.SSHException, EOFError, socket.timeout, OSError)
            last_transient: Exception | None = None
            for attempt in range(3):
                try:
                    return self._connect_attempt(paramiko)
                except immediate_fail as e:
                    log.warning("SFTP auth failed for %s (no retry): %s",
                                self.name, e)
                    return False
                except transient as e:
                    last_transient = e
                    log.warning("SFTP transient connect failure for %s "
                                "(attempt %d/3): %s", self.name, attempt + 1, e)
                    if attempt < 2:
                        time.sleep(0.5 if attempt == 0 else 1.5)
                except Exception as e:
                    log.warning("SFTP connect failed for %s: %s", self.name, e)
                    return False
            log.warning("SFTP connect to %s failed after 3 attempts: %s",
                        self.name, last_transient)
            return False

    def _connect_attempt(self, paramiko) -> bool:
        """One full connect pass: key auth (if configured), then password
        auth. Raises on failure — the caller owns the transient retry and
        the auth fast-fail. A key-stage failure (including a rejected key)
        falls through to the password stage, as before."""
        if self.key_file:
            try:
                key = paramiko.RSAKey.from_private_key_file(
                    os.path.expanduser(self.key_file)
                )
                transport = self._open_transport(paramiko)
                transport.connect(username=self.username, pkey=key)
                self._open_channel(transport, paramiko)
                self._transport = transport
                self._sftp.chdir(self.base_path)
                self._connected = True
                return True
            except Exception as ke:
                log.warning("SFTP key auth failed for %s: %s", self.name, ke)
                self.disconnect()
        try:
            transport = self._open_transport(paramiko)
            transport.connect(username=self.username, password=self.password)
            self._open_channel(transport, paramiko)
            self._transport = transport
            self._sftp.chdir(self.base_path)
            self._connected = True
            return True
        except Exception as pe:
            log.warning("SFTP password auth failed for %s: %s", self.name, pe)
            try:
                transport.close()
            except Exception:
                pass
            raise

    def _open_transport(self, paramiko):
        # A bare Transport has no TCP timeout; create a pre-configured
        # socket and cap the SSH banner/auth exchanges too — without
        # banner_timeout a blackholed server hangs transport.connect()
        # forever (it waits on a threading.Event, not on the socket).
        sock = socket.create_connection((self.host, self.port), timeout=15)
        # Large window + packet sizes are required for real throughput:
        # paramiko's defaults (2 MiB window, 32 KiB packets) turn every
        # read/write into hundreds of synchronous round trips — measured
        # ~80 KB/s on the Storage Box link vs tens of MB/s after this.
        try:
            t = paramiko.Transport(
                sock,
                default_window_size=64 * 1024 * 1024,
                default_max_packet_size=1024 * 1024,
            )
        except TypeError:  # very old paramiko without the kwargs
            t = paramiko.Transport(sock)
            t.window_size = 64 * 1024 * 1024
            t.max_packet_size = 1024 * 1024
        # Older paramiko versions don't take these as constructor kwargs;
        # they are plain instance attributes on Transport.
        t.banner_timeout = 15
        t.auth_timeout = 15
        return t

    def _open_channel(self, transport, paramiko) -> None:
        # SFTPClient.from_transport opens the session channel. Rate-limited
        # servers reject new channels ("Unable to open channel") for a few
        # seconds — that transient case is retried by connect()'s bounded
        # 3-attempt loop now, so a failure here bubbles up instead of
        # spinning the old unbounded inner sleep loop.
        self._sftp = paramiko.SFTPClient.from_transport(transport)

    def disconnect(self):
        if self._sftp:
            self._sftp.close()
        if self._transport:
            self._transport.close()
        self._connected = False
        self._sftp = None
        self._transport = None

    def _reconnect(self):
        if not self._connected:
            self.connect()

    def _ensure_dir(self, path: str):
        parts = [p for p in path.split("/") if p]
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            try:
                self._sftp.stat(sub)
            except FileNotFoundError:
                self._sftp.mkdir(sub)

    def _remote_path(self, rel_path: str) -> str:
        return f"{self.base_path}/{rel_path}".replace("//", "/")

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        self._reconnect()
        dir_part = remote_rel_path.rsplit("/", 1)[0] if "/" in remote_rel_path else ""
        if dir_part:
            self._ensure_dir(dir_part)
        sha = hashlib.sha256()
        with open(local_path, "rb") as src:
            with self._sftp.open(remote_rel_path, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    sha.update(chunk)
        return sha.hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        self._reconnect()
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self._sftp.get(remote_rel_path, local_path)

    def delete(self, remote_rel_path: str):
        self._reconnect()
        try:
            self._sftp.remove(remote_rel_path)
        except FileNotFoundError:
            pass

    def exists(self, remote_rel_path: str) -> bool:
        self._reconnect()
        try:
            self._sftp.stat(remote_rel_path)
            return True
        except Exception:
            return False

    def list_files(self, prefix: str = "") -> list[dict]:
        self._reconnect()
        result = []
        try:
            items = self._sftp.listdir_attr(prefix) if prefix else self._sftp.listdir_attr()
            for attr in items:
                if not attr.filename.startswith("."):
                    rel = f"{prefix}/{attr.filename}" if prefix else attr.filename
                    if not stat.S_ISDIR(attr.st_mode):
                        result.append({"path": rel, "size": attr.st_size, "mtime": attr.st_mtime})
        except Exception:
            pass
        return result

    def _listdir_retry(self, prefix: str = ""):
        """listdir_attr with reconnect+backoff: a dropped SFTP session mid-walk
        must retry instead of silently truncating the listing."""
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                if prefix:
                    return self._sftp.listdir_attr(prefix)
                return self._sftp.listdir_attr()
            except Exception as e:
                last_exc = e
                time.sleep(2 * (attempt + 1))
                try:
                    self._reconnect()
                except Exception:
                    pass
        raise last_exc if last_exc else RuntimeError("listdir failed")

    def walk(self, prefix: str = "") -> list[dict]:
        self.walk_errors = 0
        return self._walk_recursive(prefix)

    def _walk_recursive(self, prefix: str = "") -> list[dict]:
        self._reconnect()
        items = []
        try:
            for attr in self._listdir_retry(prefix):
                rel = f"{prefix}/{attr.filename}" if prefix else attr.filename
                if stat.S_ISDIR(attr.st_mode):
                    items.extend(self._walk_recursive(rel))
                else:
                    items.append({"path": rel, "size": attr.st_size, "mtime": attr.st_mtime})
        except Exception as e:
            # Never silently truncate: record the gap so callers can detect
            # an incomplete listing (walk_errors > 0 means partial data).
            self.walk_errors = getattr(self, "walk_errors", 0) + 1
            log.warning("sftp walk: skipping %s after retries: %s", prefix or "/", e)
        return items

    def free_space(self) -> int | None:
        self._reconnect()
        try:
            st = self._sftp.statvfs(self.base_path)
            return st.f_frsize * st.f_bavail
        except Exception:
            return None

    def total_space(self) -> int | None:
        self._reconnect()
        try:
            st = self._sftp.statvfs(self.base_path)
            return st.f_frsize * st.f_blocks
        except Exception:
            return None

    def hash(self, remote_rel_path: str) -> str:
        self._reconnect()
        sha = hashlib.sha256()
        with self._sftp.open(remote_rel_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()

    def health(self) -> bool:
        try:
            self._reconnect()
            self._sftp.listdir(".")
            return True
        except Exception:
            return False

    def fingerprint(self, remote_rel_path: str) -> dict:
        self._reconnect()
        attr = self._sftp.stat(remote_rel_path)
        head = b""
        tail = b""
        with self._sftp.open(remote_rel_path, "rb") as f:
            head = f.read(4096)
            if attr.st_size > 4096:
                f.seek(-4096, 2)
                tail = f.read(4096)
            else:
                tail = head
        h = hashlib.sha256(head + tail).hexdigest()
        return {"size": attr.st_size, "mtime": attr.st_mtime, "head_tail_sha256": h}
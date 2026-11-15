import hashlib
import os
import time
from pathlib import Path

from .base import StorageBackend
from backupme.log import get_logger

log = get_logger("backends.smb")


class SMBBackend(StorageBackend):
    @property
    def type(self) -> str:
        return "smb"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.host = config.get("host", "localhost")
        self.share = config.get("share", "backups")
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.base_path = config.get("path", "")

    def connect(self) -> bool:
        try:
            from smbclient import register_session
            register_session(self.host, username=self.username, password=self.password)
            self._connected = True
            return True
        except Exception:
            return False

    def disconnect(self):
        self._connected = False

    @staticmethod
    def _sanitize(rel_path: str) -> str:
        parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != ".."]
        return "/".join(parts)

    def _smb_path(self, rel_path: str = "") -> str:
        parts = [f"//{self.host}/{self.share}"]
        if self.base_path:
            parts.append(self.base_path)
        if rel_path:
            parts.append(self._sanitize(rel_path))
        return "/".join(parts).replace("\\", "/")

    def _ensure_dir(self, path: str):
        from smbclient import mkdir, stat as smb_stat
        parts = [p for p in path.split("/") if p]
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            try:
                smb_stat(sub)
            except Exception:
                try:
                    mkdir(sub)
                except Exception:
                    pass

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        from smbclient import open_file
        full = self._smb_path(remote_rel_path)
        dir_part = full.rsplit("/", 1)[0]
        self._ensure_dir(dir_part)
        sha = hashlib.sha256()
        with open(local_path, "rb") as src:
            with open_file(full, mode="wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    sha.update(chunk)
        return sha.hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        from smbclient import open_file
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        with open_file(self._smb_path(remote_rel_path), mode="rb") as src:
            with open(local_path, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)

    def delete(self, remote_rel_path: str):
        from smbclient import remove
        full = self._smb_path(remote_rel_path)
        try:
            remove(full)
        except FileNotFoundError:
            pass

    def exists(self, remote_rel_path: str) -> bool:
        from smbclient import stat as smb_stat
        try:
            smb_stat(self._smb_path(remote_rel_path))
            return True
        except Exception:
            return False

    def _listdir_retry(self, path: str):
        """listdir with backoff: a transient SMB hiccup mid-walk must retry
        instead of silently truncating the listing."""
        from smbclient import listdir
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return listdir(path)
            except Exception as e:
                last_exc = e
                time.sleep(2 * (attempt + 1))
                try:
                    self.connect()
                except Exception:
                    pass
        raise last_exc if last_exc else RuntimeError("listdir failed")

    def list_files(self, prefix: str = "") -> list[dict]:
        self.walk_errors = 0
        from smbclient import stat as smb_stat
        import stat as statmod
        result = []
        base = self._smb_path(prefix) if prefix else self._smb_path()
        try:
            entries = self._listdir_retry(base)
        except Exception as e:
            # Never silently truncate: record the gap so callers can detect
            # an incomplete listing (walk_errors > 0 means partial data).
            self.walk_errors += 1
            log.warning("smb list: cannot list %s after retries: %s", base, e)
            return result
        for entry in entries:
            name = str(entry).split("/")[-1]
            rel = f"{prefix}/{name}" if prefix else name
            try:
                s = smb_stat(f"{base}/{name}")
                if not statmod.S_ISDIR(s.st_mode):
                    result.append({"path": rel, "size": s.st_size, "mtime": s.st_mtime})
            except Exception as e:
                self.walk_errors += 1
                log.warning("smb list: skipping %s: %s", rel, e)
        return result

    def walk(self, prefix: str = "") -> list[dict]:
        self.walk_errors = 0
        return self._walk_recursive(prefix)

    def _walk_recursive(self, prefix: str = "") -> list[dict]:
        from smbclient import stat as smb_stat
        import stat as statmod
        items = []
        base = self._smb_path(prefix) if prefix else self._smb_path()
        try:
            entries = self._listdir_retry(base)
        except Exception as e:
            self.walk_errors += 1
            log.warning("smb walk: skipping %s after retries: %s", prefix or "/", e)
            return items
        for entry in entries:
            name = str(entry).split("/")[-1]
            rel = f"{prefix}/{name}" if prefix else name
            full = f"{base}/{name}"
            try:
                s = smb_stat(full)
                if statmod.S_ISDIR(s.st_mode):
                    items.extend(self._walk_recursive(rel))
                else:
                    items.append({"path": rel, "size": s.st_size, "mtime": s.st_mtime})
            except Exception as e:
                self.walk_errors += 1
                log.warning("smb walk: skipping %s: %s", rel, e)
        return items

    def free_space(self) -> int | None:
        return None

    def total_space(self) -> int | None:
        return None

    def hash(self, remote_rel_path: str) -> str:
        from smbclient import open_file
        sha = hashlib.sha256()
        with open_file(self._smb_path(remote_rel_path), mode="rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()

    def health(self) -> bool:
        try:
            from smbclient import listdir
            listdir(self._smb_path())
            return True
        except Exception:
            return False

    def fingerprint(self, remote_rel_path: str) -> dict:
        from smbclient import open_file, stat as smb_stat
        s = smb_stat(self._smb_path(remote_rel_path))
        head = b""
        tail = b""
        with open_file(self._smb_path(remote_rel_path), mode="rb") as f:
            head = f.read(4096)
            if s.st_size > 4096:
                f.seek(-4096, 2)
                tail = f.read(4096)
            else:
                tail = head
        h = hashlib.sha256(head + tail).hexdigest()
        return {"size": s.st_size, "mtime": s.st_mtime, "head_tail_sha256": h}
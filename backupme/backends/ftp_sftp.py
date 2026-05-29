import hashlib
import io
import os
import stat
import tempfile
from pathlib import Path

from .base import StorageBackend


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
        with open(local_path, "rb") as f:
            data = f.read()
        buf = io.BytesIO(data)
        self._ftp.storbinary(f"STOR {remote_rel_path}", buf)
        return hashlib.sha256(data).hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        self._reconnect()
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        self._ftp.retrbinary(f"RETR {remote_rel_path}", buf.write)
        with open(local_path, "wb") as f:
            f.write(buf.getvalue())

    def delete(self, remote_rel_path: str):
        self._reconnect()
        try:
            self._ftp.delete(remote_rel_path)
        except Exception:
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
        except Exception:
            pass
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
        import time
        from datetime import datetime
        mtime = None
        return {"path": " ".join(parts[8:]), "size": size, "mtime": mtime, "is_dir": is_dir}

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
        buf = io.BytesIO()
        self._ftp.retrbinary(f"RETR {remote_rel_path}", buf.write)
        return hashlib.sha256(buf.getvalue()).hexdigest()

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
        buf = io.BytesIO()
        self._ftp.retrbinary(f"RETR {remote_rel_path}", buf.write)
        data = buf.getvalue()
        head = data[:4096]
        tail = data[-4096:] if len(data) > 4096 else data
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

    def connect(self) -> bool:
        try:
            import paramiko
            transport = paramiko.Transport((self.host, self.port))
            if self.key_file:
                key = paramiko.RSAKey.from_private_key_file(
                    os.path.expanduser(self.key_file)
                )
                transport.connect(username=self.username, pkey=key)
            else:
                transport.connect(username=self.username, password=self.password)
            self._transport = transport
            self._sftp = paramiko.SFTPClient.from_transport(transport)
            self._sftp.chdir(self.base_path)
            self._connected = True
            return True
        except Exception:
            return False

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
        with open(local_path, "rb") as f:
            data = f.read()
        with self._sftp.open(remote_rel_path, "wb") as f:
            f.write(data)
        return hashlib.sha256(data).hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        self._reconnect()
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self._sftp.get(remote_rel_path, local_path)

    def delete(self, remote_rel_path: str):
        self._reconnect()
        try:
            self._sftp.remove(remote_rel_path)
        except Exception:
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

    def walk(self, prefix: str = "") -> list[dict]:
        return self._walk_recursive(prefix)

    def _walk_recursive(self, prefix: str = "") -> list[dict]:
        self._reconnect()
        items = []
        try:
            for attr in self._sftp.listdir_attr(prefix) if prefix else self._sftp.listdir_attr():
                rel = f"{prefix}/{attr.filename}" if prefix else attr.filename
                if stat.S_ISDIR(attr.st_mode):
                    items.extend(self._walk_recursive(rel))
                else:
                    items.append({"path": rel, "size": attr.st_size, "mtime": attr.st_mtime})
        except Exception:
            pass
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
        with self._sftp.open(remote_rel_path, "rb") as f:
            data = f.read()
        return hashlib.sha256(data).hexdigest()

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
        with self._sftp.open(remote_rel_path, "rb") as f:
            data = f.read()
        head = data[:4096]
        tail = data[-4096:] if len(data) > 4096 else data
        h = hashlib.sha256(head + tail).hexdigest()
        return {"size": attr.st_size, "mtime": attr.st_mtime, "head_tail_sha256": h}
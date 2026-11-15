import hashlib
import os
import shutil
from pathlib import Path

from .base import StorageBackend


class LocalBackend(StorageBackend):
    @property
    def type(self) -> str:
        return "local"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.base_path: str = config.get("path", "backup_storage") if config else "backup_storage"

    def connect(self) -> bool:
        try:
            Path(self.base_path).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            import logging
            logging.getLogger(__name__).warning(
                "backend %s: base path unusable: %s (%s)", self._name, self.base_path, e)
            self._connected = False
            return False
        self._connected = True
        return True

    def disconnect(self):
        self._connected = False

    @staticmethod
    def _sanitize(rel_path: str) -> str:
        parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != ".."]
        return "/".join(parts)

    def _resolve(self, rel_path: str) -> Path:
        return Path(self.base_path) / self._sanitize(rel_path).replace("/", os.sep)

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        dest = self._resolve(remote_rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256()
        with open(local_path, "rb") as src, open(str(dest), "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                sha.update(chunk)
        shutil.copystat(local_path, str(dest))
        return sha.hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(self._resolve(remote_rel_path)), local_path)

    def delete(self, remote_rel_path: str):
        path = self._resolve(remote_rel_path)
        if path.exists():
            path.unlink()

    def exists(self, remote_rel_path: str) -> bool:
        return self._resolve(remote_rel_path).exists()

    def list_files(self, prefix: str = "") -> list[dict]:
        base = Path(self.base_path)
        if prefix:
            base = base / prefix.replace("/", os.sep)
        if not base.exists():
            return []
        result = []
        for p in base.rglob("*"):
            if p.is_file():
                rel = p.relative_to(Path(self.base_path)).as_posix()
                s = p.stat()
                result.append({"path": rel, "size": s.st_size, "mtime": s.st_mtime})
        return result

    def walk(self, prefix: str = "") -> list[dict]:
        return self.list_files(prefix)

    def free_space(self) -> int | None:
        usage = shutil.disk_usage(self.base_path)
        return usage.free

    def total_space(self) -> int | None:
        usage = shutil.disk_usage(self.base_path)
        return usage.total

    def hash(self, remote_rel_path: str) -> str:
        sha = hashlib.sha256()
        with open(str(self._resolve(remote_rel_path)), "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()

    def health(self) -> bool:
        return Path(self.base_path).exists()

    def fingerprint(self, remote_rel_path: str) -> dict:
        path = self._resolve(remote_rel_path)
        s = path.stat()
        head = b""
        tail = b""
        with open(str(path), "rb") as f:
            head = f.read(4096)
            if s.st_size > 4096:
                f.seek(-4096, 2)
                tail = f.read(4096)
            else:
                tail = head
        h = hashlib.sha256(head + tail).hexdigest()
        return {"size": s.st_size, "mtime": s.st_mtime, "head_tail_sha256": h}
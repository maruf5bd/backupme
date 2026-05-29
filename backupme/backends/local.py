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
        Path(self.base_path).mkdir(parents=True, exist_ok=True)
        self._connected = True
        return True

    def disconnect(self):
        self._connected = False

    def _resolve(self, rel_path: str) -> Path:
        return Path(self.base_path) / rel_path.replace("/", os.sep)

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        dest = self._resolve(remote_rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, str(dest))
        with open(local_path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        return h

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
        data = self._resolve(remote_rel_path).read_bytes()
        return hashlib.sha256(data).hexdigest()

    def health(self) -> bool:
        return Path(self.base_path).exists()

    def fingerprint(self, remote_rel_path: str) -> dict:
        path = self._resolve(remote_rel_path)
        s = path.stat()
        data = path.read_bytes()
        head = data[:4096]
        tail = data[-4096:] if len(data) > 4096 else data
        h = hashlib.sha256(head + tail).hexdigest()
        return {"size": s.st_size, "mtime": s.st_mtime, "head_tail_sha256": h}
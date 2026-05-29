import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from .base import StorageBackend


class RcloneBackend(StorageBackend):
    @property
    def type(self) -> str:
        return "rclone"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.remote = config.get("remote", "")
        self.bwlimit = config.get("bwlimit", "")

    def _args(self) -> list[str]:
        cmd = ["rclone"]
        if self.bwlimit:
            cmd.extend(["--bwlimit", self.bwlimit])
        return cmd

    def _dest(self, rel_path: str = "") -> str:
        dest = self.remote
        if rel_path:
            dest = f"{dest}/{rel_path}"
        return dest

    def connect(self) -> bool:
        try:
            result = subprocess.run(
                self._args() + ["lsf", self._dest()],
                capture_output=True, text=True, timeout=15,
            )
            self._connected = result.returncode == 0
            return self._connected
        except Exception:
            self._connected = False
            return False

    def disconnect(self):
        self._connected = False

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        dir_part = remote_rel_path.rsplit("/", 1)[0] if "/" in remote_rel_path else ""
        dest_dir = self._dest(dir_part)
        subprocess.run(
            self._args() + ["copy", local_path, dest_dir],
            capture_output=True, timeout=120,
        )
        with open(local_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            self._args() + ["copy", self._dest(remote_rel_path), local_path],
            capture_output=True, timeout=120,
        )

    def delete(self, remote_rel_path: str):
        subprocess.run(
            self._args() + ["delete", self._dest(remote_rel_path)],
            capture_output=True, timeout=60,
        )

    def exists(self, remote_rel_path: str) -> bool:
        result = subprocess.run(
            self._args() + ["lsf", self._dest(remote_rel_path)],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0

    def list_files(self, prefix: str = "") -> list[dict]:
        target = self._dest(prefix)
        result = subprocess.run(
            self._args() + ["lsjson", "-R", target],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return []
        try:
            items = json.loads(result.stdout)
        except json.JSONDecodeError:
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

    def free_space(self) -> int | None:
        result = subprocess.run(
            self._args() + ["about", self._dest(), "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
            free = data.get("free") or data.get("free", 0)
            return int(free)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    def total_space(self) -> int | None:
        result = subprocess.run(
            self._args() + ["about", self._dest(), "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
            total = data.get("total") or data.get("total", 0)
            return int(total)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    def hash(self, remote_rel_path: str) -> str:
        result = subprocess.run(
            self._args() + ["hashsum", "SHA256", self._dest(remote_rel_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise FileNotFoundError(f"Could not hash {remote_rel_path}")
        return result.stdout.strip().split()[0]

    def health(self) -> bool:
        try:
            return self.connect()
        except Exception:
            return False

    def fingerprint(self, remote_rel_path: str) -> dict:
        target = self._dest(remote_rel_path)
        info = subprocess.run(
            self._args() + ["lsjson", target],
            capture_output=True, text=True, timeout=30,
        )
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
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                self._args() + ["copy", self._dest(remote_rel_path), tmp_path],
                capture_output=True, timeout=120,
            )
            p = Path(tmp_path)
            if p.exists():
                data = p.read_bytes()
                head = data[:4096]
                tail = data[-4096:] if len(data) > 4096 else data
                h = hashlib.sha256(head + tail).hexdigest()
                return {"size": size, "mtime": mtime, "head_tail_sha256": h}
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return {"size": size, "mtime": mtime, "head_tail_sha256": ""}
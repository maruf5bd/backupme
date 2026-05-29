import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from .base import StorageBackend


class SSHBackend(StorageBackend):
    @property
    def type(self) -> str:
        return "ssh"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.host = config.get("host", "localhost")
        self.username = config.get("username", "")
        self.key_file = config.get("key_file", "")
        self.base_path = config.get("path", "/tmp/backupme")
        self.port = int(config.get("port", 22))

    def _ssh_base_cmd(self) -> list[str]:
        cmd = ["ssh", "-p", str(self.port), "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10"]
        if self.key_file:
            cmd.extend(["-i", os.path.expanduser(self.key_file)])
        return cmd

    def _scp_base_cmd(self) -> list[str]:
        cmd = ["scp", "-P", str(self.port), "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10"]
        if self.key_file:
            cmd.extend(["-i", os.path.expanduser(self.key_file)])
        return cmd

    def _remote(self) -> str:
        return f"{self.username}@{self.host}"

    def _remote_path(self, rel_path: str = "") -> str:
        return f"{self.base_path}/{rel_path}" if rel_path else self.base_path

    def connect(self) -> bool:
        try:
            cmd = self._ssh_base_cmd() + [self._remote(), "mkdir", "-p", self.base_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            self._connected = result.returncode == 0
            return self._connected
        except Exception:
            self._connected = False
            return False

    def disconnect(self):
        self._connected = False

    def _run_ssh(self, command: str) -> subprocess.CompletedProcess:
        cmd = self._ssh_base_cmd() + [self._remote(), command]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def _ensure_remote_dir(self, rel_path: str):
        dir_part = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        if dir_part:
            self._run_ssh(f"mkdir -p {self.base_path}/{dir_part}")

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        self._ensure_remote_dir(remote_rel_path)
        with open(local_path, "rb") as f:
            data = f.read()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            dest = f"{self._remote()}:{self.base_path}/{remote_rel_path}"
            cmd = self._scp_base_cmd() + [tmp_path, dest]
            subprocess.run(cmd, capture_output=True, timeout=60)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return hashlib.sha256(data).hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        src = f"{self._remote()}:{self.base_path}/{remote_rel_path}"
        cmd = self._scp_base_cmd() + [src, local_path]
        subprocess.run(cmd, capture_output=True, timeout=60)

    def delete(self, remote_rel_path: str):
        self._run_ssh(f"rm -f {self.base_path}/{remote_rel_path}")

    def exists(self, remote_rel_path: str) -> bool:
        r = self._run_ssh(f"test -f {self.base_path}/{remote_rel_path} && echo 1 || echo 0")
        return r.stdout.strip() == "1"

    def list_files(self, prefix: str = "") -> list[dict]:
        target = f"{self.base_path}/{prefix}" if prefix else self.base_path
        files = self._run_ssh(f"find {target} -type f 2>/dev/null")
        if files.returncode != 0:
            return []
        result = []
        for line in files.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            rel = line.replace(self.base_path + "/", "").replace("\\", "/")
            info = self._run_ssh(f"stat --format='%s %Y' '{line}' 2>/dev/null")
            parts = info.stdout.strip().split()
            size = int(parts[0]) if parts else 0
            mtime = float(parts[1]) if len(parts) > 1 else 0
            result.append({"path": rel, "size": size, "mtime": mtime})
        return result

    def walk(self, prefix: str = "") -> list[dict]:
        return self.list_files(prefix)

    def free_space(self) -> int | None:
        r = self._run_ssh(f"df --output=avail {self.base_path} 2>/dev/null | tail -1")
        if r.returncode == 0:
            try:
                return int(r.stdout.strip()) * 1024
            except (ValueError, IndexError):
                pass
        return None

    def total_space(self) -> int | None:
        r = self._run_ssh(f"df --output=size {self.base_path} 2>/dev/null | tail -1")
        if r.returncode == 0:
            try:
                return int(r.stdout.strip()) * 1024
            except (ValueError, IndexError):
                pass
        return None

    def hash(self, remote_rel_path: str) -> str:
        r = self._run_ssh(f"sha256sum {self.base_path}/{remote_rel_path} 2>/dev/null | cut -d' ' -f1")
        if r.returncode == 0:
            return r.stdout.strip()
        raise FileNotFoundError(f"Could not hash {remote_rel_path}")

    def health(self) -> bool:
        try:
            r = self._run_ssh("echo 1")
            return r.returncode == 0
        except Exception:
            return False

    def fingerprint(self, remote_rel_path: str) -> dict:
        path = f"{self.base_path}/{remote_rel_path}"
        size_r = self._run_ssh(f"stat --format='%s %Y' {path} 2>/dev/null")
        size = 0
        mtime = 0
        parts = size_r.stdout.strip().split()
        if len(parts) >= 2:
            try:
                size = int(parts[0])
                mtime = float(parts[1])
            except ValueError:
                pass
        head_tail = self._run_ssh(
            f"dd if={path} bs=4096 count=1 2>/dev/null; dd if={path} bs=4096 "
            f"skip=$(( ($(stat --format='%s' {path}) - 4096) / 4096 )) 2>/dev/null"
        )
        h = hashlib.sha256(head_tail.stdout.encode()).hexdigest() if head_tail.returncode == 0 else ""
        return {"size": size, "mtime": mtime, "head_tail_sha256": h}
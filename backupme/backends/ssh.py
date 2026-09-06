import hashlib
import os
import shlex
import subprocess
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

    def _quote_remote(self, rel_path: str = "") -> str:
        return shlex.quote(self._remote_path(rel_path))

    def connect(self) -> bool:
        try:
            cmd = self._ssh_base_cmd() + [self._remote(), "mkdir", "-p", self.base_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            self._connected = result.returncode == 0
            if self._connected:
                self._deploy_agent()
            return self._connected
        except Exception:
            self._connected = False
            return False

    def disconnect(self):
        self._connected = False

    def _deploy_agent(self):
        self.use_agent = False
        try:
            check_py = self._run_ssh("python3 -c 'import json, sys; print(sys.version_info[0])'")
            if check_py.returncode == 0 and check_py.stdout.strip() == "3":
                local_agent_path = os.path.join(os.path.dirname(__file__), "agent.py")
                dest = f"{self._remote()}:{self.base_path}/.backupme_agent.py"
                cmd = self._scp_base_cmd() + [local_agent_path, dest]
                res = subprocess.run(cmd, capture_output=True, timeout=15)
                if res.returncode == 0:
                    self.use_agent = True
        except Exception:
            self.use_agent = False

    def _run_ssh(self, command: str, stdin_data: str | None = None) -> subprocess.CompletedProcess:
        cmd = self._ssh_base_cmd() + [self._remote(), command]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              input=stdin_data)

    def _ensure_remote_dir(self, rel_path: str):
        dir_part = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        if dir_part:
            self._run_ssh(f"mkdir -p {shlex.quote(self.base_path + '/' + dir_part)}")

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        self._ensure_remote_dir(remote_rel_path)
        sha = hashlib.sha256()
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
        dest = f"{self._remote()}:{self._remote_path(remote_rel_path)}"
        cmd = self._scp_base_cmd() + [local_path, dest]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise IOError(f"SCP upload failed: {result.stderr.strip()}")
        return sha.hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        src = f"{self._remote()}:{self._remote_path(remote_rel_path)}"
        cmd = self._scp_base_cmd() + [src, local_path]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise IOError(f"SCP download failed: {result.stderr.strip()}")

    def delete(self, remote_rel_path: str):
        self._run_ssh(f"rm -f {self._quote_remote(remote_rel_path)}")

    def exists(self, remote_rel_path: str) -> bool:
        if getattr(self, "use_agent", False):
            path = self._remote_path(remote_rel_path)
            r = self._run_ssh(f"python3 {shlex.quote(self.base_path + '/.backupme_agent.py')} exists {shlex.quote(path)}")
            if r.returncode == 0:
                try:
                    import json
                    return json.loads(r.stdout.strip()).get("exists", False)
                except Exception:
                    pass
        r = self._run_ssh(f"test -f {self._quote_remote(remote_rel_path)} && echo 1 || echo 0")
        return r.stdout.strip() == "1"

    def list_files(self, prefix: str = "") -> list[dict]:
        if getattr(self, "use_agent", False):
            target = self._remote_path(prefix) if prefix else self.base_path
            r = self._run_ssh(f"python3 {shlex.quote(self.base_path + '/.backupme_agent.py')} list {shlex.quote(target)}")
            if r.returncode == 0:
                try:
                    import json
                    return json.loads(r.stdout.strip())
                except Exception:
                    pass
        target = self._quote_remote(prefix) if prefix else shlex.quote(self.base_path)
        files = self._run_ssh(f"find {target} -type f -print0 2>/dev/null")
        if files.returncode != 0:
            return []
        paths = [p for p in files.stdout.split("\x00") if p.strip()]
        if not paths:
            return []
        stat_input = "\0".join(paths)
        batch = self._run_ssh(f"xargs -0 stat --format='%n %s %Y' 2>/dev/null", stdin_data=stat_input)
        result = []
        for line in batch.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 3:
                full_path = parts[0]
                try:
                    size = int(parts[-2])
                    mtime = float(parts[-1])
                except ValueError:
                    continue
                rel = full_path.replace(self.base_path + "/", "").replace("\\", "/")
                result.append({"path": rel, "size": size, "mtime": mtime})
        return result

    def walk(self, prefix: str = "") -> list[dict]:
        return self.list_files(prefix)

    def free_space(self) -> int | None:
        if getattr(self, "use_agent", False):
            r = self._run_ssh(f"python3 {shlex.quote(self.base_path + '/.backupme_agent.py')} free {shlex.quote(self.base_path)}")
            if r.returncode == 0:
                try:
                    import json
                    return json.loads(r.stdout.strip()).get("free")
                except Exception:
                    pass
        r = self._run_ssh(f"df --output=avail {self.base_path} 2>/dev/null | tail -1")
        if r.returncode == 0:
            try:
                return int(r.stdout.strip()) * 1024
            except (ValueError, IndexError):
                pass
        return None

    def total_space(self) -> int | None:
        if getattr(self, "use_agent", False):
            r = self._run_ssh(f"python3 {shlex.quote(self.base_path + '/.backupme_agent.py')} free {shlex.quote(self.base_path)}")
            if r.returncode == 0:
                try:
                    import json
                    return json.loads(r.stdout.strip()).get("total")
                except Exception:
                    pass
        r = self._run_ssh(f"df --output=size {self.base_path} 2>/dev/null | tail -1")
        if r.returncode == 0:
            try:
                return int(r.stdout.strip()) * 1024
            except (ValueError, IndexError):
                pass
        return None

    def hash(self, remote_rel_path: str) -> str:
        if getattr(self, "use_agent", False):
            path = self._remote_path(remote_rel_path)
            r = self._run_ssh(f"python3 {shlex.quote(self.base_path + '/.backupme_agent.py')} hash {shlex.quote(path)}")
            if r.returncode == 0:
                try:
                    import json
                    res = json.loads(r.stdout.strip())
                    if "hash" in res:
                        return res["hash"]
                except Exception:
                    pass
        r = self._run_ssh(f"sha256sum {self._quote_remote(remote_rel_path)} 2>/dev/null | cut -d' ' -f1")
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
        if getattr(self, "use_agent", False):
            path = self._remote_path(remote_rel_path)
            r = self._run_ssh(f"python3 {shlex.quote(self.base_path + '/.backupme_agent.py')} fingerprint {shlex.quote(path)}")
            if r.returncode == 0:
                try:
                    import json
                    return json.loads(r.stdout.strip())
                except Exception:
                    pass
        path = self._quote_remote(remote_rel_path)
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
            f"(dd if={path} bs=4096 count=1 2>/dev/null; "
            f"dd if={path} bs=4096 skip=$(( ($(stat --format='%s' {path}) - 4096) / 4096 )) 2>/dev/null) | sha256sum"
        )
        h = head_tail.stdout.strip().split()[0] if head_tail.returncode == 0 else ""
        return {"size": size, "mtime": mtime, "head_tail_sha256": h}
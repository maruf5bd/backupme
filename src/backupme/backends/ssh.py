import hashlib
import os
import shlex
import subprocess
import time
from pathlib import Path

from .base import StorageBackend
from backupme.log import get_logger

log = get_logger("backends.ssh")


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
            else:
                log.warning("ssh connect to %s failed rc=%s: %s",
                            self._remote(), result.returncode, result.stderr.strip()[:200])
            return self._connected
        except Exception as e:
            log.warning("ssh connect to %s failed: %s", self._remote(), e)
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
                else:
                    log.warning("ssh: agent deployment to %s failed rc=%s; using plain ssh probes",
                                self._remote(), res.returncode)
        except Exception as e:
            log.warning("ssh: agent deployment to %s failed: %s", self._remote(), e)
            self.use_agent = False

    def _run_ssh_retry(self, command: str, stdin_data: str | None = None,
                       attempts: int = 3) -> subprocess.CompletedProcess:
        """_run_ssh with bounded backoff: a transient SSH/network hiccup
        mid-walk must retry before the listing is counted as incomplete."""
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                result = self._run_ssh(command, stdin_data=stdin_data)
            except Exception as e:  # timeout / connection failure
                last_exc = e
                time.sleep(2 * (attempt + 1))
                continue
            if result.returncode == 0:
                return result
            last_exc = RuntimeError(
                f"exit {result.returncode}: {result.stderr.strip()[:200]}"
            )
            time.sleep(2 * (attempt + 1))
        raise last_exc if last_exc else RuntimeError("ssh command failed")

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
                except Exception as e:
                    log.warning("ssh exists: agent output unparseable for %s: %s", path, e)
            else:
                log.warning("ssh exists: agent probe failed rc=%s for %s, falling back to test -f",
                            r.returncode, path)
        r = self._run_ssh(f"test -f {self._quote_remote(remote_rel_path)} && echo 1 || echo 0")
        return r.stdout.strip() == "1"

    def list_files(self, prefix: str = "") -> list[dict]:
        self.walk_errors = 0
        return self._list_files_inner(prefix)

    def walk(self, prefix: str = "") -> list[dict]:
        self.walk_errors = 0
        return self._list_files_inner(prefix)

    def _list_files_inner(self, prefix: str = "") -> list[dict]:
        if getattr(self, "use_agent", False):
            target = self._remote_path(prefix) if prefix else self.base_path
            r = self._run_ssh(f"python3 {shlex.quote(self.base_path + '/.backupme_agent.py')} list {shlex.quote(target)}")
            if r.returncode == 0:
                try:
                    import json
                    parsed = json.loads(r.stdout.strip())
                    if isinstance(parsed, list):
                        return parsed
                    # An agent error payload ({"error": ...}) is not a listing;
                    # never hand it back as if it were one.
                    log.warning("ssh list: agent returned malformed payload for %s, falling back to find", target)
                    self.walk_errors += 1
                except Exception as e:
                    log.warning("ssh list: agent output unparseable for %s: %s", target, e)
                    self.walk_errors += 1
            else:
                log.warning("ssh list: agent probe failed rc=%s for %s, falling back to find: %s",
                            r.returncode, target, r.stderr.strip()[:200])
        target = self._quote_remote(prefix) if prefix else shlex.quote(self.base_path)
        try:
            files = self._run_ssh_retry(f"find {target} -type f -print0 2>/dev/null")
            paths = [p for p in files.stdout.split("\x00") if p.strip()]
        except Exception as e:
            # Never silently truncate: record the gap so callers can detect
            # an incomplete listing (walk_errors > 0 means partial data).
            self.walk_errors += 1
            log.warning("ssh walk: cannot list %s after retries: %s", prefix or self.base_path, e)
            return []
        if not paths:
            return []
        stat_input = "\0".join(paths)
        # Best-effort batch: files may vanish between find and stat, so a
        # nonzero exit is not fatal — the gap check below is what matters.
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
                    # Logged here; the gap check below counts it exactly once.
                    log.warning("ssh walk: skipping unparseable stat line for %s", full_path)
                    continue
                rel = full_path.replace(self.base_path + "/", "").replace("\\", "/")
                result.append({"path": rel, "size": size, "mtime": mtime})
        if len(result) < len(paths):
            # Entries the stat batch failed to report (vanished between find
            # and stat, parse skips, ...): count the gap, never truncate silently.
            missing = len(paths) - len(result)
            self.walk_errors += missing
            log.warning("ssh walk: %d of %d entries could not be stat'd under %s",
                        missing, len(paths), prefix or self.base_path)
        return result

    def free_space(self) -> int | None:
        if getattr(self, "use_agent", False):
            r = self._run_ssh(f"python3 {shlex.quote(self.base_path + '/.backupme_agent.py')} free {shlex.quote(self.base_path)}")
            if r.returncode == 0:
                try:
                    import json
                    return json.loads(r.stdout.strip()).get("free")
                except Exception as e:
                    log.warning("ssh free_space: agent output unparseable: %s", e)
            else:
                log.warning("ssh free_space: agent probe failed rc=%s, falling back to df", r.returncode)
        r = self._run_ssh(f"df --output=avail {self.base_path} 2>/dev/null | tail -1")
        if r.returncode == 0:
            try:
                return int(r.stdout.strip()) * 1024
            except (ValueError, IndexError):
                log.warning("ssh free_space: unparseable df output: %r", r.stdout.strip()[:100])
        else:
            log.warning("ssh free_space: df probe failed rc=%s", r.returncode)
        return None

    def total_space(self) -> int | None:
        if getattr(self, "use_agent", False):
            r = self._run_ssh(f"python3 {shlex.quote(self.base_path + '/.backupme_agent.py')} free {shlex.quote(self.base_path)}")
            if r.returncode == 0:
                try:
                    import json
                    return json.loads(r.stdout.strip()).get("total")
                except Exception as e:
                    log.warning("ssh total_space: agent output unparseable: %s", e)
            else:
                log.warning("ssh total_space: agent probe failed rc=%s, falling back to df", r.returncode)
        r = self._run_ssh(f"df --output=size {self.base_path} 2>/dev/null | tail -1")
        if r.returncode == 0:
            try:
                return int(r.stdout.strip()) * 1024
            except (ValueError, IndexError):
                log.warning("ssh total_space: unparseable df output: %r", r.stdout.strip()[:100])
        else:
            log.warning("ssh total_space: df probe failed rc=%s", r.returncode)
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
                    log.warning("ssh hash: agent payload missing hash for %s, falling back to sha256sum", path)
                except Exception as e:
                    log.warning("ssh hash: agent output unparseable for %s: %s", path, e)
            else:
                log.warning("ssh hash: agent probe failed rc=%s for %s, falling back to sha256sum",
                            r.returncode, path)
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
                    res = json.loads(r.stdout.strip())
                    if "head_tail_sha256" in res:
                        return res
                    log.warning("ssh fingerprint: agent payload incomplete for %s, falling back to dd", path)
                except Exception as e:
                    log.warning("ssh fingerprint: agent output unparseable for %s: %s", path, e)
            else:
                log.warning("ssh fingerprint: agent probe failed rc=%s for %s, falling back to dd",
                            r.returncode, path)
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
                log.warning("ssh fingerprint: unparseable stat output for %s: %r", path, size_r.stdout.strip()[:100])
        else:
            log.warning("ssh fingerprint: stat probe failed for %s rc=%s", path, size_r.returncode)
        head_tail = self._run_ssh(
            f"(dd if={path} bs=4096 count=1 2>/dev/null; "
            f"dd if={path} bs=4096 skip=$(( ($(stat --format='%s' {path}) - 4096) / 4096 )) 2>/dev/null) | sha256sum"
        )
        h = head_tail.stdout.strip().split()[0] if head_tail.returncode == 0 else ""
        if not h:
            log.warning("ssh fingerprint: head+tail digest unavailable for %s", path)
        return {"size": size, "mtime": mtime, "head_tail_sha256": h}
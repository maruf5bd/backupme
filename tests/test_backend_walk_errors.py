"""B01 regression tests: a backend listing must never come back silently
truncated. Every walk()/list_files() resets ``walk_errors``, retries transient
list failures with a small backoff, and logs every skipped entry; ssh/agent
probes must make failures distinguishable from clean negative results.

All fakes are in-memory: no network, no real smbclient/ssh/9drive calls.
"""
import json
import logging
import stat as stat_module
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from backupme.backends.smb import SMBBackend
from backupme.backends.ssh import SSHBackend
from backupme.backends.ninedrive import NineDriveBackend, NineDriveError
from backupme.backends import agent as agent_mod


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Backoff sleeps in the retry helpers must not slow the tests down."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _capture_backend_warnings(caplog):
    caplog.set_level(logging.WARNING)


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# smb: fake smbclient module (listdir / stat / register_session)
# ---------------------------------------------------------------------------


class FakeSmbError(Exception):
    pass


class FakeSmbModule:
    """In-memory smbclient: ``tree`` maps absolute path -> entry tuple."""

    def __init__(self, tree):
        self.tree = tree
        # path -> remaining listdir failures; -1 means fail forever
        self.fail_paths = {}
        self.stat_fail_paths = set()
        self.listdir_calls = []

    @staticmethod
    def _norm(path):
        return path.replace("\\", "/").rstrip("/")

    def _children(self, path):
        prefix = path + "/"
        names = []
        for full in self.tree:
            if full.startswith(prefix) and "/" not in full[len(prefix):]:
                names.append(full[len(prefix):])
        return sorted(names)

    def register_session(self, *args, **kwargs):
        pass

    def listdir(self, path):
        path = self._norm(path)
        self.listdir_calls.append(path)
        remaining = self.fail_paths.get(path)
        if remaining is not None:
            if remaining == 0:
                del self.fail_paths[path]  # transient failure recovered
            else:
                if remaining > 0:
                    self.fail_paths[path] = remaining - 1
                raise FakeSmbError(f"simulated listdir failure for {path}")
        return self._children(path)

    def stat(self, path):
        path = self._norm(path)
        if path in self.stat_fail_paths:
            raise FakeSmbError(f"simulated stat failure for {path}")
        entry = self.tree.get(path)
        if entry is None:
            raise FileNotFoundError(path)
        if entry[0] == "dir":
            return SimpleNamespace(st_mode=stat_module.S_IFDIR | 0o755,
                                   st_size=0, st_mtime=0)
        _, size, mtime = entry
        return SimpleNamespace(st_mode=stat_module.S_IFREG | 0o644,
                               st_size=size, st_mtime=mtime)


@pytest.fixture()
def smb_backend(monkeypatch):
    root = "//h/sh/bp"
    tree = {
        f"{root}/a.txt": ("file", 11, 1.0),
        f"{root}/ok": ("dir",),
        f"{root}/ok/f1.txt": ("file", 22, 2.0),
        f"{root}/ok/f3.txt": ("file", 44, 4.0),
        f"{root}/bad": ("dir",),
        f"{root}/bad/f2.txt": ("file", 33, 3.0),
    }
    fake = FakeSmbModule(tree)
    monkeypatch.setitem(sys.modules, "smbclient", fake)
    bk = SMBBackend("smbtest", {"host": "h", "share": "sh", "path": "bp"})
    return bk, fake, root


def test_smb_walk_retries_transient_listdir_failure(smb_backend):
    bk, fake, root = smb_backend
    fake.fail_paths[f"{root}/ok"] = 1  # fails once, then recovers

    items = bk.walk()

    assert {f["path"] for f in items} == {"a.txt", "ok/f1.txt", "ok/f3.txt", "bad/f2.txt"}
    assert bk.walk_errors == 0
    # two listdir attempts for ok/ prove the retry happened
    assert fake.listdir_calls.count(f"{root}/ok") == 2


def test_smb_walk_permanent_listdir_failure_is_counted_and_logged(smb_backend, caplog):
    bk, fake, root = smb_backend
    fake.fail_paths[root] = -1

    items = bk.walk()

    assert items == []
    assert bk.walk_errors == 1
    assert any("after retries" in r.getMessage() for r in _warnings(caplog))


def test_smb_walk_partial_failure_keeps_successful_part(smb_backend, caplog):
    bk, fake, root = smb_backend
    fake.fail_paths[f"{root}/bad"] = -1

    items = bk.walk()

    paths = {f["path"] for f in items}
    assert "a.txt" in paths
    assert "ok/f1.txt" in paths
    assert "bad/f2.txt" not in paths  # the failed subtree is gone
    assert bk.walk_errors >= 1
    assert any("bad" in r.getMessage() for r in _warnings(caplog))


def test_smb_walk_stat_error_skips_entry_and_counts(smb_backend, caplog):
    bk, fake, root = smb_backend
    fake.stat_fail_paths.add(f"{root}/a.txt")

    items = bk.walk()

    paths = {f["path"] for f in items}
    assert "a.txt" not in paths
    assert {"ok/f1.txt", "bad/f2.txt"} <= paths
    assert bk.walk_errors >= 1
    assert any("a.txt" in r.getMessage() for r in _warnings(caplog))


def test_smb_walk_clean_tree_has_zero_errors_and_no_warnings(smb_backend, caplog):
    bk, fake, root = smb_backend

    items = bk.walk()

    assert len(items) == 4
    assert bk.walk_errors == 0
    assert _warnings(caplog) == []


def test_smb_list_files_counts_errors_too(smb_backend, caplog):
    bk, fake, root = smb_backend
    fake.stat_fail_paths.add(f"{root}/ok/f1.txt")

    # list_files is flat (no recursion): pointing it at ok/ skips the dirs
    items = bk.list_files("ok")

    assert {f["path"] for f in items} == {"ok/f3.txt"}
    assert bk.walk_errors >= 1
    assert any("f1.txt" in r.getMessage() for r in _warnings(caplog))


def test_smb_walk_errors_reset_between_walks(smb_backend):
    bk, fake, root = smb_backend
    fake.fail_paths[root] = -1
    bk.walk()
    assert bk.walk_errors >= 1

    del fake.fail_paths[root]
    bk.walk()

    assert bk.walk_errors == 0


# ---------------------------------------------------------------------------
# ninedrive: fake _refresh_index (the /files listing primitive)
# ---------------------------------------------------------------------------


def make_ninedrive_backend():
    return NineDriveBackend("nd", {"base_url": "http://9drive.test", "api_key": "k"})


class FakeRefreshIndex:
    """Stateful stand-in for _refresh_index (the /files listing primitive)."""

    def __init__(self, bk, fail_forever=False, fail_first=0):
        self.bk = bk
        self.fail_forever = fail_forever
        self.fail_first = fail_first
        self.calls = 0

    def __call__(self, force=False):
        self.calls += 1
        if self.fail_forever or self.calls <= self.fail_first:
            raise NineDriveError("simulated 9drive outage")
        self.bk._index = {
            "objects/aa/h1": {
                "name": "objects/aa/h1", "sizeBytes": "10",
                "status": "active", "createdAt": "2026-01-01T00:00:00Z",
            },
        }
        self.bk._index_ts = time.monotonic()


def test_ninedrive_walk_retries_transient_index_failure(monkeypatch):
    bk = make_ninedrive_backend()
    fake = FakeRefreshIndex(bk, fail_first=1)  # fails once, then recovers
    monkeypatch.setattr(bk, "_refresh_index", fake)

    items = bk.walk()

    assert [f["path"] for f in items] == ["objects/aa/h1"]
    assert items[0]["size"] == 10
    assert bk.walk_errors == 0
    assert fake.calls == 2  # retried once before succeeding


def test_ninedrive_walk_permanent_index_failure_is_counted_and_logged(monkeypatch, caplog):
    bk = make_ninedrive_backend()
    monkeypatch.setattr(bk, "_refresh_index", FakeRefreshIndex(bk, fail_forever=True))

    items = bk.walk()

    assert items == []
    assert bk.walk_errors == 1
    assert any("truncated" in r.getMessage() for r in _warnings(caplog))


def test_ninedrive_walk_errors_reset_between_walks(monkeypatch):
    bk = make_ninedrive_backend()
    fake = FakeRefreshIndex(bk, fail_forever=True)
    monkeypatch.setattr(bk, "_refresh_index", fake)
    bk.walk()
    assert bk.walk_errors == 1

    fake.fail_forever = False
    bk._index = None
    bk.walk()

    assert bk.walk_errors == 0


def test_ninedrive_walk_clean_index_has_zero_errors(monkeypatch, caplog):
    bk = make_ninedrive_backend()
    monkeypatch.setattr(bk, "_refresh_index", FakeRefreshIndex(bk))

    items = bk.walk()

    assert len(items) == 1
    assert bk.walk_errors == 0
    assert _warnings(caplog) == []


# ---------------------------------------------------------------------------
# ssh: fake _run_ssh (find / stat batch / agent probes)
# ---------------------------------------------------------------------------


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class FakeSshRunner:
    """Routes ssh commands to canned answers the way a real server would."""

    def __init__(self):
        self.agent_answers = {}   # probe name -> (rc, stdout)
        self.find_failures_left = 0
        self.find_paths = []
        self.stat_drop = set()    # full paths the stat batch "loses"
        self.df_answer = _cp(stdout="  456\n")
        self.find_calls = 0

    def __call__(self, command, stdin_data=None):
        if ".backupme_agent.py" in command:
            for probe, (rc, out) in self.agent_answers.items():
                if f" {probe} " in command:
                    return _cp(returncode=rc, stdout=out)
            return _cp(returncode=0, stdout="")
        if "find " in command:
            self.find_calls += 1
            if self.find_failures_left > 0:
                self.find_failures_left -= 1
                return _cp(returncode=1, stderr="simulated ssh failure")
            out = "\0".join(self.find_paths) + "\0" if self.find_paths else ""
            return _cp(stdout=out)
        if "xargs" in command:
            lines = []
            for path in (stdin_data or "").split("\0"):
                if path.strip() and path not in self.stat_drop:
                    lines.append(f"{path} 123 1700000000")
            return _cp(stdout="\n".join(lines))
        if "test -f" in command:
            return _cp(stdout="0\n")
        if "df --output" in command:
            return self.df_answer
        return _cp()


@pytest.fixture()
def ssh_backend(monkeypatch):
    bk = SSHBackend("ssh-test", {"host": "h", "username": "u", "path": "/remote/bk"})
    bk.use_agent = False
    bk._connected = True
    runner = FakeSshRunner()
    monkeypatch.setattr(bk, "_run_ssh", runner)
    return bk, runner


def test_ssh_walk_retries_transient_find_failure(ssh_backend):
    bk, runner = ssh_backend
    runner.find_paths = ["/remote/bk/objects/aa/h1", "/remote/bk/objects/ab/h2"]
    runner.find_failures_left = 1

    items = bk.walk()

    assert [f["path"] for f in items] == ["objects/aa/h1", "objects/ab/h2"]
    assert bk.walk_errors == 0
    assert runner.find_calls == 2  # retried once before succeeding


def test_ssh_walk_permanent_find_failure_is_counted_and_logged(ssh_backend, caplog):
    bk, runner = ssh_backend
    runner.find_failures_left = 99

    items = bk.walk()

    assert items == []
    assert bk.walk_errors == 1
    assert any("cannot list" in r.getMessage() for r in _warnings(caplog))


def test_ssh_walk_stat_gap_is_counted_and_logged(ssh_backend, caplog):
    bk, runner = ssh_backend
    runner.find_paths = ["/remote/bk/objects/aa/h1", "/remote/bk/objects/ab/h2"]
    runner.stat_drop = {"/remote/bk/objects/ab/h2"}

    items = bk.walk()

    assert [f["path"] for f in items] == ["objects/aa/h1"]
    assert bk.walk_errors == 1
    assert any("could not be stat'd" in r.getMessage() for r in _warnings(caplog))


def test_ssh_walk_errors_reset_between_walks(ssh_backend):
    bk, runner = ssh_backend
    runner.find_failures_left = 99
    bk.walk()
    assert bk.walk_errors == 1

    runner.find_failures_left = 0
    runner.find_paths = ["/remote/bk/objects/aa/h1"]
    bk.walk()

    assert bk.walk_errors == 0


def test_ssh_exists_clean_negative_is_not_an_error(ssh_backend, caplog):
    bk, runner = ssh_backend

    assert bk.exists("objects/aa/h1") is False
    assert _warnings(caplog) == []


def test_ssh_exists_agent_garbage_is_distinguishable_from_clean_negative(ssh_backend, caplog):
    bk, runner = ssh_backend
    bk.use_agent = True
    runner.agent_answers["exists"] = (0, "not-json at all")

    assert bk.exists("objects/aa/h1") is False  # still a bool, via fallback
    assert any("unparseable" in r.getMessage() for r in _warnings(caplog))


def test_ssh_list_agent_error_payload_falls_back_to_find(ssh_backend, caplog):
    bk, runner = ssh_backend
    bk.use_agent = True
    runner.agent_answers["list"] = (0, json.dumps({"error": "simulated agent crash"}))
    runner.find_paths = ["/remote/bk/objects/aa/h1"]

    items = bk.walk()

    # the error object must never come back as if it were a listing
    assert [f["path"] for f in items] == ["objects/aa/h1"]
    assert bk.walk_errors == 1
    assert any("malformed payload" in r.getMessage() for r in _warnings(caplog))


def test_ssh_free_space_failure_is_none_and_logged(ssh_backend, caplog):
    bk, runner = ssh_backend
    runner.df_answer = _cp(returncode=1, stderr="df: simulated failure")

    assert bk.free_space() is None
    assert any("df probe failed" in r.getMessage() for r in _warnings(caplog))


def test_ssh_free_space_success_returns_value(ssh_backend):
    bk, runner = ssh_backend

    assert bk.free_space() == 456 * 1024


# ---------------------------------------------------------------------------
# agent: the standalone remote helper (stderr warnings, exit codes)
# ---------------------------------------------------------------------------


def test_agent_list_skips_unstatable_file_with_stderr_warning(tmp_path, monkeypatch, capsys):
    base = tmp_path / "bk"
    base.mkdir()
    (base / "good.txt").write_bytes(b"fine")
    victim = base / "victim.txt"
    victim.write_bytes(b"doomed")

    real_stat = agent_mod.os.stat

    def fake_stat(path, *args, **kwargs):
        if str(path) == str(victim):
            raise OSError("simulated stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(agent_mod.os, "stat", fake_stat)

    result = agent_mod.list_files(str(base))

    assert [f["path"] for f in result] == ["good.txt"]
    err = capsys.readouterr().err
    assert "backupme-agent" in err
    assert "victim.txt" in err


def test_agent_list_missing_root_warns_to_stderr(tmp_path, capsys):
    result = agent_mod.list_files(str(tmp_path / "missing"))

    assert result == []
    err = capsys.readouterr().err
    assert "backupme-agent" in err
    assert "missing" in err


def test_agent_fingerprint_tail_failure_reports_unusable_hash(tmp_path, monkeypatch, capsys):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 10000)
    real_open = open

    class BrokenTailFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            return self._fh.read(n)

        def seek(self, *args, **kwargs):
            raise OSError("simulated seek failure")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._fh.close()
            return False

    def fake_open(file, mode="r", *args, **kwargs):
        fh = real_open(file, mode, *args, **kwargs)
        if str(file) == str(big) and "rb" in mode:
            return BrokenTailFile(fh)
        return fh

    monkeypatch.setattr(agent_mod, "open", fake_open, raising=False)

    fp = agent_mod.get_fingerprint(str(big))

    # "" is the established "unavailable" sentinel; never a head-only hash
    assert fp["head_tail_sha256"] == ""
    assert fp["size"] == 10000
    err = capsys.readouterr().err
    assert "backupme-agent" in err
    assert "big.bin" in err


def test_agent_fingerprint_healthy_file_hash_matches(tmp_path, monkeypatch):
    import hashlib
    small = tmp_path / "small.bin"
    data = b"y" * 5000
    small.write_bytes(data)

    fp = agent_mod.get_fingerprint(str(small))

    assert fp["head_tail_sha256"] == hashlib.sha256(data[:4096] + data[-4096:]).hexdigest()


def test_agent_main_unknown_command_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agent.py", "bogus", "/x"])

    with pytest.raises(SystemExit) as excinfo:
        agent_mod.main()

    assert excinfo.value.code == 1
    assert json.loads(capsys.readouterr().out.strip())["error"]


def test_agent_main_hash_missing_file_is_error_not_negative(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["agent.py", "hash", str(tmp_path / "nope.bin")])

    with pytest.raises(SystemExit) as excinfo:
        agent_mod.main()

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" in payload


def test_agent_main_exists_missing_file_is_clean_negative(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["agent.py", "exists", str(tmp_path / "nope.bin")])

    agent_mod.main()  # no SystemExit

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"exists": False}

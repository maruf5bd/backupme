"""RcloneBackend tests: rclone treats any plain local path as a valid remote,
so these run against a temp directory "remote" — no server needed.

They pin the hardening that came out of the 10-hour real-world SFTP
operation: per-op timeout budget (timeout_s, default 6 h), --config on every
call, extra_args passthrough, byte-identical roundtrips, and LOUD (not
silent) empty listings on failure.
"""
import hashlib
import logging
import subprocess

import pytest

from backupme.backends import rclone as rclone_mod
from backupme.backends.rclone import RcloneBackend
from backupme.config import _build_backend, load_config

LOGGER_NAME = "backupme.rclone_backend"


def make_backend(remote, **extra):
    cfg = {"remote": str(remote)}
    cfg.update(extra)
    return RcloneBackend("rclone_test", cfg)


@pytest.fixture()
def remote_dir(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    return root


def _flag_pair(cmd, flag, value=None):
    """True if cmd contains flag (with following value when given)."""
    for i, part in enumerate(cmd):
        if part == flag and (value is None or (i + 1 < len(cmd) and cmd[i + 1] == value)):
            return True
    return False


class RunRecorder:
    """Records every rclone invocation, then delegates to the real subprocess.run."""

    def __init__(self, real_run):
        self._real_run = real_run
        self.calls = []

    def __call__(self, cmd, capture_output=False, text=False, errors=None,
                 timeout=None, **kwargs):
        self.calls.append({"cmd": list(cmd), "timeout": timeout})
        return self._real_run(cmd, capture_output=capture_output, text=text,
                              errors=errors, timeout=timeout, **kwargs)


@pytest.fixture()
def recorder(monkeypatch):
    rec = RunRecorder(subprocess.run)
    monkeypatch.setattr(rclone_mod.subprocess, "run", rec)
    return rec


# ---------------------------------------------------------------------------
# Core storage ops against a plain local-dir remote
# ---------------------------------------------------------------------------


def test_upload_download_roundtrip_byte_identical(remote_dir, tmp_path):
    bk = make_backend(remote_dir)
    data = bytes(range(256)) * 41 + "unicode-ütf".encode("utf-8")
    src = tmp_path / "src.bin"
    src.write_bytes(data)

    bk.upload(str(src), "objects/aa/roundtrip.bin")
    out = tmp_path / "out.bin"
    bk.download("objects/aa/roundtrip.bin", str(out))

    assert out.read_bytes() == data
    assert (remote_dir / "objects" / "aa" / "roundtrip.bin").read_bytes() == data


def test_upload_returns_local_file_sha(remote_dir, tmp_path):
    bk = make_backend(remote_dir)
    data = b"payload for sha check"
    src = tmp_path / "f.bin"
    src.write_bytes(data)

    sha = bk.upload(str(src), "f.bin")

    assert sha == hashlib.sha256(data).hexdigest()


def test_hash_matches_sha256_of_file(remote_dir, tmp_path):
    bk = make_backend(remote_dir)
    data = b"hash me please" * 100
    src = tmp_path / "h.bin"
    src.write_bytes(data)

    sha = bk.upload(str(src), "h.bin")

    assert bk.hash("h.bin") == sha == hashlib.sha256(data).hexdigest()


def test_hash_missing_file_raises_with_stderr_tail(remote_dir):
    bk = make_backend(remote_dir)

    with pytest.raises(FileNotFoundError) as ei:
        bk.hash("nope.bin")

    assert "Could not hash nope.bin" in str(ei.value)
    # the rclone stderr tail must be carried into the exception
    assert str(ei.value) != "Could not hash nope.bin:"
    assert "error" in str(ei.value).lower() or "not found" in str(ei.value).lower()


def test_exists_true_and_false(remote_dir, tmp_path):
    bk = make_backend(remote_dir)
    src = tmp_path / "e.bin"
    src.write_bytes(b"x")

    assert bk.exists("e.bin") is False
    bk.upload(str(src), "e.bin")
    assert bk.exists("e.bin") is True
    assert bk.exists("missing/e.bin") is False


def test_exists_returns_false_on_exception(monkeypatch, tmp_path):
    def boom(cmd, capture_output=False, text=False, errors=None, timeout=None, **kw):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(rclone_mod.subprocess, "run", boom)
    bk = make_backend(tmp_path)

    assert bk.exists("whatever.bin") is False


def test_list_files_finds_nested_files_with_relative_paths(remote_dir):
    bk = make_backend(remote_dir)
    (remote_dir / "a.txt").write_bytes(b"A")
    (remote_dir / "sub").mkdir()
    (remote_dir / "sub" / "b.txt").write_bytes(b"BB")
    (remote_dir / "sub" / "deep").mkdir()
    (remote_dir / "sub" / "deep" / "c.bin").write_bytes(b"CCC")

    items = bk.list_files()
    by_path = {i["path"]: i for i in items}
    assert set(by_path) == {"a.txt", "sub/b.txt", "sub/deep/c.bin"}
    assert by_path["sub/b.txt"]["size"] == 2
    assert by_path["sub/deep/c.bin"]["size"] == 3
    assert by_path["a.txt"]["mtime"]

    # prefix handling: relative names come back re-anchored to the prefix
    sub_items = bk.list_files("sub")
    assert {i["path"] for i in sub_items} == {"sub/b.txt", "sub/deep/c.bin"}


def test_delete_removes_file(remote_dir, tmp_path):
    bk = make_backend(remote_dir)
    src = tmp_path / "d.bin"
    src.write_bytes(b"delete me")
    bk.upload(str(src), "d.bin")
    assert bk.exists("d.bin") is True

    bk.delete("d.bin")

    assert bk.exists("d.bin") is False


def test_connect_true_for_valid_remote_false_for_missing(remote_dir, tmp_path):
    bk = make_backend(remote_dir)
    assert bk.connect() is True
    assert bk.connected is True

    bad = make_backend(tmp_path / "no_such_remote_root")
    assert bad.connect() is False
    assert bad.connected is False


# ---------------------------------------------------------------------------
# Failure semantics: stderr tails, loud (not silent) empty listings
# ---------------------------------------------------------------------------


def test_download_missing_file_raises_with_stderr_tail_and_logs(remote_dir, tmp_path, caplog):
    bk = make_backend(remote_dir)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        with pytest.raises(IOError) as ei:
            bk.download("gone.bin", str(tmp_path / "out.bin"))

    msg = str(ei.value)
    assert "rclone download failed" in msg
    # rclone's stderr tail ("directory not found") reached the exception
    assert "directory not found" in msg
    # and the failure was logged with rc and the target path
    assert any("rc=" in r.getMessage() and "gone.bin" in r.getMessage()
               for r in caplog.records)


def test_list_files_failure_returns_empty_and_logs_warning(tmp_path, caplog):
    bk = make_backend(tmp_path / "definitely_missing_remote_root")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert bk.list_files() == []

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("EMPTY listing" in m for m in warnings), warnings


def test_ops_logged_at_info_with_path_duration_rc(remote_dir, tmp_path, caplog):
    bk = make_backend(remote_dir)
    src = tmp_path / "log.bin"
    src.write_bytes(b"logged")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        bk.upload(str(src), "log.bin")
        bk.delete("log.bin")

    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("rclone upload" in m and "log.bin" in m and "rc=0" in m for m in msgs)
    assert any("rclone delete" in m and "log.bin" in m and "rc=0" in m for m in msgs)


# ---------------------------------------------------------------------------
# config_path / extra_args / timeout_s wiring
# ---------------------------------------------------------------------------


def test_config_path_honored_ops_still_work(remote_dir, tmp_path):
    conf = tmp_path / "custom_rclone.conf"
    conf.write_text("[localdisk]\ntype = local\n", encoding="utf-8")
    bk = RcloneBackend("rclone_cfg", {
        "remote": f"localdisk:{remote_dir}",
        "config_path": str(conf),
    })
    data = b"config path honored"
    src = tmp_path / "cf.bin"
    src.write_bytes(data)

    sha = bk.upload(str(src), "cf.bin")
    out = tmp_path / "cf_out.bin"
    bk.download("cf.bin", str(out))

    assert out.read_bytes() == data
    assert bk.hash("cf.bin") == sha
    assert bk.exists("cf.bin") is True
    assert [i["path"] for i in bk.list_files()] == ["cf.bin"]


def test_config_path_and_extra_args_on_every_call(recorder, remote_dir, tmp_path):
    conf = tmp_path / "custom_rclone.conf"
    conf.write_text("[localdisk]\ntype = local\n", encoding="utf-8")
    bk = RcloneBackend("rclone_args", {
        "remote": f"localdisk:{remote_dir}",
        "config_path": str(conf),
        "extra_args": ["--sftp-set-modtime=false", "--checkers=8"],
        "bwlimit": "10M",
    })
    src = tmp_path / "args.bin"
    src.write_bytes(b"args")
    out = tmp_path / "args_out.bin"

    bk.upload(str(src), "args.bin")
    bk.download("args.bin", str(out))
    bk.exists("args.bin")
    bk.list_files()
    bk.hash("args.bin")
    bk.delete("args.bin")
    bk.connect()

    assert recorder.calls, "expected at least one rclone invocation"
    for call in recorder.calls:
        cmd = call["cmd"]
        assert cmd[:1] == ["rclone"]
        assert _flag_pair(cmd, "--config", str(conf)), cmd
        assert "--sftp-set-modtime=false" in cmd, cmd
        assert "--checkers=8" in cmd, cmd
        assert _flag_pair(cmd, "--bwlimit", "10M"), cmd


def test_extra_args_harmless_flag_ops_still_work(remote_dir, tmp_path):
    bk = make_backend(remote_dir, extra_args=["--sftp-set-modtime=false", "--checkers=8"])
    data = b"harmless flags"
    src = tmp_path / "flag.bin"
    src.write_bytes(data)

    sha = bk.upload(str(src), "flag.bin")
    out = tmp_path / "flag_out.bin"
    bk.download("flag.bin", str(out))

    assert out.read_bytes() == data
    assert bk.hash("flag.bin") == sha


def test_timeout_s_accepted_and_used_for_transfers(recorder, remote_dir, tmp_path):
    bk = make_backend(remote_dir, timeout_s=12345)
    assert bk.timeout_s == 12345
    src = tmp_path / "t.bin"
    src.write_bytes(b"t")

    bk.upload(str(src), "t.bin")
    bk.hash("t.bin")

    assert recorder.calls
    for call in recorder.calls:
        assert call["timeout"] == 12345, call


def test_timeout_s_parsed_from_string():
    assert make_backend("x", timeout_s="5400").timeout_s == 5400


def test_timeout_s_bad_value_falls_back_to_default():
    assert make_backend("x", timeout_s="not-a-number").timeout_s == 21600
    assert make_backend("x", timeout_s=0).timeout_s == 21600


def test_list_timeout_capped_at_four_hours(monkeypatch, tmp_path):
    timeouts = []

    def fake_run(cmd, capture_output=False, text=False, errors=None, timeout=None, **kw):
        timeouts.append(timeout)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(rclone_mod.subprocess, "run", fake_run)

    big = make_backend(tmp_path, timeout_s=100000)
    assert big.list_files() == []
    assert timeouts == [4 * 3600]

    small = make_backend(tmp_path, timeout_s=1000)
    assert small.list_files() == []
    assert timeouts[-1] == 1000


# ---------------------------------------------------------------------------
# config.py wiring (_build_backend / load_config)
# ---------------------------------------------------------------------------


def test_build_backend_passes_new_fields():
    b = _build_backend("my_rclone", "rclone", {
        "type": "rclone",
        "remote": "gdrive:backupme",
        "config_path": "C:/rclone/rclone.conf",
        "extra_args": ["--sftp-set-modtime=false", "--transfers=4"],
        "timeout_s": "9999",
        "bwlimit": "10M",
    })
    assert b.config_path == "C:/rclone/rclone.conf"
    assert b.extra_args == ["--sftp-set-modtime=false", "--transfers=4"]
    assert b.timeout_s == 9999
    assert b.bwlimit == "10M"


def test_build_backend_extra_args_string_split_and_bad_timeout_kept_default():
    b = _build_backend("r2", "rclone", {"extra_args": "--flag-a --flag-b",
                                        "timeout_s": "oops"})
    assert b.extra_args == ["--flag-a", "--flag-b"]
    assert b.timeout_s == 21600  # int coercion failed -> BackendDef default kept


def test_load_config_rclone_section_wiring(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "backends:\n"
        "  rclone:\n"
        "  - name: sftp_box\n"
        "    type: rclone\n"
        "    remote: box:/home/backupme\n"
        "    config_path: /etc/rclone/rclone.conf\n"
        "    extra_args:\n"
        "    - --sftp-set-modtime=false\n"
        "    - --transfers=4\n"
        "    timeout_s: 43200\n"
        "    priority: 80\n",
        encoding="utf-8")

    cfg = load_config(str(cfg_file))
    bdef = cfg.backends["rclone"][0]
    assert bdef.name == "sftp_box"
    assert bdef.config_path == "/etc/rclone/rclone.conf"
    assert bdef.extra_args == ["--sftp-set-modtime=false", "--transfers=4"]
    assert bdef.timeout_s == 43200

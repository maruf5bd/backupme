import hashlib

import pytest

from backupme.backends.local import LocalBackend
from backupme.core.uploader import _do_upload


class VerifySpyBackend(LocalBackend):
    """Counts hash()/download() calls; can lie about hashes or corrupt uploads."""

    def __init__(self, name, config, hash_mode="correct", corrupt=False):
        super().__init__(name, config)
        self.hash_mode = hash_mode
        self.corrupt = corrupt
        self.hash_calls = 0
        self.download_calls = 0

    def hash(self, remote_rel_path: str) -> str:
        self.hash_calls += 1
        if self.hash_mode == "raise":
            raise RuntimeError("no remote hash for you")
        real = super().hash(remote_rel_path)
        if self.hash_mode == "lie":
            return "0" * 64
        return real

    def download(self, remote_rel_path: str, local_path: str):
        self.download_calls += 1
        return super().download(remote_rel_path, local_path)

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        if self.corrupt:
            import os
            import shutil
            shutil.copyfile(local_path, local_path + ".probe")
            with open(local_path + ".probe", "r+b") as f:
                f.seek(0)
                f.write(b"CORRUPTED!")
            out = super().upload(local_path + ".probe", remote_rel_path)
            os.remove(local_path + ".probe")
            return out
        return super().upload(local_path, remote_rel_path)


def _src(tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"verify me " * 500)
    return str(p), hashlib.sha256(p.read_bytes()).hexdigest()


def test_correct_hash_never_downloads(tmp_path):
    bk = VerifySpyBackend("v1", {"path": str(tmp_path / "store")})
    bk.connect()
    src, sha = _src(tmp_path)
    assert _do_upload(bk, src, "objects/aa/bb/x", sha) is True
    assert bk.hash_calls >= 1 and bk.download_calls == 0


def test_raising_hash_falls_back_to_download(tmp_path):
    bk = VerifySpyBackend("v2", {"path": str(tmp_path / "store")}, hash_mode="raise")
    bk.connect()
    src, sha = _src(tmp_path)
    assert _do_upload(bk, src, "objects/aa/bb/x", sha) is True
    assert bk.download_calls >= 1


def test_lying_hash_caught_by_download(tmp_path):
    bk = VerifySpyBackend("v3", {"path": str(tmp_path / "store")}, hash_mode="lie")
    bk.connect()
    src, sha = _src(tmp_path)
    assert _do_upload(bk, src, "objects/aa/bb/x", sha) is True
    assert bk.download_calls >= 1


def test_corrupt_upload_fails_after_retries(tmp_path):
    bk = VerifySpyBackend("v4", {"path": str(tmp_path / "store")}, hash_mode="raise", corrupt=True)
    bk.connect()
    src, sha = _src(tmp_path)
    assert _do_upload(bk, src, "objects/aa/bb/x", sha) is False

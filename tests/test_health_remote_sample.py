"""Sampled remote-backend verification (defect D6 detection)."""
import hashlib

import pytest

from backupme.core import data_health
from backupme.core.database import execute, commit


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _insert_storage_row(backend_name: str, path: str, size: int = 10):
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
            (_sha(path), size))
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
        "VALUES (?, ?, ?, ?, ?)",
        (_sha(path), backend_name, path, size, _sha("stored-" + path)),
    )
    commit()


class FakeRemote:
    """Minimal backend double: name/type/connect/exists only."""

    def __init__(self, name: str, existing: set, connect_ok: bool = True):
        self.name = name
        self.type = "sftp"
        self._existing = existing
        self._connect_ok = connect_ok
        self.connected = False

    def connect(self) -> bool:
        self.connected = self._connect_ok
        return self._connect_ok

    def exists(self, remote_rel_path: str) -> bool:
        return remote_rel_path in self._existing


def test_remote_sample_plan_excludes_members_and_caps(env):
    for i in range(8):
        _insert_storage_row("fakebox", f"objects/{i:02d}/aa/file{i}.bin")
    # member rows must never be sampled
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, 10, 1)",
            (_sha("mem"),))
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256) "
        "VALUES (?, 'fakebox', 'objects/00/aa/file0.bin@0:10', 10, ?)",
        (_sha("mem"), _sha("mem")),
    )
    commit()
    plan = data_health.remote_sample_plan("fakebox", n=100)
    assert len(plan) == 8
    assert all("@0:10" not in p for p in plan)
    assert len(data_health.remote_sample_plan("fakebox", n=3)) == 3


def test_sample_remote_backend_detects_phantom_rows(env):
    for i in range(6):
        _insert_storage_row("fakebox", f"objects/{i:02d}/bb/file{i}.bin")
    box = FakeRemote("fakebox", existing={f"objects/{i:02d}/bb/file{i}.bin" for i in range(4)})
    result = data_health.sample_remote_backend(box, n=25)
    assert result["note"] == "sampled-missing"
    assert result["checked"] == 6
    assert sorted(result["missing"]) == ["objects/04/bb/file4.bin", "objects/05/bb/file5.bin"]
    assert box.connected is True


def test_sample_remote_backend_all_present(env):
    paths = {f"objects/{i:02d}/cc/file{i}.bin" for i in range(5)}
    for p in paths:
        _insert_storage_row("fakebox2", p)
    box = FakeRemote("fakebox2", existing=paths)
    result = data_health.sample_remote_backend(box, n=25)
    assert result["note"] == "sampled"
    assert result["missing"] == []
    assert result["checked"] == 5


def test_sample_remote_backend_unreachable(env):
    _insert_storage_row("fakebox3", "objects/00/dd/x.bin")
    box = FakeRemote("fakebox3", existing=set(), connect_ok=False)
    result = data_health.sample_remote_backend(box, n=25)
    assert result["note"] == "unreachable"
    assert result["checked"] == 0
    assert result["errors"]


def test_sample_remote_backend_no_paths(env):
    box = FakeRemote("fakebox4", existing=set())
    result = data_health.sample_remote_backend(box, n=25)
    assert result["note"] == "no-paths"

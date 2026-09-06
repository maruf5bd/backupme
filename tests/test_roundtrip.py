import hashlib

from backupme.core.verify import run_integrity_check
from tests.helpers import backup_bytes, restore_bytes

import pytest


def test_roundtrip_small_binary_file(env, backends):
    # Non-UTF-8 bytes: the old inline path stored these lossily.
    data = bytes(range(256)) + b"\x80\x81\x82 hello \xff\xfe"
    r = backup_bytes(env, "tiny.bin", data, backends)
    assert "file_id" in r
    assert restore_bytes(r["file_id"], env) == data


def test_roundtrip_bundle_file(env, backends):
    data = b"A" * 1024 * 512 + bytes(range(256)) * 4
    r = backup_bytes(env, "medium.dat", data, backends)
    assert restore_bytes(r["file_id"], env) == data


def test_roundtrip_chunked_file(env, backends):
    data = bytes(range(256)) * (12 * 1024 * 1024 // 256)
    r = backup_bytes(env, "large.bin", data, backends)
    assert restore_bytes(r["file_id"], env) == data


def test_roundtrip_directory_tree(env, backends):
    files = {
        "sub/a.txt": b"alpha " + bytes([0xFF]),
        "sub/b.txt": b"beta",
        "sub/deep/c.bin": bytes(range(64)),
    }
    from backupme.core.uploader import upload_bundle
    from tests.helpers import load_cfg
    infos = []
    for rel, data in files.items():
        p = env / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        infos.append({"local_path": str(p), "source_root": str(env), "relative_path": rel})
    results = upload_bundle(infos, backends, load_cfg(), 1)
    for info, res in zip(infos, results):
        assert restore_bytes(res["file_id"], env) == (env / info["relative_path"]).read_bytes()


def test_dedup_second_run_no_new_blobs(env, backends):
    data = b"same content " * 200
    backup_bytes(env, "one.txt", data, backends)
    blobs_before = execute_count("SELECT COUNT(*) FROM blob_storage")
    backup_bytes(env, "two.txt", data, backends)
    assert execute_count("SELECT COUNT(*) FROM blob_storage") == blobs_before


def test_verify_after_backup(env, backends):
    backup_bytes(env, "v1.txt", b"verify me " * 50, backends)
    backup_bytes(env, "v2.txt", bytes(range(32)) * 40, backends)
    rep = run_integrity_check(count=5)
    assert rep["failed"] == 0, rep


def execute_count(sql):
    from backupme.core.database import execute
    return execute(sql).fetchone()[0]

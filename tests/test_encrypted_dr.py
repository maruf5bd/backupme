import pytest

from backupme.core.database import execute
from backupme.core.manifest import rebuild_database_from_manifests
from backupme.core.compress import recompress_blobs
from tests.helpers import backup_bytes, restore_bytes, load_cfg
from backupme.backends import get_all_backends


def test_encrypted_roundtrip(enc_env):
    from backupme.backends import get_all_backends
    bks = get_all_backends(load_cfg())
    data = b"secret " * 100 + bytes(range(256))
    r = backup_bytes(enc_env, "sec.txt", data, bks)
    assert restore_bytes(r["file_id"], enc_env) == data


def test_encrypted_chunked_roundtrip(enc_env):
    from backupme.backends import get_all_backends
    bks = get_all_backends(load_cfg())
    data = bytes(range(256)) * (12 * 1024 * 1024 // 256)
    r = backup_bytes(enc_env, "big.bin", data, bks)
    assert restore_bytes(r["file_id"], enc_env) == data


def test_manifest_v2_carries_nonce(enc_env):
    from backupme.backends import get_all_backends
    bks = get_all_backends(load_cfg())
    backup_bytes(enc_env, "n.txt", b"nonce check " * 50, bks)
    storage = enc_env / "storage"
    mpaths = sorted(storage.rglob("*.manifest.json"))
    assert mpaths
    body = mpaths[0].read_text()
    assert '"v": 2' in body
    assert '"nonce"' in body


def test_rebuild_restores_encrypted(enc_env, tmp_path):
    from backupme.backends import get_all_backends
    from backupme.core import database as dbmod
    bks = get_all_backends(load_cfg())
    data = b"dr secret " * 100
    r = backup_bytes(enc_env, "dr.txt", data, bks)
    # Simulate total DB loss: fresh database, rebuild from manifests.
    dbmod.close_connection()
    dbmod.DB_PATH = tmp_path / "rebuilt.db"
    dbmod.initialize()
    result = rebuild_database_from_manifests(bks)
    assert result["files_rebuilt"] >= 1
    assert result["needs_reupload"] == []
    assert restore_bytes(r["file_id"], tmp_path) == data


def test_rebuild_reports_v1_encrypted_without_crypto(enc_env, tmp_path):
    import json
    from backupme.core import database as dbmod
    from backupme.backends import get_all_backends, invalidate_cache
    # Hand-craft a v1-style manifest with an encrypted storage entry missing nonce/tag.
    manifest = {
        "v": 1, "file_uuid": "deadbeef" * 4, "source_root": str(enc_env),
        "relative_path": "legacy.txt", "sha256": "a" * 64, "size": 10,
        "mtime": 0.0, "version_group": "legacygroup0000", "version_seq": 1,
        "redundancy": 1,
        "chunks": [{
            "seq": 0, "blob_id": "f" * 64, "pt_offset": 0, "pt_size": 10,
            "storage": [{
                "backend": "test_local", "path": "objects/ff/ff/" + "f" * 64,
                "stored_size": 10, "stored_sha256": "b" * 64,
                "algo": None, "level": None, "encrypted": True,
            }],
        }],
    }
    blob_dir = enc_env / "storage" / "manifests" / "de" / "ad"
    blob_dir.mkdir(parents=True)
    (blob_dir / ("deadbeef" * 4 + ".manifest.json")).write_text(json.dumps(manifest))

    dbmod.close_connection()
    dbmod.DB_PATH = tmp_path / "rebuilt2.db"
    dbmod.initialize()
    invalidate_cache()
    result = rebuild_database_from_manifests(get_all_backends(load_cfg()))
    assert "f" * 64 in result["needs_reupload"]


def test_recompress_skips_encrypted_without_key(enc_env):
    from backupme.backends import get_all_backends
    bks = get_all_backends(load_cfg())
    data = b"keep me safe " * 100
    r = backup_bytes(enc_env, "rk.txt", data, bks)
    out = recompress_blobs(algo="zstd", level=1, key=None)
    assert out["skipped_encrypted"] >= 1
    assert restore_bytes(r["file_id"], enc_env) == data

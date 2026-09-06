import pytest

from backupme.core.database import execute, commit
from backupme.core.downloader import download_file, _extract_from_bundle
from tests.helpers import backup_bytes, restore_bytes


def test_good_bundle_restore_unaffected(env, backends):
    data = b"bundle ok " * 100
    r = backup_bytes(env, "g.txt", data, backends)
    assert restore_bytes(r["file_id"], env) == data


def test_legacy_bundle_row_without_range_raises_clearly(env, backends):
    r = backup_bytes(env, "l.txt", b"legacy " * 100, backends)
    chunk = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ? ORDER BY seq LIMIT 1", (r["file_id"],)
    ).fetchone()
    # Rewrite the storage row as a legacy entry without @offset:length
    execute(
        "UPDATE blob_storage SET storage_path = 'bundle/xx/yy/legacy' WHERE blob_id = ?",
        (chunk["blob_id"],),
    )
    commit()
    with pytest.raises(RuntimeError, match="re-backup"):
        restore_bytes(r["file_id"], env)


def test_encrypted_row_without_key_raises_clearly(enc_env):
    from backupme.backends import get_all_backends
    from tests.helpers import load_cfg
    bks = get_all_backends(load_cfg())
    data = b"locked " * 100
    r = backup_bytes(enc_env, "k.txt", data, bks)
    # Simulate a lost key: point the config at a key file that no longer exists.
    cfg_path = enc_env / "config.yaml"
    cfg_path.write_text(cfg_path.read_text().replace("test.key", "gone.key"))
    with pytest.raises(RuntimeError, match="[Kk]ey"):
        restore_bytes(r["file_id"], enc_env)

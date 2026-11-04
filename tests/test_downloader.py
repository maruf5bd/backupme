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


def test_failed_restore_leaves_no_partial_file(env, backends, monkeypatch):
    """QA round D: a restore that fails mid-download must not leave a partial
    or corrupt file at the destination — the previous good content survives
    and no .part temp file leaks."""
    from pathlib import Path
    from backupme.backends.local import LocalBackend
    r = backup_bytes(env, "atomic/a.txt", b"atomic " * 100, backends)
    dest = env / "out" / "a.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"PREVIOUS GOOD CONTENT")

    def bad_download(self, remote_path, local_path):
        Path(local_path).write_bytes(b"garbage")   # hash check will fail

    original_download = LocalBackend.download
    monkeypatch.setattr(LocalBackend, "download", bad_download)
    with pytest.raises(RuntimeError):
        download_file(r["file_id"], str(dest))

    assert dest.read_bytes() == b"PREVIOUS GOOD CONTENT"
    assert list(dest.parent.glob("*.part-*")) == []

    # A later successful restore replaces the destination atomically.
    # (Targeted restore — monkeypatch.undo() would also undo the env
    # fixture's BACKUPME_CONFIG/chdir patches.)
    monkeypatch.setattr(LocalBackend, "download", original_download)
    out = download_file(r["file_id"], str(dest))
    assert out["status"] == "ok"
    assert dest.read_bytes() == b"atomic " * 100
    assert list(dest.parent.glob("*.part-*")) == []

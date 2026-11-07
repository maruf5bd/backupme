import pytest

from backupme.core.database import execute, commit
from backupme.core.downloader import download_file, _extract_from_bundle
from tests.helpers import backup_bytes, restore_bytes


def test_good_bundle_restore_unaffected(env, backends):
    data = b"bundle ok " * 100
    r = backup_bytes(env, "g.txt", data, backends)
    assert restore_bytes(r["file_id"], env) == data


def test_legacy_standalone_object_restores(env, backends):
    """Legacy rows with no @offset:length are standalone compressed objects named
    after the member sha (2026-05-era uploader). When the object physically
    exists, the downloader must recover it instead of refusing."""
    from backupme.core import compress as compress_mod
    import os
    data = b"legacy standalone " * 100
    r = backup_bytes(env, "ls.txt", data, backends)
    chunk = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ? ORDER BY seq LIMIT 1",
        (r["file_id"],),
    ).fetchone()
    sha = chunk["blob_id"].rsplit("/", 1)[-1]
    legacy_path = f"bundle/zz/99/{sha}.zstd"
    backend_root = env.parent / "storage"
    obj = backend_root / legacy_path.replace("/", os.sep)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(compress_mod.compress(data, "zstd"))
    execute(
        "UPDATE blob_storage SET storage_path = ? WHERE blob_id = ?",
        (legacy_path, chunk["blob_id"]),
    )
    commit()
    assert restore_bytes(r["file_id"], env) == data


def test_legacy_row_missing_object_fails_clearly(env, backends):
    """A legacy row whose standalone object does not exist still fails with a
    clear download error (no silent success)."""
    r = backup_bytes(env, "l.txt", b"legacy " * 100, backends)
    chunk = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ? ORDER BY seq LIMIT 1", (r["file_id"],)
    ).fetchone()
    # Rewrite the storage row as a legacy entry pointing at a missing object
    execute(
        "UPDATE blob_storage SET storage_path = 'bundle/xx/yy/legacy' WHERE blob_id = ?",
        (chunk["blob_id"],),
    )
    commit()
    with pytest.raises(RuntimeError, match="Failed to download chunk"):
        restore_bytes(r["file_id"], env)


def _envelope(content: bytes) -> bytes:
    """Build the legacy dedup envelope: 52-byte header + content. The header is
    'bndl/objects/<a>/<b>/<13-hex content-sha prefix>' padded to 32 bytes, then
    20 binary bytes (observed: sizes/offsets, unused by recovery)."""
    import hashlib
    sha = hashlib.sha256(content).hexdigest()
    header = b"bndl/objects/" + sha[:2].encode() + b"/" + sha[2:4].encode() + b"/" \
        + sha[:13].encode()
    header = header.ljust(32, b"\x00") + b"\x00" * 20
    assert len(header) == 52
    return header + content


def test_legacy_dedup_envelope_restores(env, backends):
    """Legacy blobs are dedup envelopes: 52-byte 'bndl/objects/...' header + the
    real content, with the blob named after the whole envelope. Recovery must
    strip the header (validated against the embedded sha prefix)."""
    import hashlib
    import os
    from backupme.core import compress as compress_mod
    data = b"envelope body " * 100
    r = backup_bytes(env, "e.txt", data, backends)
    chunk = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ? ORDER BY seq LIMIT 1",
        (r["file_id"],),
    ).fetchone()
    old_bid = chunk["blob_id"]

    envelope = _envelope(data)
    new_tail = hashlib.sha256(envelope).hexdigest()
    new_bid = f"bundle/zz/97/{new_tail}"
    legacy_path = f"bundle/zz/97/{new_tail}.zstd"

    backend_root = env.parent / "storage"
    obj = backend_root / legacy_path.replace("/", os.sep)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(compress_mod.compress(envelope, "zstd"))

    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
            (new_bid, len(data)))
    execute("UPDATE file_chunks SET blob_id = ? WHERE file_id = ? AND seq = 0",
            (new_bid, r["file_id"]))
    execute("UPDATE blob_storage SET blob_id = ?, storage_path = ? WHERE blob_id = ?",
            (new_bid, legacy_path, old_bid))
    commit()
    assert restore_bytes(r["file_id"], env) == data


def test_legacy_envelope_tampered_content_fails(env, backends):
    """An envelope whose content does not match the header's sha prefix (or the
    file sha) must never be written out as restore output."""
    import hashlib
    import os
    from backupme.core import compress as compress_mod
    r = backup_bytes(env, "t.txt", b"tamper target " * 100, backends)
    chunk = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ? ORDER BY seq LIMIT 1",
        (r["file_id"],),
    ).fetchone()
    old_bid = chunk["blob_id"]

    # Envelope as stored hashes to its own name (so the blob-name gate passes),
    # but its content does not match the header's embedded sha prefix.
    honest_data = b"real body" * 500
    tampered = _envelope(honest_data).replace(b"real body", b"evil body")
    new_tail = hashlib.sha256(tampered).hexdigest()
    new_bid = f"bundle/zz/96/{new_tail}"
    legacy_path = f"bundle/zz/96/{new_tail}.zstd"

    backend_root = env.parent / "storage"
    obj = backend_root / legacy_path.replace("/", os.sep)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(compress_mod.compress(tampered, "zstd"))

    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
            (new_bid, len(honest_data)))
    execute("UPDATE file_chunks SET blob_id = ? WHERE file_id = ? AND seq = 0",
            (new_bid, r["file_id"]))
    execute("UPDATE blob_storage SET blob_id = ?, storage_path = ? WHERE blob_id = ?",
            (new_bid, legacy_path, old_bid))
    commit()
    with pytest.raises(RuntimeError):
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

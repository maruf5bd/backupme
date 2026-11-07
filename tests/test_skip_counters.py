"""B09: skip/failure counters must be visible; B14: crypto capability report."""
import hashlib
import os
from pathlib import Path

from backupme.core import crypto as crypto_mod
from backupme.core import manifest as manifest_mod


def test_crypto_capabilities_shape():
    caps = crypto_mod.capabilities()
    assert caps["cipher"] == "aes-256-gcm"
    assert caps["kdf"] in ("argon2id", "pbkdf2-hmac-sha256 (600k iters)")
    assert isinstance(caps["aead_available"], bool)
    assert caps["file_layout"] in ("tag-at-end (AEAD)", "tag-after-nonce (legacy fallback)")


def test_recompress_counts_failures(env, backends, monkeypatch):
    """A blob whose re-upload fails verification must land in the failed bucket."""
    from backupme.core.database import execute, commit
    from backupme.core import compress as compress_mod

    bk = backends[0]
    blob_id = hashlib.sha256(b"recomp-fail").hexdigest()
    old_data = b"a" * 64
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
            (blob_id, len(old_data)))
    path = f"objects/xx/yy/{blob_id}"
    target = Path(bk.base_path) / path.replace("/", os.sep)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(old_data)
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256, "
        "compression_algo, compression_level) VALUES (?, ?, ?, ?, ?, 'zstd', 3)",
        (blob_id, bk.name, path, len(old_data), hashlib.sha256(old_data).hexdigest()),
    )
    commit()

    # Force post-upload verification to fail: hash() reports a wrong digest.
    monkeypatch.setattr(type(bk), "hash", lambda self, p: "0" * 64)
    result = compress_mod.recompress_blobs(algo="zstd", level=1, backend_name=bk.name)
    assert result["failed"] >= 1
    assert result["recompressed"] == 0


def test_rebuild_reports_unreadable_manifests(env, backends):
    """A corrupt manifest must show up in unreadable_manifests, not vanish."""
    bk = backends[0]
    mdir = Path(bk.base_path) / "manifests" / "aa" / "bb"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "corrupt.manifest.json").write_bytes(b"not-a-manifest")

    result = manifest_mod.rebuild_database_from_manifests([bk])
    assert result["rebuilding"] == "ok"
    assert result["unreadable_manifests"] >= 1

"""mirror.replicate_to_n source fall-through (D14 lesson).

The newest storage row can point at a depleted store; replication must
fall through to older readable copies instead of failing the blob.
"""
import hashlib
import os
from pathlib import Path

from backupme.core.database import commit, execute
from backupme.core.downloader import _find_chunk_storage
from backupme.core.uploader import upload_bundle
from backupme.mirror import replicate_to_n
from tests.helpers import add_backend, load_cfg


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_replicate_falls_through_dead_newest_source(env):
    from backupme.backends import get_all_backends

    storage_root = Path(load_cfg().backends["local"][0].path)
    old_root = storage_root.parent / "old_be_store"
    old_root.mkdir(exist_ok=True)
    add_backend(env, "old_be", old_root)

    cfg = load_cfg()
    backends = get_all_backends(cfg)

    src_dir = storage_root.parent / "src"
    src_dir.mkdir(exist_ok=True)
    data = os.urandom(12_000_000)  # > 10 MB vault threshold -> standalone chunk
    p = src_dir / "f.bin"
    p.write_bytes(data)
    upload_bundle(
        [{"local_path": str(p), "source_root": str(src_dir), "relative_path": "f.bin"}],
        backends, cfg, 2,  # every configured backend gets a chunk copy
    )
    commit()

    # add the replication target AFTER the upload so it holds no copy yet
    target_root = storage_root.parent / "target_store"
    target_root.mkdir(exist_ok=True)
    add_backend(env, "target", target_root)
    cfg = load_cfg()
    backends = get_all_backends(cfg)

    rows = execute(
        "SELECT blob_id FROM blob_storage WHERE backend_name = 'test_local' LIMIT 1"
    ).fetchall()
    blob_id = rows[0]["blob_id"]

    # make the old_be row clearly OLDER, so the newest row is test_local
    execute("UPDATE blob_storage SET uploaded_at = '2026-01-01 00:00:00' WHERE backend_name = 'old_be'")
    commit()
    ordered = _find_chunk_storage(blob_id)
    assert ordered[0]["backend_name"] == "test_local", [r["backend_name"] for r in ordered]

    # destroy the newest copy's object; the old_be copy stays intact
    st_row = next(r for r in ordered if r["backend_name"] == "test_local")
    dead_obj = storage_root / st_row["storage_path"]
    os.remove(dead_obj)

    target_backend = next(b for b in backends if b.name == "target")
    n = replicate_to_n(blob_id, [target_backend], cfg)
    assert n == 1

    copied = target_root / st_row["storage_path"]
    assert copied.is_file()

    # the copied object must decompress to the plaintext chunk sha == blob tail
    from backupme.core import compress as compress_mod

    stored = copied.read_bytes()
    algo = execute(
        "SELECT compression_algo FROM blob_storage WHERE blob_id = ? AND backend_name = 'old_be'",
        (blob_id,),
    ).fetchone()["compression_algo"]
    plaintext = stored if not algo else compress_mod.decompress(stored, algo)
    assert _sha(plaintext) == blob_id.rsplit("/", 1)[-1]


def test_replicate_returns_zero_when_no_source_readable(env):
    from backupme.backends import get_all_backends

    storage_root = Path(load_cfg().backends["local"][0].path)
    cfg = load_cfg()
    backends = get_all_backends(cfg)
    src_dir = storage_root.parent / "src2"
    src_dir.mkdir(exist_ok=True)
    data = os.urandom(11_000_000)
    p = src_dir / "g.bin"
    p.write_bytes(data)
    upload_bundle(
        [{"local_path": str(p), "source_root": str(src_dir), "relative_path": "g.bin"}],
        backends, cfg, 1,
    )
    commit()

    # add the target AFTER the upload so it is a genuine replicate target
    target_root = storage_root.parent / "target_store2"
    target_root.mkdir(exist_ok=True)
    add_backend(env, "target2", target_root)
    cfg = load_cfg()
    backends = get_all_backends(cfg)

    row = execute(
        "SELECT blob_id FROM blob_storage WHERE backend_name = 'test_local' LIMIT 1"
    ).fetchone()
    blob_id = row["blob_id"]

    # wipe every physical copy
    for dp, _dn, fn in os.walk(storage_root):
        for f in fn:
            os.remove(os.path.join(dp, f))

    target_backend = next(b for b in backends if b.name == "target2")
    assert replicate_to_n(blob_id, [target_backend], cfg) == 0

import hashlib
import struct
import tempfile
from pathlib import Path

from backupme.core import hasher as hasher_mod
from backupme.core import compress as compress_mod
from backupme.core import crypto as crypto_mod
from backupme.core.database import execute, commit

_BUNDLE_HEADER_FMT = struct.Struct(">32sQQI")


def _find_chunk_storage(blob_id: str, backend_name: str | None = None) -> list[dict]:
    if backend_name:
        rows = execute(
            "SELECT * FROM blob_storage WHERE blob_id = ? AND backend_name = ? ORDER BY uploaded_at DESC",
            (blob_id, backend_name),
        ).fetchall()
    else:
        rows = execute(
            "SELECT * FROM blob_storage WHERE blob_id = ? ORDER BY uploaded_at DESC",
            (blob_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _extract_from_bundle(bk, remote_path: str, offset: int, length: int, reveal_path: str) -> bytes:
    """Download a specific byte range from a bundled tar on the backend.

    Works with full-download fallback: download the entire bundle, then extract
    the file at the given offset. For backends that support range requests
    (local, SSH, rclone), could be optimized to download only the needed bytes.
    """
    tmp = Path(tempfile.mkdtemp()) / "bundle_full"
    bk.download(remote_path, str(tmp))
    data = tmp.read_bytes()
    tmp.unlink()
    return data[offset:offset + length]


def download_file(
    file_id: int,
    dest_path: str,
    backend_name: str | None = None,
    version: int | None = None,
) -> dict:
    if version is not None:
        file_row = execute(
            "SELECT * FROM files WHERE id = ? AND version_seq = ? AND deleted_at IS NULL",
            (file_id, version),
        ).fetchone()
    else:
        file_row = execute(
            "SELECT * FROM files WHERE id = ? AND deleted_at IS NULL",
            (file_id,),
        ).fetchone()

    if not file_row:
        raise FileNotFoundError(f"File id={file_id} not found")

    chunks = execute(
        "SELECT * FROM file_chunks WHERE file_id = ? ORDER BY seq",
        (file_id,),
    ).fetchall()
    if not chunks:
        return {"status": "error", "message": "No chunks found"}

    from backupme.backends import get_backend as gb
    from backupme.config import get_config
    config = get_config()

    reassembled = bytearray()

    for chunk in chunks:
        blob_id = chunk["blob_id"]
        storage_options = _find_chunk_storage(blob_id, backend_name)
        if not storage_options:
            storage_options = _find_chunk_storage(blob_id)

        data = None
        for st in storage_options:
            bk = None
            for b in gb.get_all_backends(config):
                if b.name == st["backend_name"]:
                    bk = b
                    break
            if bk is None:
                continue

            try:
                remote_path = st["storage_path"]

                if blob_id.startswith("bundle/") and "@" in remote_path:
                    bundle_path, range_spec = remote_path.split("@", 1)
                    offset_str, len_str = range_spec.split(":", 1)
                    offset = int(offset_str)
                    length = int(len_str)
                    data = _extract_from_bundle(bk, bundle_path, offset, length)
                else:
                    tmp = Path(tempfile.mkdtemp()) / "chunk_dl"
                    bk.download(remote_path, str(tmp))
                    data = tmp.read_bytes()
                    tmp.unlink()

                if data is not None:
                    if st["encrypted"] and st["nonce"] and st["auth_tag"]:
                        enc_cfg = config.get("encryption", {})
                        key_file = enc_cfg.get("key_file", "")
                        if key_file:
                            key = crypto_mod.load_key(key_file)
                            data = crypto_mod.decrypt(data, key, bytes(st["nonce"]), bytes(st["auth_tag"]))
                    if st["compression_algo"]:
                        data = compress_mod.decompress(data, st["compression_algo"])
                    if hashlib.sha256(data).hexdigest() == blob_id:
                        break
                    data = None
            except Exception:
                continue

        if data is None:
            raise RuntimeError(f"Failed to download chunk {blob_id}")

        reassembled.extend(data)

    final_sha256 = hashlib.sha256(reassembled).hexdigest()
    if final_sha256 != file_row["sha256"]:
        raise RuntimeError(f"Integrity mismatch: expected {file_row['sha256']}, got {final_sha256}")

    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    Path(dest_path).write_bytes(bytes(reassembled))
    return {"status": "ok", "path": dest_path, "sha256": final_sha256, "size": len(reassembled)}


def download_by_sha256(sha256: str, dest_path: str, backend_name: str | None = None) -> dict:
    rows = execute(
        "SELECT id FROM files WHERE sha256 = ? AND deleted_at IS NULL ORDER BY version_seq DESC LIMIT 1",
        (sha256,),
    ).fetchall()
    if not rows:
        raise FileNotFoundError(f"No file with SHA256 {sha256}")
    return download_file(rows[0]["id"], dest_path, backend_name)
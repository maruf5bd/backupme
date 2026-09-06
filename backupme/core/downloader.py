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


def _extract_from_bundle(bk, remote_path: str, offset: int, length: int, st: dict, config, expected_hash: str | None = None) -> bytes:
    """Download the full bundle, decrypt it, decompress it, and extract the target bytes."""
    tmp_dir = Path(tempfile.mkdtemp())
    tmp = tmp_dir / "bundle_full"
    dec = tmp_dir / "bundle_dec"
    try:
        bk.download(remote_path, str(tmp))
        
        work_file = tmp
        if st.get("encrypted") and st.get("nonce") and st.get("auth_tag"):
            enc_cfg = getattr(config, "encryption", None)
            if enc_cfg and getattr(enc_cfg, "enabled", False):
                key_file = getattr(enc_cfg, "key_file", "")
                if key_file and Path(key_file).exists():
                    key = crypto_mod.load_key(key_file)
                    crypto_mod.decrypt_file(str(work_file), str(dec), key)
                    work_file = dec
                else:
                    raise RuntimeError(f"Encryption key file not found: {key_file}")
            else:
                key_file = "backupme.key"
                if Path(key_file).exists():
                    key = crypto_mod.load_key(key_file)
                    crypto_mod.decrypt_file(str(work_file), str(dec), key)
                    work_file = dec
                    
        data = work_file.read_bytes()
        if st.get("compression_algo"):
            data = compress_mod.decompress(data, st["compression_algo"])
            
        # 1. New seek-based extraction (if offset/length are valid)
        if offset is not None and length is not None and offset > 0:
            return data[offset : offset + length]
            
        # 2. Backwards-compatible walk-based extraction (fallback for old bundled database entries)
        if expected_hash:
            expected_name = ("bndl/" + expected_hash).encode()[:32].ljust(32, b"\0")
            cur_offset = 0
            tar_len = len(data)
            hdr_size = _BUNDLE_HEADER_FMT.size
            while cur_offset + hdr_size <= tar_len:
                header = data[cur_offset : cur_offset + hdr_size]
                name, orig_size, comp_size, flags = _BUNDLE_HEADER_FMT.unpack(header)
                if name == expected_name:
                    data_offset = cur_offset + hdr_size
                    return data[data_offset : data_offset + orig_size]
                cur_offset += hdr_size + orig_size
                
        # 3. Fallback to raw slice if all else fails
        return data[offset : offset + length] if (offset is not None and length is not None) else data
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


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

    from backupme.backends import get_all_backends as _get_all_backends
    from backupme.config import load_config
    config = load_config()

    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    total_written = 0

    with open(dest_path, "wb") as fout:
        for chunk in chunks:
            blob_id = chunk["blob_id"]
            storage_options = _find_chunk_storage(blob_id, backend_name)
            if not storage_options:
                storage_options = _find_chunk_storage(blob_id)

            data = None
            for st in storage_options:
                bk = None
                for b in _get_all_backends(config):
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
                        expected_hash = blob_id.rsplit("/", 1)[-1]
                        data = _extract_from_bundle(bk, bundle_path, offset, length, st, config, expected_hash)
                    elif blob_id.startswith("bundle/"):
                        # Old-style bundled entry fallback where remote_path has no @offset:length!
                        expected_hash = blob_id.rsplit("/", 1)[-1]
                        data = _extract_from_bundle(bk, remote_path, None, None, st, config, expected_hash)
                    else:
                        tmp_dir = Path(tempfile.mkdtemp())
                        tmp = tmp_dir / "chunk_dl"
                        try:
                            bk.download(remote_path, str(tmp))
                            if st["encrypted"] and st["nonce"] and st["auth_tag"]:
                                enc_cfg = getattr(config, "encryption", None)
                                if enc_cfg and getattr(enc_cfg, "enabled", False):
                                    key_file = getattr(enc_cfg, "key_file", "")
                                    if key_file and Path(key_file).exists():
                                        key = crypto_mod.load_key(key_file)
                                        dec_tmp = tmp_dir / "chunk_dec"
                                        crypto_mod.decrypt_file(str(tmp), str(dec_tmp), key)
                                        data = dec_tmp.read_bytes()
                                    else:
                                        raise RuntimeError(f"Encryption key file not found: {key_file}")
                            else:
                                data = tmp.read_bytes()
                            if data is not None and st["compression_algo"]:
                                data = compress_mod.decompress(data, st["compression_algo"])
                        finally:
                            import shutil
                            shutil.rmtree(str(tmp_dir), ignore_errors=True)

                    if data is not None:
                        expected_hash = blob_id.rsplit("/", 1)[-1]
                        if hashlib.sha256(data).hexdigest() == expected_hash:
                            break
                        data = None
                except Exception:
                    continue

            if data is None:
                raise RuntimeError(f"Failed to download chunk {blob_id}")

            fout.write(data)
            hasher.update(data)
            total_written += len(data)

    final_sha256 = hasher.hexdigest()
    if final_sha256 != file_row["sha256"]:
        raise RuntimeError(f"Integrity mismatch: expected {file_row['sha256']}, got {final_sha256}")

    return {"status": "ok", "path": dest_path, "sha256": final_sha256, "size": total_written}


def download_by_sha256(sha256: str, dest_path: str, backend_name: str | None = None) -> dict:
    rows = execute(
        "SELECT id FROM files WHERE sha256 = ? AND deleted_at IS NULL ORDER BY version_seq DESC LIMIT 1",
        (sha256,),
    ).fetchall()
    if not rows:
        raise FileNotFoundError(f"No file with SHA256 {sha256}")
    return download_file(rows[0]["id"], dest_path, backend_name)
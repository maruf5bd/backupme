import hashlib
import shutil
import struct
import tempfile
from pathlib import Path

from backupme.core import hasher as hasher_mod
from backupme.core import compress as compress_mod
from backupme.core import crypto as crypto_mod
from backupme.core.database import execute, commit

_BUNDLE_HEADER_FMT = struct.Struct(">32sQQI")


class UnrecoverableChunkError(RuntimeError):
    """The chunk cannot be restored from this storage row (bad metadata, missing key)."""


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


def _decrypt_if_needed(work_file: Path, dec: Path, st: dict, config) -> Path:
    if not st.get("encrypted"):
        return work_file
    if not (st.get("nonce") and st.get("auth_tag")):
        raise UnrecoverableChunkError(
            f"Blob {st.get('storage_path')} is marked encrypted but has no nonce/auth_tag "
            "(legacy manifest rebuild) — re-upload required"
        )
    enc_cfg = getattr(config, "encryption", None)
    key_file = getattr(enc_cfg, "key_file", "") if enc_cfg else ""
    if not key_file or not Path(key_file).exists():
        raise UnrecoverableChunkError(
            f"Encryption required but key file not configured or missing: '{key_file}'"
        )
    crypto_mod.decrypt_file(str(work_file), str(dec), crypto_mod.load_key(key_file))
    return dec


def _extract_from_bundle(bk, remote_path: str, offset: int | None, length: int | None,
                         st: dict, config, expected_hash: str | None = None) -> bytes:
    """Download the full bundle, decrypt/decompress it, and extract the target bytes."""
    if offset is None or length is None or offset <= 0 or length <= 0:
        raise UnrecoverableChunkError(
            f"Bundle storage row for {remote_path} has no valid @offset:length range "
            "(legacy metadata) — re-backup this file"
        )
    tmp_dir = Path(tempfile.mkdtemp())
    tmp = tmp_dir / "bundle_full"
    dec = tmp_dir / "bundle_dec"
    try:
        bk.download(remote_path, str(tmp))
        work_file = _decrypt_if_needed(tmp, dec, st, config)
        data = work_file.read_bytes()
        if st.get("compression_algo"):
            data = compress_mod.decompress(data, st["compression_algo"])
        if offset + length > len(data):
            raise RuntimeError(
                f"Bundle {remote_path} truncated: need {offset + length} bytes, have {len(data)}"
            )
        return data[offset : offset + length]
    finally:
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
                            work = _decrypt_if_needed(tmp, tmp_dir / "chunk_dec", st, config)
                            data = work.read_bytes()
                            if st["compression_algo"]:
                                data = compress_mod.decompress(data, st["compression_algo"])
                        finally:
                            shutil.rmtree(str(tmp_dir), ignore_errors=True)

                    if data is not None:
                        expected_hash = blob_id.rsplit("/", 1)[-1]
                        if hashlib.sha256(data).hexdigest() == expected_hash:
                            break
                        data = None
                except UnrecoverableChunkError:
                    raise
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
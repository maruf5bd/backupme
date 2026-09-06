import hashlib
import json
import os
import struct
import sys
import tempfile
import time
import uuid
from pathlib import Path

from backupme.core.database import execute, commit, bump_generation, get_generation
from backupme.core import hasher as hasher_mod
from backupme.core import compress as compress_mod
from backupme.core import crypto as crypto_mod
from backupme.core import retry as retry_mod
from backupme.core import dedup as dedup_mod
from backupme.core import index as index_mod
from backupme.core import state as state_mod
from backupme.core import op_log as op_log_mod
from backupme.core.manifest import Manifest, ManifestChunk, ChunkStorageEntry, write_manifest
from backupme.backends import get_backend


def _long_path(p: str) -> str:
    """On Windows, prefix path with \\\\?\\ to bypass 260-char limit."""
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p


_chunk_min_size = 10 * 1024 * 1024
_chunk_size = 64 * 1024 * 1024
_inline_max = 1024
_vault_max = 10 * 1024 * 1024
_no_compress_max = 256
_bundle_target_size = 64 * 1024 * 1024
_circuit_breaker = retry_mod.CircuitBreaker()

_BUNDLE_HEADER_FMT = struct.Struct(">32sQQI")


def _make_blob_id(plaintext_sha256: str) -> str:
    return f"objects/{plaintext_sha256[:2]}/{plaintext_sha256[2:4]}/{plaintext_sha256}"


def _make_pointer(manifest: Manifest) -> dict:
    return {
        "v": 1,
        "file_uuid": manifest.file_uuid,
        "source_root": manifest.source_root,
        "relative_path": manifest.relative_path,
        "size": manifest.size,
        "sha256": manifest.sha256,
        "mtime": manifest.mtime,
        "version_group": manifest.version_group,
        "version_seq": manifest.version_seq,
        "manifest_path": f"manifests/{manifest.file_uuid[:2]}/{manifest.file_uuid[2:4]}/{manifest.file_uuid}.manifest.json",
    }


def _write_pointer(backend, pointer: dict, manifest: Manifest):
    remote_path = f"tree/{manifest.source_root.replace(':', '_')}/{manifest.relative_path}.backupme"
    data = json.dumps(pointer, indent=2)
    tmp = Path(tempfile.mkdtemp()) / "pointer.json"
    tmp.write_text(data)
    try:
        backend.upload(str(tmp), remote_path)
    finally:
        tmp.unlink(missing_ok=True)


def _do_upload(backend, local_path: str, remote_path: str) -> bool:
    retry = retry_mod.ExponentialBackoff(max_attempts=4)
    expected = hasher_mod.sha256_file(local_path)
    for attempt, delay in retry:
        try:
            backend.upload(local_path, remote_path)
            
            # Strict post-upload download and cross-check verification
            dl_dir = Path(tempfile.mkdtemp())
            dl = dl_dir / "verify.tmp"
            try:
                backend.download(remote_path, str(dl))
                if dl.exists():
                    downloaded_size = dl.stat().st_size
                    # Check for 0-byte stub files created by async virtual file systems (like DriveFS)
                    if downloaded_size == 0 and os.path.getsize(local_path) > 0:
                        raise RuntimeError("Downloaded file is empty (detected broken virtual stub)")
                    
                    remote_hash = hasher_mod.sha256_file(str(dl))
                else:
                    raise RuntimeError("Downloaded file does not exist after verification download")
            finally:
                import shutil
                shutil.rmtree(str(dl_dir), ignore_errors=True)
                
            if remote_hash == expected:
                _circuit_breaker.record_success(backend.name)
                return True
            else:
                raise RuntimeError(f"Hash mismatch: expected {expected}, got {remote_hash}")
        except Exception as e:
            if attempt < 4:
                time.sleep(delay)
            continue
    _circuit_breaker.record_failure(backend.name)
    return False


def _upload_chunk_to_backends(
    chunk_data: bytes, blob_id: str, pt_offset: int, pt_size: int,
    backends: list, compress_algo: str | None, compress_level: int | None,
    encrypted: bool, nonce: bytes | None, auth_tag: bytes | None,
    seq: int, file_id: int,
) -> list[dict]:
    tmp = Path(tempfile.mkdtemp()) / "chunk"
    tmp.write_bytes(chunk_data)
    stored_sha256 = hashlib.sha256(chunk_data).hexdigest()
    stored_size = len(chunk_data)
    remote_path = _make_blob_id(blob_id)
    if encrypted and compress_algo and compress_algo != "none":
        remote_path += f".{compress_algo}.aes"
    elif compress_algo and compress_algo != "none":
        remote_path += f".{compress_algo}"

    storage_rows = []
    dedup_mod.record_blob(blob_id, pt_size)
    for bk in backends:
        if _circuit_breaker.is_tripped(bk.name):
            continue
        ok = _do_upload(bk, str(tmp), remote_path)
        if ok:
            execute(
                """INSERT OR IGNORE INTO blob_storage
                   (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                    compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (blob_id, bk.name, remote_path, stored_size, stored_sha256,
                 compress_algo, compress_level, 1 if encrypted else 0, nonce, auth_tag),
            )
            storage_rows.append({
                "backend": bk.name,
                "path": remote_path,
                "stored_size": stored_size,
                "stored_sha256": stored_sha256,
                "algo": compress_algo,
                "level": compress_level,
                "encrypted": encrypted,
            })

    execute(
        "INSERT OR IGNORE INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
        (file_id, seq, blob_id, pt_offset, pt_size),
    )
    commit()
    tmp.unlink(missing_ok=True)
    return storage_rows


def _compress_data(data: bytes) -> tuple[bytes, str | None, int | None]:
    if len(data) <= _no_compress_max:
        return data, None, None
    algo, level = compress_mod.choose_compression_level(len(data))
    compressed = compress_mod.compress(data, algo, level)
    return compressed, algo, level


def _encrypt_data(data: bytes, config: dict) -> tuple[bytes, bytes | None, bytes | None]:
    enc_cfg = config.get("encryption", {})
    if not enc_cfg.get("enabled", False):
        return data, None, None
    key_file = enc_cfg.get("key_file", "")
    if not key_file:
        return data, None, None
    key_path = Path(key_file)
    if not key_path.exists():
        return data, None, None
    key = crypto_mod.load_key(str(key_path))
    ct, nonce, tag = crypto_mod.encrypt(data, key)
    return ct, nonce, tag


def _upload_single_blob(
    data: bytes, blob_id: str, pt_offset: int, pt_size: int,
    backends: list, config: dict, redundancy: int,
    seq: int, file_id: int,
) -> list[dict]:
    enc_cfg = config.get("encryption", {})
    if enc_cfg.get("enabled", False):
        key_file = enc_cfg.get("key_file", "")
        if key_file and Path(key_file).exists():
            compressed, algo, level = _compress_data(data)
            tmp_dir = Path(tempfile.mkdtemp())
            try:
                comp_file = tmp_dir / "compressed"
                comp_file.write_bytes(compressed)
                del compressed
                
                key = crypto_mod.load_key(str(key_file))
                enc_file = tmp_dir / "encrypted"
                nonce, auth_tag = crypto_mod.encrypt_file(str(comp_file), str(enc_file), key)
                
                stored_sha256 = hasher_mod.sha256_file(str(enc_file))
                stored_size = os.path.getsize(str(enc_file))
                
                remote_path = _make_blob_id(blob_id)
                if algo and algo != "none":
                    remote_path += f".{algo}.aes"
                else:
                    remote_path += ".aes"
                
                storage_rows = []
                dedup_mod.record_blob(blob_id, pt_size)
                for bk in backends[:redundancy]:
                    if _circuit_breaker.is_tripped(bk.name):
                        continue
                    ok = _do_upload(bk, str(enc_file), remote_path)
                    if ok:
                        execute(
                            """INSERT OR IGNORE INTO blob_storage
                               (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                                compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                            (blob_id, bk.name, remote_path, stored_size, stored_sha256,
                             algo, level, 1, nonce, auth_tag),
                        )
                        storage_rows.append({
                            "backend": bk.name,
                            "path": remote_path,
                            "stored_size": stored_size,
                            "stored_sha256": stored_sha256,
                            "algo": algo,
                            "level": level,
                            "encrypted": True,
                        })
                execute(
                    "INSERT OR IGNORE INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
                    (file_id, seq, blob_id, pt_offset, pt_size),
                )
                commit()
                return storage_rows
            finally:
                import shutil
                shutil.rmtree(str(tmp_dir), ignore_errors=True)

    compressed, algo, level = _compress_data(data)
    encrypted_data, nonce, auth_tag = _encrypt_data(compressed, config)
    targets = backends[:redundancy]
    return _upload_chunk_to_backends(
        encrypted_data, blob_id, pt_offset, pt_size,
        targets, algo, level,
        encrypted=(nonce is not None), nonce=nonce, auth_tag=auth_tag,
        seq=seq, file_id=file_id,
    )


def upload_bundle(
    files: list[dict],
    backends: list,
    config: dict,
    redundancy: int = 1,
) -> list[dict]:
    """Batch small files (1KB-64KB) into tar archives for faster transfer.

    Multiple small files are packed into a single tar, then compressed+encrypted
    together. This reduces round-trips from N to 1, and achieves far better
    compression since zstd can exploit cross-file redundancy.

    Each file in the bundle gets its own file_chunks row with offset into the
    tar, so download can extract individual files without downloading the whole
    bundle.
    """
    if not isinstance(config, dict):
        from dataclasses import asdict
        config = asdict(config)

    results = []

    batch: list[tuple[dict, Manifest, int, bytes]] = []
    batch_size = 0

    def _flush_batch():
        nonlocal batch, batch_size
        if not batch:
            return

        tar_path = Path(tempfile.mkdtemp()) / "bundle.tar"
        with open(tar_path, "wb") as f:
            for _, _, _, data in batch:
                name_bytes = ("bndl/" + _make_blob_id(hashlib.sha256(data).hexdigest())).encode()[:32]
                header = _BUNDLE_HEADER_FMT.pack(
                    name_bytes.ljust(32, b"\0"), len(data), len(data), 0
                )
                f.write(header)
                f.write(data)

        tar_data = tar_path.read_bytes()
        tar_sha256 = hashlib.sha256(tar_data).hexdigest()
        tar_path.unlink(missing_ok=True)

        compressed, algo, level = _compress_data(tar_data)
        encrypted_data, nonce, auth_tag = _encrypt_data(compressed, config)
        stored_sha256 = hashlib.sha256(encrypted_data).hexdigest()
        stored_size = len(encrypted_data)
        bundle_blob_id = f"bundle/{tar_sha256[:2]}/{tar_sha256[2:4]}/{tar_sha256}"
        remote_path = bundle_blob_id
        if algo and algo != "none":
            remote_path += f".{algo}"
        if nonce is not None:
            remote_path += ".aes"

        tmp = Path(tempfile.mkdtemp()) / "bundle"
        tmp.write_bytes(encrypted_data)
        targets = [b for b in backends[:redundancy] if not _circuit_breaker.is_tripped(b.name)]

        offset = 0
        entries_by_file: dict[int, list[dict]] = {}
        for item, _, file_id, data in batch:
            hdr_len = _BUNDLE_HEADER_FMT.size
            entry = {
                "blob_id": hashlib.sha256(data).hexdigest(),
                "pt_offset": 0,
                "pt_size": len(data),
                "stored_offset": offset + hdr_len,
                "stored_len": len(data),
            }
            entries_by_file.setdefault(file_id, []).append(entry)
            offset += hdr_len + len(data)

        uploaded_to_any = False
        recorded_blobs: set[str] = set()
        for bk in targets:
            ok = _do_upload(bk, str(tmp), remote_path)
            if ok:
                # [OLD BATCH UPLOAD DB INSERTION START]
                # uploaded_to_any = True
                # execute(
                #     """INSERT OR IGNORE INTO blob_storage
                #        (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                #         compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at)
                #        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                #     (bundle_blob_id, bk.name, remote_path, stored_size, stored_sha256,
                #      algo, level, 1 if nonce is not None else 0, nonce, auth_tag),
                # )
                # [OLD BATCH UPLOAD DB INSERTION END]

                # Call record_blob BEFORE inserting into blob_storage/file_chunks to avoid FOREIGN KEY failures
                if not uploaded_to_any:
                    dedup_mod.record_blob(bundle_blob_id, stored_size)
                uploaded_to_any = True
                
                execute(
                    """INSERT OR IGNORE INTO blob_storage
                       (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                        compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (bundle_blob_id, bk.name, remote_path, stored_size, stored_sha256,
                     algo, level, 1 if nonce is not None else 0, nonce, auth_tag),
                )
                for item, manifest, file_id, _ in batch:
                    for entry in entries_by_file.get(file_id, []):
                        mc = ManifestChunk(
                            seq=0,
                            blob_id=bundle_blob_id,
                            pt_offset=0,
                            pt_size=entry["pt_size"],
                        )
                        mc.storage.append(ChunkStorageEntry(
                            backend=bk.name,
                            path=f"{remote_path}@{entry['stored_offset']}:{entry['stored_len']}",
                            stored_size=entry["stored_len"],
                            stored_sha256=stored_sha256,
                            algo=algo,
                            level=level,
                            encrypted=(nonce is not None),
                        ))
                        manifest.chunks.append(mc)
                        
                        unique_chunk_blob_id = f"bundle/{tar_sha256[:2]}/{tar_sha256[2:4]}/{tar_sha256}/{entry['blob_id']}"
                        dedup_mod.record_blob(unique_chunk_blob_id, entry["pt_size"])
                        execute(
                            """INSERT OR IGNORE INTO blob_storage
                               (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                                compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                            (unique_chunk_blob_id, bk.name, f"{remote_path}@{entry['stored_offset']}:{entry['stored_len']}",
                             entry["stored_len"], stored_sha256, algo, level, 1 if nonce is not None else 0, nonce, auth_tag),
                        )
                        execute(
                            "INSERT OR IGNORE INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
                            (file_id, 0, unique_chunk_blob_id, 0, entry["pt_size"]),
                        )

                    if item["sha256"] not in recorded_blobs:
                        dedup_mod.record_blob(item["sha256"], item["size"])
                        recorded_blobs.add(item["sha256"])
                    for bk2 in backends[:redundancy]:
                        try:
                            write_manifest(bk2, manifest)
                            pointer = _make_pointer(manifest)
                            _write_pointer(bk2, pointer, manifest)
                        except Exception:
                            pass

                results.extend([
                    {"file_id": item["file_id"], "sha256": item["sha256"], "bundled": True, "local_path": item["local_path"]}
                    for item, _, _, _ in batch
                ])

        # [OLD RECORD_BLOB POST-LOOP START]
        # if uploaded_to_any:
        #     dedup_mod.record_blob(bundle_blob_id, stored_size)
        # [OLD RECORD_BLOB POST-LOOP END]
        commit()
        tmp.unlink(missing_ok=True)
        batch.clear()
        batch_size = 0

    for file_info in files:
        try:
            local_path = file_info["local_path"]
            src_path = _long_path(local_path)
            source_root = file_info.get("source_root", "")
            relative_path = file_info.get("relative_path", local_path)
            version_group = file_info.get("version_group")
            version_seq = file_info.get("version_seq", 1)

            file_size = os.path.getsize(src_path)
            file_sha256 = hasher_mod.sha256_file(src_path)

            existing = dedup_mod.get_blob(file_sha256)
            if existing and existing["ref_count"] > 0:
                file_id = index_mod.index_file(
                    source_root, relative_path, file_sha256, file_size,
                    mtime=os.path.getmtime(src_path), chunked=False,
                    redundancy=redundancy, version_seq=version_seq,
                    version_group=version_group,
                )
                # Find the original file's chunks and copy them to the deduplicated file!
                orig_file = execute(
                    "SELECT id FROM files WHERE sha256 = ? AND id != ? AND deleted_at IS NULL ORDER BY id ASC LIMIT 1",
                    (file_sha256, file_id),
                ).fetchone()
                if orig_file:
                    orig_chunks = execute(
                        "SELECT seq, blob_id, pt_offset, pt_size FROM file_chunks WHERE file_id = ?",
                        (orig_file["id"],),
                    ).fetchall()
                    for c in orig_chunks:
                        execute(
                            "INSERT OR IGNORE INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
                            (file_id, c["seq"], c["blob_id"], c["pt_offset"], c["pt_size"]),
                        )
                else:
                    # Fallback: if no original file chunks found, map it directly to the file's own sha256 blob
                    execute(
                        "INSERT OR IGNORE INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
                        (file_id, 0, file_sha256, 0, file_size),
                    )
                commit()
                results.append({"file_id": file_id, "sha256": file_sha256, "dedup": True, "local_path": local_path})
                continue

            manifest = Manifest(
                file_uuid=uuid.uuid4().hex,
                source_root=source_root,
                relative_path=relative_path,
                sha256=file_sha256,
                size=file_size,
                mtime=os.path.getmtime(src_path),
                version_group=version_group or file_sha256[:16],
                version_seq=version_seq,
                redundancy=redundancy,
            )

            if file_size <= _inline_max:
                content_text = Path(src_path).read_bytes().decode("utf-8", errors="replace")
                file_id = index_mod.index_file(
                    source_root, relative_path, file_sha256, file_size,
                    mtime=os.path.getmtime(src_path), chunked=False,
                    redundancy=redundancy, version_seq=version_seq,
                    version_group=manifest.version_group, content_text=content_text,
                )
                dedup_mod.record_blob(file_sha256, file_size)
                results.append({"file_id": file_id, "sha256": file_sha256, "inline": True, "local_path": local_path})
                continue

            file_id = index_mod.index_file(
                source_root, relative_path, file_sha256, file_size,
                mtime=os.path.getmtime(src_path), chunked=(file_size > _chunk_min_size),
                redundancy=redundancy, version_seq=version_seq,
                version_group=manifest.version_group,
            )

            if file_size > _chunk_min_size:
                with open(src_path, "rb") as fsrc:
                    for seq, offset in enumerate(range(0, file_size, _chunk_size)):
                        chunk_data = fsrc.read(_chunk_size)
                        chunk_sha256 = hashlib.sha256(chunk_data).hexdigest()
                        dedup_mod.record_blob(chunk_sha256, len(chunk_data))
                        storage_rows = _upload_single_blob(
                            chunk_data, chunk_sha256, offset, len(chunk_data),
                            backends, config, redundancy, seq, file_id,
                        )
                        mc = ManifestChunk(seq=seq, blob_id=chunk_sha256, pt_offset=offset, pt_size=len(chunk_data))
                        for s in storage_rows:
                            mc.storage.append(ChunkStorageEntry(**s))
                        manifest.chunks.append(mc)
            elif _inline_max < file_size <= _vault_max:
                data = Path(src_path).read_bytes()
                dedup_mod.record_blob(file_sha256, file_size)
                batch.append(({"file_id": file_id, "sha256": file_sha256, "size": file_size, "local_path": local_path}, manifest, file_id, data))
                batch_size += _BUNDLE_HEADER_FMT.size + len(data)
                if batch_size >= _bundle_target_size:
                    _flush_batch()
            else:
                data = Path(src_path).read_bytes()
                storage_rows = _upload_single_blob(
                    data, file_sha256, 0, file_size,
                    backends, config, redundancy, 0, file_id,
                )
                mc = ManifestChunk(seq=0, blob_id=file_sha256, pt_offset=0, pt_size=file_size)
                for s in storage_rows:
                    mc.storage.append(ChunkStorageEntry(**s))
                manifest.chunks.append(mc)
                dedup_mod.record_blob(file_sha256, file_size)

            for bk in backends[:redundancy]:
                try:
                    write_manifest(bk, manifest)
                    pointer = _make_pointer(manifest)
                    _write_pointer(bk, pointer, manifest)
                except Exception:
                    pass

            results.append({"file_id": file_id, "sha256": file_sha256, "file_uuid": manifest.file_uuid, "local_path": local_path})
        except Exception as e:
            print(f"Error processing {file_info.get('local_path')}: {e}", file=sys.stderr)
            results.append({"local_path": file_info.get("local_path"), "error": str(e)})

    _flush_batch()
    bump_generation()
    return results


def upload_file(
    source_root: str,
    relative_path: str,
    local_path: str,
    backends: list,
    config: dict,
    redundancy: int = 1,
    version_seq: int = 1,
    version_group: str | None = None,
) -> dict:
    results = upload_bundle(
        [{
            "local_path": local_path,
            "source_root": source_root,
            "relative_path": relative_path,
            "version_group": version_group,
            "version_seq": version_seq,
        }],
        backends, config, redundancy,
    )
    return results[0] if results else {"error": "upload failed"}
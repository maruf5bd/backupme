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
    chunk_data: bytes, blob_id: str,
    backends: list, compress_algo: str | None, compress_level: int | None,
    encrypted: bool, nonce: bytes | None, auth_tag: bytes | None,
) -> list[dict]:
    """Upload one blob to every healthy backend. Pure I/O — no DB writes."""
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
    for bk in backends:
        if _circuit_breaker.is_tripped(bk.name):
            continue
        if _do_upload(bk, str(tmp), remote_path):
            storage_rows.append({
                "backend": bk.name,
                "path": remote_path,
                "stored_size": stored_size,
                "stored_sha256": stored_sha256,
                "algo": compress_algo,
                "level": compress_level,
                "encrypted": encrypted,
                "nonce": nonce.hex() if nonce else None,
                "auth_tag": auth_tag.hex() if auth_tag else None,
            })
    tmp.unlink(missing_ok=True)
    return storage_rows


def _record_chunk_upload(
    blob_id: str, blob_pt_size: int, storage_rows: list[dict],
    file_id: int, seq: int, pt_offset: int, pt_size: int,
) -> None:
    """DB side of a chunk upload. Caller owns the transaction (no commit here)."""
    dedup_mod.ensure_blob(blob_id, blob_pt_size)
    for s in storage_rows:
        execute(
            """INSERT OR IGNORE INTO blob_storage
               (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (blob_id, s["backend"], s["path"], s["stored_size"], s["stored_sha256"],
             s["algo"], s["level"], 1 if s["encrypted"] else 0,
             bytes.fromhex(s["nonce"]) if s.get("nonce") else None,
             bytes.fromhex(s["auth_tag"]) if s.get("auth_tag") else None),
        )
    execute(
        "INSERT OR IGNORE INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
        (file_id, seq, blob_id, pt_offset, pt_size),
    )
    dedup_mod.add_ref(blob_id)


def _write_manifest_and_pointers(backends: list, manifest: Manifest, redundancy: int) -> None:
    for bk2 in backends[:redundancy]:
        try:
            write_manifest(bk2, manifest)
            _write_pointer(bk2, _make_pointer(manifest), manifest)
        except Exception as e:
            print(f"warning: manifest/pointer write failed on backend {bk2.name} "
                  f"for {manifest.relative_path}: {e}", file=sys.stderr)


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
    data: bytes, blob_id: str,
    backends: list, config: dict,
) -> list[dict]:
    """Upload a single blob (encrypting via file streaming when configured). Pure I/O."""
    enc_cfg = config.get("encryption", {})
    enc_enabled = enc_cfg.get("enabled", False)
    key_file = enc_cfg.get("key_file", "")

    if enc_enabled and key_file and Path(key_file).exists():
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
            for bk in backends:
                if _circuit_breaker.is_tripped(bk.name):
                    continue
                if _do_upload(bk, str(enc_file), remote_path):
                    storage_rows.append({
                        "backend": bk.name,
                        "path": remote_path,
                        "stored_size": stored_size,
                        "stored_sha256": stored_sha256,
                        "algo": algo,
                        "level": level,
                        "encrypted": True,
                        "nonce": nonce.hex(),
                        "auth_tag": auth_tag.hex(),
                    })
            return storage_rows
        finally:
            import shutil
            shutil.rmtree(str(tmp_dir), ignore_errors=True)

    compressed, algo, level = _compress_data(data)
    encrypted_data, nonce, auth_tag = _encrypt_data(compressed, config)
    return _upload_chunk_to_backends(
        encrypted_data, blob_id,
        backends, algo, level,
        encrypted=(nonce is not None), nonce=nonce, auth_tag=auth_tag,
    )


def upload_bundle(
    files: list[dict],
    backends: list,
    config,
    redundancy: int = 1,
) -> list[dict]:
    """Upload files: bundle small ones, chunk big ones, dedup identical content.

    Accounting rules:
    - DB rows are written only after at least one backend accepted the data.
    - All DB writes for a file (or batch) happen in one transaction.
    - Every input file gets exactly one result entry with a status:
      "ok", "dedup", "partial" (uploaded to fewer backends than requested),
      or "error".
    """
    if not isinstance(config, dict):
        from dataclasses import asdict
        config = asdict(config)

    results = []

    batch: list[tuple[dict, Manifest, bytes]] = []
    batch_size = 0

    def _flush_batch():
        nonlocal batch, batch_size
        if not batch:
            return

        tar_path = Path(tempfile.mkdtemp()) / "bundle.tar"
        with open(tar_path, "wb") as f:
            for _, _, data in batch:
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

        ok_backends = []
        failed_backends = []
        for bk in targets:
            if _do_upload(bk, str(tmp), remote_path):
                ok_backends.append(bk)
            else:
                failed_backends.append(bk.name)
        tmp.unlink(missing_ok=True)

        if not ok_backends:
            for item_info, _, _ in batch:
                results.append({
                    "local_path": item_info["local_path"], "status": "error",
                    "error": "all backends failed",
                })
            batch.clear()
            batch_size = 0
            return

        # Member offsets within the tar
        offset = 0
        entries: list[tuple[dict, Manifest, dict]] = []
        for item_info, manifest, data in batch:
            hdr_len = _BUNDLE_HEADER_FMT.size
            entry = {
                "member_sha": hashlib.sha256(data).hexdigest(),
                "stored_offset": offset + hdr_len,
                "stored_len": len(data),
                "pt_size": len(data),
            }
            entries.append((item_info, manifest, entry))
            offset += hdr_len + len(data)

        # One transaction for the whole batch: blob rows, storage rows, chunk
        # mappings, and the file index rows are committed together.
        dedup_mod.ensure_blob(bundle_blob_id, len(tar_data))
        for bk in ok_backends:
            execute(
                """INSERT OR IGNORE INTO blob_storage
                   (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                    compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (bundle_blob_id, bk.name, remote_path, stored_size, stored_sha256,
                 algo, level, 1 if nonce is not None else 0,
                 nonce.hex() if nonce else None, auth_tag.hex() if auth_tag else None),
            )
        for item_info, manifest, entry in entries:
            file_id = index_mod.index_file(
                item_info["source_root"], item_info["relative_path"], item_info["sha256"],
                item_info["size"], mtime=item_info["mtime"], chunked=False,
                redundancy=redundancy, version_seq=item_info["version_seq"],
                version_group=item_info["version_group"], auto_commit=False,
            )
            item_info["file_id"] = file_id
            member_id = f"{bundle_blob_id}/{entry['member_sha']}"
            dedup_mod.ensure_blob(member_id, entry["pt_size"])
            dedup_mod.add_ref(bundle_blob_id)  # member -> parent bundle reference
            for bk in ok_backends:
                execute(
                    """INSERT OR IGNORE INTO blob_storage
                       (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                        compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (member_id, bk.name,
                     f"{remote_path}@{entry['stored_offset']}:{entry['stored_len']}",
                     entry["stored_len"], stored_sha256, algo, level,
                     1 if nonce is not None else 0,
                     nonce.hex() if nonce else None, auth_tag.hex() if auth_tag else None),
                )
            execute(
                "INSERT OR IGNORE INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
                (file_id, 0, member_id, 0, entry["pt_size"]),
            )
            dedup_mod.add_ref(member_id)
            manifest.v = 2
            mc = ManifestChunk(seq=0, blob_id=member_id, pt_offset=0, pt_size=entry["pt_size"])
            for bk in ok_backends:
                mc.storage.append(ChunkStorageEntry(
                    backend=bk.name,
                    path=f"{remote_path}@{entry['stored_offset']}:{entry['stored_len']}",
                    stored_size=entry["stored_len"],
                    stored_sha256=stored_sha256,
                    algo=algo,
                    level=level,
                    encrypted=(nonce is not None),
                    nonce=nonce.hex() if nonce else None,
                    auth_tag=auth_tag.hex() if auth_tag else None,
                ))
            manifest.chunks.append(mc)
        commit()

        for _, manifest, _ in entries:
            _write_manifest_and_pointers(backends, manifest, redundancy)

        status = "ok" if len(ok_backends) >= redundancy else "partial"
        for item_info, _, _ in entries:
            results.append({
                "file_id": item_info["file_id"],
                "sha256": item_info["sha256"],
                "status": status,
                "bundled": True,
                "failed_backends": failed_backends,
                "local_path": item_info["local_path"],
            })
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
            mtime = os.path.getmtime(src_path)

            # Content dedup: some live file already has this content.
            prior_chunks = execute(
                """SELECT fc.seq, fc.blob_id, fc.pt_offset, fc.pt_size
                   FROM file_chunks fc
                   JOIN files f2 ON f2.id = fc.file_id
                   WHERE f2.sha256 = ? AND f2.deleted_at IS NULL
                   ORDER BY f2.id ASC""",
                (file_sha256,),
            ).fetchall()
            if prior_chunks:
                file_id = index_mod.index_file(
                    source_root, relative_path, file_sha256, file_size,
                    mtime=mtime, chunked=False, redundancy=redundancy,
                    version_seq=version_seq,
                    version_group=version_group or file_sha256[:16],
                    auto_commit=False,
                )
                for c in prior_chunks:
                    execute(
                        "INSERT OR IGNORE INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
                        (file_id, c["seq"], c["blob_id"], c["pt_offset"], c["pt_size"]),
                    )
                    dedup_mod.add_ref(c["blob_id"])
                commit()
                results.append({"file_id": file_id, "sha256": file_sha256,
                                "dedup": True, "status": "ok", "local_path": local_path})
                continue

            manifest = Manifest(
                file_uuid=uuid.uuid4().hex,
                source_root=source_root,
                relative_path=relative_path,
                sha256=file_sha256,
                size=file_size,
                mtime=mtime,
                version_group=version_group or file_sha256[:16],
                version_seq=version_seq,
                redundancy=redundancy,
            )
            manifest.v = 2

            if file_size <= _vault_max:
                data = Path(src_path).read_bytes()
                item_info = {
                    "file_id": None, "sha256": file_sha256, "size": file_size,
                    "local_path": local_path, "source_root": source_root,
                    "relative_path": relative_path, "mtime": mtime,
                    "version_group": manifest.version_group, "version_seq": version_seq,
                }
                batch.append((item_info, manifest, data))
                batch_size += _BUNDLE_HEADER_FMT.size + len(data)
                if batch_size >= _bundle_target_size:
                    _flush_batch()
            else:
                pending = []
                min_copies = redundancy
                with open(src_path, "rb") as fsrc:
                    for seq, offset in enumerate(range(0, file_size, _chunk_size)):
                        chunk_data = fsrc.read(_chunk_size)
                        chunk_sha256 = hashlib.sha256(chunk_data).hexdigest()
                        storage_rows = _upload_single_blob(
                            chunk_data, chunk_sha256, backends, config,
                        )
                        if not storage_rows:
                            min_copies = 0
                        else:
                            min_copies = min(min_copies, len(storage_rows))
                        pending.append((seq, offset, chunk_data, chunk_sha256, storage_rows))
                if min_copies == 0:
                    results.append({
                        "local_path": local_path, "status": "error",
                        "error": "all backends failed for a chunk",
                    })
                    continue

                file_id = index_mod.index_file(
                    source_root, relative_path, file_sha256, file_size,
                    mtime=mtime, chunked=True, redundancy=redundancy,
                    version_seq=version_seq, version_group=manifest.version_group,
                    auto_commit=False,
                )
                for seq, offset, chunk_data, chunk_sha256, storage_rows in pending:
                    _record_chunk_upload(chunk_sha256, len(chunk_data), storage_rows,
                                         file_id, seq, offset, len(chunk_data))
                commit()

                for seq, offset, chunk_data, chunk_sha256, storage_rows in pending:
                    mc = ManifestChunk(seq=seq, blob_id=chunk_sha256, pt_offset=offset,
                                       pt_size=len(chunk_data))
                    for s in storage_rows:
                        mc.storage.append(ChunkStorageEntry(**s))
                    manifest.chunks.append(mc)
                _write_manifest_and_pointers(backends, manifest, redundancy)

                common = None
                for _, _, _, _, rows in pending:
                    names = {s["backend"] for s in rows}
                    common = names if common is None else (common & names)
                failed = [b.name for b in backends[:redundancy] if b.name not in (common or set())]
                status = "ok" if not failed else "partial"
                results.append({
                    "file_id": file_id, "sha256": file_sha256, "status": status,
                    "file_uuid": manifest.file_uuid, "failed_backends": failed,
                    "local_path": local_path,
                })
        except Exception as e:
            print(f"Error processing {file_info.get('local_path')}: {e}", file=sys.stderr)
            results.append({"local_path": file_info.get("local_path"),
                            "status": "error", "error": str(e)})

    _flush_batch()
    bump_generation()
    commit()
    return results


def upload_file(
    source_root: str,
    relative_path: str,
    local_path: str,
    backends: list,
    config,
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
    return results[0] if results else {"status": "error", "error": "upload failed"}

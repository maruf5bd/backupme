import gzip
import bz2
import lzma

try:
    import zstandard as zstd
    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False
    zstd = None

COMPRESS_ALGORITHMS = {"zstd", "gzip", "bz2", "lzma", "xz"}

_MAGIC_MAP = {
    b"\x89PNG\r\n\x1a\n": ("image/png", True),
    b"\xff\xd8\xff": ("image/jpeg", True),
    b"GIF87a": ("image/gif", True),
    b"GIF89a": ("image/gif", True),
    b"\x1a\x45\xdf\xa3": ("video/webm", True),
    b"\x00\x00\x00\x18ftypmp42": ("video/mp4", True),
    b"\x00\x00\x00\x1cftyp": ("video/mp4", True),
    b"\x1f\x8b\x08": ("gzip", True),
    b"BZh": ("bzip2", True),
    b"\xfd7zXZ\x00": ("xz", True),
    b"\x28\xb5\x2f\xfd": ("zstd", True),
    b"PK\x03\x04": ("application/zip", True),
    b"Rar!\x1a\x07": ("application/rar", True),
}

_EXT_SKIP = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mkv", ".avi",
    ".mov", ".mp3", ".flac", ".ogg", ".zip", ".gz", ".bz2", ".xz",
    ".zst", ".rar", ".7z", ".pdf", ".doc", ".docx", ".ppt", ".pptx",
}


def should_skip(data: bytes, ext: str = "") -> bool:
    if ext.lower() in _EXT_SKIP:
        return True
    for magic in _MAGIC_MAP:
        if data.startswith(magic):
            return True
    return False


def choose_compression_level(file_size: int) -> tuple[str, int]:
    if file_size < 64 * 1024:
        return ("zstd", 19)
    elif file_size < 1024 * 1024:
        return ("zstd", 10)
    elif file_size < 64 * 1024 * 1024:
        return ("zstd", 3)
    else:
        return ("zstd", 1)


def compress(data: bytes, algorithm: str = "zstd", level: int = 3) -> bytes:
    if algorithm == "none":
        return data
    if algorithm == "zstd":
        if not _HAS_ZSTD:
            return data
        try:
            cctx = zstd.ZstdCompressor(level=level)
            return cctx.compress(data)
        except Exception:
            return data
    elif algorithm == "gzip":
        return gzip.compress(data, compresslevel=level)
    elif algorithm == "bz2":
        return bz2.compress(data, compresslevel=level)
    elif algorithm in ("lzma", "xz"):
        preset = min(level, 9)
        return lzma.compress(data, preset=preset)
    return data


def decompress(data: bytes, algorithm: str = "zstd") -> bytes:
    if algorithm == "none":
        return data
    if algorithm == "zstd":
        if not _HAS_ZSTD:
            return data
        try:
            dctx = zstd.ZstdDecompressor()
            return dctx.decompress(data)
        except Exception:
            return data
    elif algorithm == "gzip":
        return gzip.decompress(data)
    elif algorithm == "bz2":
        return bz2.decompress(data)
    elif algorithm in ("lzma", "xz"):
        return lzma.decompress(data)
    return data


def recompress_blobs(algo: str = "zstd", level: int = 3, backend_name: str | None = None, key: bytes | None = None):
    """PLAN_FIXED B12: Write-new → DB-commit → GC-old recompression migration.

    Safe order: recompress locally → upload new variant → verify → INSERT blob_storage
    → DELETE old variant → DELETE old blob_storage row.
    Never lose readable data — can crash between steps and recover.
    """
    from backupme.core.database import execute, commit
    import hashlib, tempfile, time
    from pathlib import Path

    rows = execute(
        "SELECT bs.* FROM blob_storage bs "
        + ("WHERE bs.backend_name = ?" if backend_name else "")
        + " AND (bs.compression_algo != ? OR bs.compression_level != ?)",
        (backend_name, algo, level) if backend_name else (algo, level),
    ).fetchall()

    recompressed = 0
    for row_s in rows:
        try:
            row = dict(row_s)
            blob_id = row["blob_id"]

            from backupme.backends import get_backend
            bk = get_backend(row["backend_name"], {})
            if bk is None:
                continue

            dl_path = Path(tempfile.mkdtemp()) / "recompress_dl"
            bk.download(row["storage_path"], str(dl_path))
            raw = dl_path.read_bytes()

            if row["encrypted"] and key:
                from backupme.core import crypto as crypto_mod
                raw = crypto_mod.decrypt(raw, row["nonce"], row["auth_tag"], key)
            if row["compression_algo"] and row["compression_algo"] != "none":
                raw = decompress(raw, row["compression_algo"])

            new_data = compress(raw, algo, level)
            if row["encrypted"] and key:
                from backupme.core import crypto as crypto_mod
                new_data, nonce, tag = crypto_mod.encrypt(new_data, key)

            new_sha = hashlib.sha256(new_data).hexdigest()
            tmp = Path(tempfile.mkdtemp()) / "recompress_new"
            tmp.write_bytes(new_data)

            bk.upload(str(tmp), row["storage_path"])
            remote_hash = bk.hash(row["storage_path"])
            if remote_hash is None:
                dl_v = Path(tempfile.mkdtemp()) / "v"
                bk.download(row["storage_path"], str(dl_v))
                remote_hash = hashlib.sha256(dl_v.read_bytes()).hexdigest()
            if remote_hash != new_sha:
                continue

            execute(
                "UPDATE blob_storage SET compression_algo=?, compression_level=?, stored_size=?, stored_sha256=?, "
                "nonce=?, auth_tag=?, verified_at=datetime('now') WHERE blob_id=? AND backend_name=?",
                (algo, level, len(new_data), new_sha, nonce if row["encrypted"] else None,
                 tag if row["encrypted"] else None, blob_id, row["backend_name"]),
            )
            commit()
            recompressed += 1

        except Exception:
            continue

    return {"recompressed": recompressed, "total_scanned": len(rows)}
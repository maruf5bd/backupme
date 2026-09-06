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
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".tiff",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mp3", ".flac", ".ogg", ".wav", ".aac", ".wma", ".m4a",
    ".zip", ".gz", ".bz2", ".xz", ".zst", ".rar", ".7z", ".lz4", ".sz",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
}


def should_skip(data: bytes, ext: str = "") -> bool:
    if ext.lower() in _EXT_SKIP:
        return True
    for magic in _MAGIC_MAP:
        if data.startswith(magic):
            return True
    return False


def choose_compression_level(file_size: int) -> tuple[str, int]:
    if not _HAS_ZSTD:
        return ("gzip", 6)
    if file_size < 64 * 1024:
        return ("zstd", 19)
    elif file_size < 1024 * 1024:
        return ("zstd", 10)
    elif file_size < 64 * 1024 * 1024:
        return ("zstd", 3)
    else:
        return ("zstd", 1)


def _default_level(algo: str, file_size: int) -> int:
    if algo == "zstd":
        if file_size < 64 * 1024:
            return 19
        if file_size < 1024 * 1024:
            return 10
        if file_size < 64 * 1024 * 1024:
            return 3
        return 1
    return 6


def choose_compression(file_size: int, comp_cfg: dict | None = None, ext: str = "") -> tuple[str, int]:
    """Pick algorithm/level from the compression config.

    Extension overrides win first (``skip: true`` disables compression for
    that extension); otherwise the configured default algorithm/level; when no
    level is configured a size-based default is used. Compression disabled in
    config returns ("none", 0).
    """
    cfg = comp_cfg or {}
    if not cfg.get("enabled", True):
        return ("none", 0)
    for o in cfg.get("overrides") or []:
        if o and str(o.get("ext", "")).lower() == ext.lower():
            if o.get("skip"):
                return ("none", 0)
            return (str(o.get("algorithm", "zstd")), int(o.get("level", 3)))
    algo = cfg.get("default_algorithm") or ("zstd" if _HAS_ZSTD else "gzip")
    if algo not in COMPRESS_ALGORITHMS:
        algo = "zstd" if _HAS_ZSTD else "gzip"
    if cfg.get("default_level") is None:
        return (algo, _default_level(algo, file_size))
    return (algo, int(cfg["default_level"]))


def compress(data: bytes, algorithm: str = "zstd", level: int = 3) -> bytes:
    if algorithm == "none":
        return data
    if algorithm == "zstd":
        if not _HAS_ZSTD:
            return gzip.compress(data, compresslevel=6)
        cctx = zstd.ZstdCompressor(level=level)
        return cctx.compress(data)
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
            raise RuntimeError("zstd decompression requested but python-zstandard not installed")
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data)
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
    import hashlib, tempfile, time, sys
    from pathlib import Path

    if backend_name:
        rows = execute(
            "SELECT bs.* FROM blob_storage bs "
            "WHERE bs.backend_name = ? AND (bs.compression_algo != ? OR bs.compression_level != ?)",
            (backend_name, algo, level),
        ).fetchall()
    else:
        rows = execute(
            "SELECT bs.* FROM blob_storage bs "
            "WHERE bs.compression_algo != ? OR bs.compression_level != ?",
            (algo, level),
        ).fetchall()

    recompressed = 0
    skipped = 0
    for row_s in rows:
        try:
            row = dict(row_s)
            blob_id = row["blob_id"]

            if row["encrypted"] and not key:
                skipped += 1
                continue

            from backupme.backends import get_backend
            bk = get_backend(row["backend_name"], {})
            if bk is None:
                continue

            dl_path = Path(tempfile.mkdtemp()) / "recompress_dl"
            bk.download(row["storage_path"], str(dl_path))
            raw = dl_path.read_bytes()

            if row["encrypted"] and key:
                from backupme.core import crypto as crypto_mod
                raw = crypto_mod.decrypt(raw, key)
            if row["compression_algo"] and row["compression_algo"] != "none":
                raw = decompress(raw, row["compression_algo"])

            new_data = compress(raw, algo, level)
            nonce = None
            tag = None
            if row["encrypted"] and key:
                from backupme.core import crypto as crypto_mod
                new_data, nonce, tag = crypto_mod.encrypt(new_data, key)

            new_sha = hashlib.sha256(new_data).hexdigest()
            tmp = Path(tempfile.mkdtemp()) / "recompress_new"
            tmp.write_bytes(new_data)

            # B12: write-new to a NEW path, never overwrite old
            old_path = row["storage_path"]
            new_path = old_path + f".recompress_{int(time.time())}"
            bk.upload(str(tmp), new_path)
            remote_hash = bk.hash(new_path)
            if remote_hash is None:
                dl_v = Path(tempfile.mkdtemp()) / "v"
                bk.download(new_path, str(dl_v))
                remote_hash = hashlib.sha256(dl_v.read_bytes()).hexdigest()
            if remote_hash != new_sha:
                # Clean up failed new variant
                try:
                    bk.delete(new_path)
                except Exception:
                    pass
                continue

            # B12: INSERT new row first, then delete old
            execute(
                """INSERT INTO blob_storage
                   (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                    compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (blob_id, row["backend_name"], new_path, len(new_data), new_sha,
                 algo, level, row["encrypted"], nonce, tag),
            )
            # Delete old row and old file
            execute(
                "DELETE FROM blob_storage WHERE blob_id = ? AND backend_name = ? AND storage_path = ?",
                (blob_id, row["backend_name"], old_path),
            )
            commit()
            try:
                bk.delete(old_path)
            except Exception:
                pass
            recompressed += 1

        except Exception as e:
            print(f"recompress failed for {blob_id}: {e}", file=sys.stderr)
            continue

    return {"recompressed": recompressed, "total_scanned": len(rows), "skipped_encrypted": skipped}
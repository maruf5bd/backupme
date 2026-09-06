import hashlib
import os
import sys

def _long_path(p: str) -> str:
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: str, chunk_size: int = 64 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(_long_path(path), "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def sha256_chunked(data: bytes) -> str:
    return sha256(data)
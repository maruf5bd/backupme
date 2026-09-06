from pathlib import Path

from backupme.config import load_config
from backupme.core.uploader import upload_bundle
from backupme.core.downloader import download_file
from backupme.backends import invalidate_cache


def load_cfg():
    return load_config()


def backup_bytes(root: Path, rel: str, data: bytes, backends, redundancy: int = 1) -> dict:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    results = upload_bundle(
        [{"local_path": str(p), "source_root": str(root), "relative_path": rel}],
        backends, load_cfg(), redundancy,
    )
    return results[0]


def restore_bytes(file_id: int, root: Path, backends=None) -> bytes:
    dest = Path(root) / "restore_out" / f"file_{file_id}"
    if dest.exists():
        dest.unlink()
    result = download_file(file_id, str(dest))
    if result.get("status") == "error":
        raise RuntimeError(f"download failed: {result}")
    return dest.read_bytes()


def add_backend(root: Path, name: str, path: Path):
    """Add a second local backend to the scratch config (inside backends.local)."""
    cfg_path = Path(root) / "config.yaml"
    lines = cfg_path.read_text().splitlines(keepends=True)
    entry = [
        f"  - name: {name}\n",
        f"    path: {path.as_posix()}\n",
        "    priority: 90\n",
    ]
    idx = next(i for i, l in enumerate(lines) if l.startswith("encryption:"))
    lines[idx:idx] = entry
    cfg_path.write_text("".join(lines))
    invalidate_cache()

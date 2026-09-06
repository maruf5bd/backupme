import hashlib
import os
import tempfile
import time
import uuid
from pathlib import Path

from backupme.core import hasher as hasher_mod
from backupme.core import compress as compress_mod
from backupme.core import crypto as crypto_mod
from backupme.core import dedup as dedup_mod
from backupme.core import index as index_mod
from backupme.core import state as state_mod
from backupme.core import uploader as uploader_mod
from backupme.core.database import execute, commit, bump_generation
from backupme.core.manifest import Manifest, ManifestChunk, ChunkStorageEntry, write_manifest
from backupme.backends import get_backend, get_all_backends

CHECKPOINT_FILE = "converter_checkpoint.json"
_CHECKPOINT_INTERVAL = 50


def _load_checkpoint() -> dict:
    p = Path(CHECKPOINT_FILE)
    if p.exists():
        import json
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"completed": [], "current": 0}


def _save_checkpoint(state: dict):
    import json
    tmp = Path(CHECKPOINT_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(CHECKPOINT_FILE)


def _is_already_indexed(relative_path: str, source_root: str) -> bool:
    row = execute(
        "SELECT 1 FROM files WHERE relative_path = ? AND source_root = ? AND deleted_at IS NULL LIMIT 1",
        (relative_path, source_root),
    ).fetchone()
    return row is not None


def _redudancy_met(blob_ids: list[str], min_copies: int = 1) -> bool:
    """PLAN_FIXED B13: require N replicated copies before delete."""
    for blob_id in blob_ids:
        row = execute(
            "SELECT COUNT(*) AS cnt FROM blob_storage WHERE blob_id = ? AND verified_at IS NOT NULL",
            (blob_id,),
        ).fetchone()
        if not row or row[0] < min_copies:
            return False
    return True


def _manifest_replicated(uuid_str: str, min_copies: int = 1) -> bool:
    path = f"manifests/{uuid_str[:2]}/{uuid_str[2:4]}/{uuid_str}.manifest.json"
    from backupme.config import load_config
    config = load_config()
    count = 0
    for be in get_all_backends(config):
        if be.exists(path):
            count += 1
    return count >= min_copies


def convert_backend(
    backend_name: str,
    prefix: str = "",
    target_backend_name: str | None = None,
    delete: bool = False,
    resume: bool = False,
):
    from backupme.config import load_config
    config = load_config()

    backend = get_backend(backend_name, config)
    if backend is None:
        raise ValueError(f"Backend '{backend_name}' not found or not reachable")

    target_be = None
    if target_backend_name:
        target_be = get_backend(target_backend_name, config)
        if target_be is None:
            raise ValueError(f"Target backend '{target_backend_name}' not found")

    files = backend.walk(prefix)

    checkpoint = _load_checkpoint() if resume else {"completed": [], "current": 0}
    skip_dirs = {"__backupme_db__", "manifests", "objects", "tree"}

    completed = 0
    skipped = 0
    errors = 0

    for i, f in enumerate(files):
        rel_path = f["path"]
        top_dir = rel_path.split("/")[0] if "/" in rel_path else rel_path
        if top_dir in skip_dirs:
            continue

        if rel_path in checkpoint["completed"]:
            skipped += 1
            continue

        if _is_already_indexed(rel_path, backend_name):
            checkpoint["completed"].append(rel_path)
            skipped += 1
            continue

        try:
            tmp = Path(tempfile.mkdtemp()) / rel_path.replace("/", "_")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            backend.download(rel_path, str(tmp))

            file_sha256 = hasher_mod.sha256_file(str(tmp))
            file_size = os.path.getsize(str(tmp))

            explicit_targets = [target_be] if target_be else [backend]

            result = uploader_mod.upload_file(
                source_root=backend_name,
                relative_path=rel_path,
                local_path=str(tmp),
                backends=explicit_targets,
                config=config,
                redundancy=1,
            )

            if delete:
                blob_ids = [file_sha256]
                if result.get("chunked"):
                    chunks = execute(
                        "SELECT blob_id FROM file_chunks WHERE file_id = ?", (result["file_id"],)
                    ).fetchall()
                    blob_ids = [c[0] for c in chunks]

                if _redudancy_met(blob_ids, min_copies=1):
                    if result.get("chunked"):
                        actual_uuid = result.get("file_uuid", file_sha256[:32])
                        import re
                        m = re.match(r'[a-f0-9]{32}', actual_uuid)
                        if m and _manifest_replicated(m.group(), min_copies=1):
                            backend.delete(rel_path)
                    else:
                        backend.delete(rel_path)

            tmp.unlink(missing_ok=True)
            completed += 1

            if completed % _CHECKPOINT_INTERVAL == 0:
                checkpoint["completed"].append(rel_path)
                _save_checkpoint(checkpoint)
                state_mod.save_checkpoint("convert_progress", {
                    "completed": completed, "errors": errors, "current": rel_path,
                })

        except Exception as e:
            errors += 1
            import traceback
            traceback.print_exc()
            continue

    _save_checkpoint(checkpoint)
    return {"completed": completed, "skipped": skipped, "errors": errors}
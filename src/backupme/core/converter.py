import hashlib
import os
import sys
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
    return state_mod.read_json_checkpoint(
        CHECKPOINT_FILE, lambda: {"completed": [], "current": 0}
    )


def _save_checkpoint(state: dict):
    state_mod.write_json_checkpoint(CHECKPOINT_FILE, state)


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
    progress_cb=None,
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
    # 'bundle' MUST be skipped: a backend that holds BackupMe objects has its
    # bundle store under bundle/<2>/<2>/<sha> — without this filter convert
    # re-ingests its own backup artifacts as fake user files (probe-proven).
    skip_dirs = {"__backupme_db__", "manifests", "objects", "tree", "bundle"}

    completed = 0
    skipped = 0
    errors = 0

    total = 0
    for f in files:
        top_dir = f["path"].split("/")[0] if "/" in f["path"] else f["path"]
        if top_dir not in skip_dirs:
            total += 1

    def _report_progress():
        if progress_cb is not None:
            try:
                progress_cb(completed, skipped, errors, total)
            except Exception:
                pass

    _report_progress()

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

            if delete and result.get("file_id"):
                # The delete gate must check the blob ids the file ACTUALLY
                # landed on (file_chunks), not the file sha — bundled small
                # files live inside bundle/member blobs whose ids differ from
                # the file sha, so the old file-sha check never matched and
                # sources were never deleted.
                chunks = execute(
                    "SELECT blob_id FROM file_chunks WHERE file_id = ?", (result["file_id"],)
                ).fetchall()
                blob_ids = [c[0] for c in chunks] or [file_sha256]

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
            _report_progress()
            continue

        _report_progress()

    _save_checkpoint(checkpoint)
    return {"completed": completed, "skipped": skipped, "errors": errors, "total": total}
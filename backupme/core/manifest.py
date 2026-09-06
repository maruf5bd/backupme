import json
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from backupme.core.database import execute, executemany, commit


MANIFEST_DIR = "manifests"


@dataclass
class ChunkStorageEntry:
    backend: str
    path: str
    stored_size: int
    stored_sha256: str
    algo: str | None = None
    level: int | None = None
    encrypted: bool = False
    nonce: str | None = None
    auth_tag: str | None = None


@dataclass
class ManifestChunk:
    seq: int
    blob_id: str
    pt_offset: int
    pt_size: int
    storage: list[ChunkStorageEntry] = field(default_factory=list)


@dataclass
class Manifest:
    v: int = 1
    file_uuid: str = field(default_factory=lambda: uuid.uuid4().hex)
    source_root: str = ""
    relative_path: str = ""
    sha256: str = ""
    size: int = 0
    mtime: float = 0.0
    version_group: str = ""
    version_seq: int = 1
    redundancy: int = 1
    chunks: list[ManifestChunk] = field(default_factory=list)


def _manifest_storage_path(manifest: Manifest) -> str:
    b0 = manifest.file_uuid[:2]
    b1 = manifest.file_uuid[2:4]
    return f"{MANIFEST_DIR}/{b0}/{b1}/{manifest.file_uuid}.manifest.json"


def write_manifest(backend, manifest: Manifest) -> str:
    data = json.dumps(asdict(manifest), indent=2)
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "manifest.json"
    tmp.write_text(data)
    try:
        remote_path = _manifest_storage_path(manifest)
        backend.upload(str(tmp), remote_path)
        return remote_path
    finally:
        tmp.unlink(missing_ok=True)


def read_manifest(backend, manifest_path: str) -> Manifest | None:
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "manifest.json"
    try:
        backend.download(manifest_path, str(tmp))
        data = json.loads(tmp.read_text())
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)

    manifest = Manifest(
        v=data.get("v", 1),
        file_uuid=data.get("file_uuid", ""),
        source_root=data.get("source_root", ""),
        relative_path=data.get("relative_path", ""),
        sha256=data.get("sha256", ""),
        size=data.get("size", 0),
        mtime=data.get("mtime", 0.0),
        version_group=data.get("version_group", ""),
        version_seq=data.get("version_seq", 1),
        redundancy=data.get("redundancy", 1),
    )
    for c in data.get("chunks", []):
        chunk = ManifestChunk(
            seq=c["seq"],
            blob_id=c["blob_id"],
            pt_offset=c["pt_offset"],
            pt_size=c["pt_size"],
        )
        for s in c.get("storage", []):
            chunk.storage.append(ChunkStorageEntry(**s))
        manifest.chunks.append(chunk)
    return manifest


def rebuild_database_from_manifests(backends: list) -> dict:
    from backupme.core.schema import SQL_SCHEMA, SQL_INDEXES

    seen_files: dict[str, Manifest] = {}

    for bk in backends:
        try:
            files = bk.walk(MANIFEST_DIR)
        except Exception:
            continue
        for f in files:
            manifest = read_manifest(bk, f["path"])
            if manifest is None:
                continue
            key = f"{manifest.source_root}:{manifest.relative_path}"
            existing = seen_files.get(key)
            if existing is None or manifest.version_seq > existing.version_seq:
                seen_files[key] = manifest

    for stmt in SQL_SCHEMA.strip().split(";"):
        s = stmt.strip()
        if s and not s.upper().startswith("PRAGMA") and not s.upper().startswith("CREATE VIEW"):
            try:
                execute(s + ";")
            except Exception:
                pass
    for stmt in SQL_INDEXES.strip().split(";"):
        s = stmt.strip()
        if s:
            execute(s + ";")

    for manifest in seen_files.values():
        existing = execute(
            "SELECT id FROM files WHERE source_root = ? AND relative_path = ? AND version_seq = ? AND deleted_at IS NULL",
            (manifest.source_root, manifest.relative_path, manifest.version_seq),
        ).fetchone()
        if existing:
            file_id = existing[0]
            execute(
                "UPDATE files SET sha256 = ?, size = ?, mtime = ?, chunked = ?, redundancy = ?, updated_at = datetime('now') WHERE id = ?",
                (manifest.sha256, manifest.size, manifest.mtime,
                 1 if len(manifest.chunks) > 1 else 0, manifest.redundancy, file_id),
            )
            execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))
        else:
            execute(
                """INSERT INTO files
                   (source_root, relative_path, sha256, size, mtime, chunked, redundancy, version_group, version_seq)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    manifest.source_root, manifest.relative_path,
                    manifest.sha256, manifest.size, manifest.mtime,
                    1 if len(manifest.chunks) > 1 else 0,
                    manifest.redundancy, manifest.version_group, manifest.version_seq,
                ),
            )
            file_id = execute("SELECT last_insert_rowid()").fetchone()[0]

        for chunk in manifest.chunks:
            execute(
                "INSERT OR IGNORE INTO blobs (blob_id, pt_size) VALUES (?, ?)",
                (chunk.blob_id, chunk.pt_size),
            )
            execute(
                "INSERT OR IGNORE INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
                (file_id, chunk.seq, chunk.blob_id, chunk.pt_offset, chunk.pt_size),
            )
            for s in chunk.storage:
                execute(
                    """INSERT OR IGNORE INTO blob_storage
                       (blob_id, backend_name, storage_path, stored_size, stored_sha256,
                        compression_algo, compression_level, encrypted)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (chunk.blob_id, s.backend, s.path, s.stored_size, s.stored_sha256,
                     s.algo, s.level, 1 if s.encrypted else 0),
                )

    execute("UPDATE blobs SET ref_count = (SELECT COUNT(*) FROM file_chunks WHERE file_chunks.blob_id = blobs.blob_id)")
    commit()

    return {"rebuilding": "ok", "files_rebuilt": len(seen_files)}
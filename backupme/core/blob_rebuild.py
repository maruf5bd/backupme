"""Rebuild physically-missing blobs from verified restore artifacts (D14).

Sources of truth, in priority order:
1. Orphan stores: roots on disk that no configured backend points at
   (e.g. a merged store left outside the config). If a referenced blob's
   stored object exists there byte-identical (sha256 == a recorded
   stored_sha256), copy it to the target backend as-is.
2. The owner's sha-verified restore tree (restore_and_prune state.json
   'ok' map): a missing blob's plaintext is a recorded
   (pt_offset, pt_size) slice of a restored file. The slice is verified
   against the blob_id tail (plaintext sha256) BEFORE anything is stored.

Rules:
- Never mutate or delete existing rows; only INSERT OR IGNORE new
  blob_storage rows on the target backend.
- Byte-exact reconstruction is attempted whenever row metadata
  (compression algo/level + stored_sha256) exists; if the recompressed
  bytes differ (zstd version drift), the new object is still valid
  (restore decompresses per-row metadata and verifies plaintext sha) and
  gets its own row with the new stored_sha256.
- Plaintext sha256 must ALWAYS equal the blob_id tail; anything else is a
  hard failure recorded per blob, never a silent substitution.
- The tool never reads the restore tree destructively; state.json is
  read-only; no file is ever written outside the target backend.
"""
import hashlib
import tempfile
from pathlib import Path

from backupme.core import compress as compress_mod
from backupme.core.database import commit, execute
from backupme.core.op_log import append as op_append
from backupme.core.op_log import mark_committed
from backupme.log import get_logger

log = get_logger("blob_rebuild")

_RAW_MAX = 256  # mirrors uploader._no_compress_max


def _blob_tail(blob_id: str) -> str:
    return blob_id.rsplit("/", 1)[-1]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _classify(config, extra_roots: list[Path]):
    """Return (refs, by_blob, configured_roots, avail_map, missing_ids).

    avail_map[blob_id] = set of root paths that physically hold a
    non-member storage row for the blob, classified by kind:
    'configured' or 'orphan'.
    """
    refs = {}
    for r in execute("SELECT blob_id, pt_size FROM blobs WHERE ref_count > 0").fetchall():
        refs[r["blob_id"]] = r["pt_size"]

    by_blob = {}
    for r in execute(
        "SELECT blob_id, backend_name, storage_path, stored_size, stored_sha256, "
        "compression_algo, compression_level, encrypted, nonce, auth_tag "
        "FROM blob_storage"
    ).fetchall():
        if "@" in r["storage_path"]:
            continue  # member rows ride their parent object
        by_blob.setdefault(r["blob_id"], []).append(dict(r))

    configured_roots = {}
    locals_map = (config.backends or {}).get("local", [])
    if isinstance(locals_map, dict):
        locals_map = [locals_map]
    for c in locals_map:
        name = getattr(c, "name", None) or c.get("name")
        path = getattr(c, "path", None) or c.get("path")
        if path:
            configured_roots[name] = Path(path)

    cache = {}

    def exists_at(root: Path, rel: str) -> bool:
        key = (str(root), rel)
        if key not in cache:
            cache[key] = (root / rel).is_file()
        return cache[key]

    parent_avail: dict[str, bool] = {}

    def parent_available(parent_id: str) -> bool:
        if parent_id in parent_avail:
            return parent_avail[parent_id]
        ok = False
        for row in by_blob.get(parent_id, []):  # parents have non-member rows
            for name, root in configured_roots.items():
                if exists_at(root, row["storage_path"]):
                    ok = True
                    break
            if ok:
                break
            for root in extra_roots:
                if exists_at(root, row["storage_path"]):
                    ok = True
                    break
            if ok:
                break
        parent_avail[parent_id] = ok
        return ok

    avail = {}
    for blob_id, rows in by_blob.items():
        for row in rows:
            for name, root in configured_roots.items():
                if exists_at(root, row["storage_path"]):
                    avail.setdefault(blob_id, set()).add(("configured", str(root), row["storage_path"]))
            for root in extra_roots:
                if exists_at(root, row["storage_path"]):
                    avail.setdefault(blob_id, set()).add(("orphan", str(root), row["storage_path"]))

    # bundle MEMBER blobs have only '@' rows — they are available when their
    # parent tar object is available on any root (copying the parent carries
    # its members). Plain blob ids resolve to themselves, so the parent-id
    # inequality below only matches member-style ids.
    missing = []
    for blob_id in refs:
        if blob_id in avail:
            continue
        if blob_id not in by_blob:
            parent_id = blob_id.rsplit("/", 1)[0]
            if parent_id != blob_id and parent_available(parent_id):
                continue
        missing.append(blob_id)
    return refs, by_blob, configured_roots, avail, missing


def _load_ok_map(state_path: Path) -> dict:
    import json

    state = json.loads(state_path.read_text(encoding="utf-8"))
    return state.get("ok", {})


def _chunk_cover(missing_ids: list[str], ok_map: dict) -> dict:
    """missing blob_id -> list of (file_id, pt_offset, pt_size) from ok files."""
    want = set(missing_ids)
    cover = {}
    rows = execute("SELECT file_id, blob_id, pt_offset, pt_size FROM file_chunks").fetchall()
    for r in rows:
        if r["blob_id"] in want and str(r["file_id"]) in ok_map:
            cover.setdefault(r["blob_id"], []).append(
                (str(r["file_id"]), r["pt_offset"], r["pt_size"])
            )
    return cover


def _compress_for_rebuild(data: bytes, rows: list[dict]):
    """Return (stored_bytes, algo, level). Try recorded params first for a
    byte-exact match; fall back to a deterministic default."""
    seen = set()
    for row in rows:
        key = (row.get("compression_algo"), row.get("compression_level"))
        if key in seen or key[0] in (None, "", "none"):
            continue
        seen.add(key)
        algo, level = key[0], key[1] if key[1] is not None else 3
        try:
            stored = compress_mod.compress(data, algo, int(level))
        except Exception:
            continue
        want = row.get("stored_sha256")
        if want and hashlib.sha256(stored).hexdigest().lower() == str(want).lower():
            return stored, algo, int(level)
    # deterministic default (mirrors the pipeline: tiny payloads stay raw)
    if len(data) <= _RAW_MAX:
        return data, None, None
    algo, level = compress_mod.choose_compression(len(data), {}, "")
    if algo == "none":
        return data, None, None
    return compress_mod.compress(data, algo, level), algo, level


def _remote_path(blob_id: str, algo) -> str:
    tail = _blob_tail(blob_id)
    rel = f"objects/{tail[:2]}/{tail[2:4]}/{tail}"
    if algo:
        rel += f".{algo}"
    return rel


def rebuild_blobs(
    config,
    restore_state_path: str,
    extra_roots: list[str] | None = None,
    target_backend_name: str = "local_disk",
    limit: int | None = None,
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    """Rebuild missing blobs; returns an honest report dict."""
    from backupme.backends import get_backend

    extra = [Path(p) for p in (extra_roots or [])]
    refs, by_blob, _cfg_roots, avail, missing = _classify(config, extra)

    report = {
        "status": "ok",
        "referenced": len(refs),
        "available": len(refs) - len(missing),
        "available_orphan_only": 0,
        "missing": len(missing),
        "orphan_copied": 0,
        "rebuilt": 0,
        "failed": [],
        "skipped_encrypted": 0,
        "unrecoverable": 0,
        "bytes_written": 0,
        "dry_run": bool(dry_run),
    }

    # --- leg 1: orphan-copy (object exists outside configured roots) -------
    orphan_ids = [b for b in refs if b in avail and all(
        kind == "orphan" for kind, _r, _p in avail[b]
    )]
    report["available_orphan_only"] = len(orphan_ids)

    # --- leg 2: rebuild from the verified restore tree ---------------------
    ok_map = _load_ok_map(Path(restore_state_path))
    cover = _chunk_cover(missing, ok_map)
    rebuild_ids = list(cover.keys())
    report["unrecoverable"] = len(missing) - len(rebuild_ids)

    todo: list[tuple[str, str]] = []
    for blob_id in orphan_ids:
        todo.append(("copy", blob_id))
    for blob_id in rebuild_ids:
        todo.append(("rebuild", blob_id))
    if limit is not None:
        todo = todo[:limit]

    if dry_run:
        report["planned_copies"] = len(orphan_ids)
        report["planned_rebuilds"] = len(rebuild_ids)
        return report

    target = get_backend(target_backend_name, config)
    if target is None:
        report["status"] = "error"
        report["error"] = f"target backend {target_backend_name!r} not found"
        return report

    op_row = op_append(
        "blob_rebuild",
        {"target": target_backend_name, "restore_state": str(restore_state_path)},
        idempotency_key=f"blob_rebuild:{target_backend_name}:{Path(restore_state_path).stat().st_mtime_ns}",
    )

    ok_map_by_id = ok_map  # file_id str -> {path, sha256, size}

    def _upload_bytes(stored: bytes, remote_rel: str) -> tuple[bool, str]:
        tmp = Path(tempfile.mkdtemp()) / "rebuild"
        tmp.write_bytes(stored)
        try:
            got = target.upload(str(tmp), remote_rel)
            return got.lower() == hashlib.sha256(stored).hexdigest().lower(), got
        finally:
            tmp.unlink(missing_ok=True)

    for i, (kind, blob_id) in enumerate(todo, 1):
        try:
            tail = _blob_tail(blob_id)
            rows = by_blob.get(blob_id, [])
            if any(r.get("encrypted") for r in rows):
                report["skipped_encrypted"] += 1
                report["failed"].append({"blob_id": blob_id, "error": "encrypted blob — not rebuildable here"})
                continue

            if kind == "copy":
                src_entry = next(iter(avail[blob_id]))
                src_path = Path(src_entry[1]) / src_entry[2]
                stored = src_path.read_bytes()
                matching = next(
                    (r for r in rows
                     if r["stored_sha256"] and r["stored_sha256"].lower() == hashlib.sha256(stored).hexdigest().lower()),
                    None,
                )
                if matching is None:
                    report["failed"].append({"blob_id": blob_id, "error": "orphan bytes do not match any stored_sha256"})
                    continue
                remote_rel = matching["storage_path"]
                ok, got = _upload_bytes(stored, remote_rel)
                if not ok:
                    report["failed"].append({"blob_id": blob_id, "error": f"upload hash mismatch got {got[:12]}"})
                    continue
                execute(
                    "INSERT OR IGNORE INTO blob_storage "
                    "(blob_id, backend_name, storage_path, stored_size, stored_sha256, "
                    " compression_algo, compression_level, encrypted, nonce, auth_tag, verified_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    (blob_id, target.name, remote_rel, len(stored), matching["stored_sha256"],
                     matching["compression_algo"], matching["compression_level"],
                     0, matching["nonce"], matching["auth_tag"]),
                )
                report["orphan_copied"] += 1
                report["bytes_written"] += len(stored)
            else:
                file_id, pt_offset, pt_size = cover[blob_id][0]
                info = ok_map_by_id.get(file_id)
                src_file = Path(info["path"]) if info else None
                if src_file is None or not src_file.is_file():
                    report["failed"].append({"blob_id": blob_id, "error": f"restored file missing for file_id {file_id}"})
                    continue
                with open(src_file, "rb") as f:
                    f.seek(pt_offset)
                    plaintext = f.read(pt_size)
                if len(plaintext) != pt_size:
                    report["failed"].append({"blob_id": blob_id, "error": f"restored file shorter than chunk map (file_id {file_id})"})
                    continue
                if hashlib.sha256(plaintext).hexdigest().lower() != tail.lower():
                    report["failed"].append({"blob_id": blob_id, "error": "plaintext sha does not match blob_id tail"})
                    continue
                stored, algo, level = _compress_for_rebuild(plaintext, rows)
                remote_rel = _remote_path(blob_id, algo)
                ok, got = _upload_bytes(stored, remote_rel)
                if not ok:
                    report["failed"].append({"blob_id": blob_id, "error": f"upload hash mismatch got {got[:12]}"})
                    continue
                execute(
                    "INSERT OR IGNORE INTO blob_storage "
                    "(blob_id, backend_name, storage_path, stored_size, stored_sha256, "
                    " compression_algo, compression_level, encrypted, verified_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))",
                    (blob_id, target.name, remote_rel, len(stored),
                     hashlib.sha256(stored).hexdigest(), algo, level),
                )
                report["rebuilt"] += 1
                report["bytes_written"] += len(stored)
        except Exception as e:  # per-blob isolation, keep going
            report["failed"].append({"blob_id": blob_id, "error": f"{type(e).__name__}: {e}"})
        if progress_cb and i % 25 == 0:
            progress_cb(i, len(todo))

    if report["failed"] or report["unrecoverable"]:
        report["status"] = "partial"
    commit()
    try:
        mark_committed(op_row["id"])
    except Exception:
        log.warning("op_log mark_committed failed for op %s", op_row.get("id"))
    log.info("blob_rebuild done: rebuilt=%s copied=%s failed=%s unrecoverable=%s",
             report["rebuilt"], report["orphan_copied"], len(report["failed"]),
             report["unrecoverable"])
    return report

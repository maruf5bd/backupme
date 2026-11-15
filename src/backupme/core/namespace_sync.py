"""Namespace sync: replicate BackupMe's restore-map namespaces from local
backend roots onto a target backend.

The restore-map is what makes an independent restore possible without the
original machine: manifest receipts (``manifests/``), tree pointers
(``tree/``) and DB snapshots + WAL bundles (``__backupme_db__/``).  This
module copies those three namespaces from every local backend root to a
single target backend (typically the SFTP Storage Box) so the target alone
can drive a restore.

Design rules:
- Purely additive: never deletes anything, on sources or target.
- Idempotent: files already present on the target are skipped (presence
  check, not size/hash — content-addressed receipts never change under a
  given rel path).
- Never opens the database. This tool reads local files and talks to one
  target backend, nothing else.
- Partial-failure tolerant: every file is wrapped in its own error handling;
  one unreadable file never aborts the run.
- Dedupe across sources: first source wins for a given rel path. Same-size
  duplicates across roots are the expected content-addressed case; a size
  disagreement is recorded as a conflict (first source still wins).
"""
import hashlib
from pathlib import Path

from ..log import get_logger

log = get_logger("namespace_sync")

#: Namespaces copied when the caller does not restrict them.
DEFAULT_NAMESPACES = ("manifests", "tree", "__backupme_db__")

#: Single-file cap. Guard/snapshot .zst files are ~177 MB and wal bundles are
#: small; this cap is a safety net against runaway files, not a tuning knob.
MAX_FILE_BYTES = 512 * 1024 * 1024

#: Maximum number of conflict examples kept in the result payload.
_CONFLICT_EXAMPLES = 5


def _sha256_file(path: Path) -> str:
    """Stream a local file through sha256 (1 MiB chunks)."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def _collect(root: Path, namespace: str) -> list[tuple[str, Path, int | None]]:
    """List candidate files for one source root + namespace.

    Returns (rel_path, local_path, size) with rel_path in the remote layout
    ``<namespace>/<subpath>``. A missing namespace dir simply yields [].
    """
    ns_dir = Path(root) / namespace
    if not ns_dir.is_dir():
        log.info("namespace %s not present on source root %s", namespace, root)
        return []
    found = []
    for p in sorted(ns_dir.rglob("*")):
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError as e:
            log.warning("cannot stat %s: %s", p, e)
            size = None
        rel = f"{namespace}/{p.relative_to(ns_dir).as_posix()}"
        found.append((rel, p, size))
    return found


def _remote_listing(target_backend, namespace: str) -> dict[str, int] | None:
    """Recursive rel-path -> size map for one namespace on the target.

    Returns None when the listing is unavailable or reported partial; callers
    then fall back to per-file exists() checks (walk_errors > 0 on remote
    backends means the recursive walk silently skipped directories).
    """
    try:
        entries = target_backend.walk(prefix=namespace)
    except Exception as e:
        log.warning("walk(%s) failed on backend %s: %s",
                    namespace, getattr(target_backend, "name", "?"), e)
        return None
    walk_errors = getattr(target_backend, "walk_errors", 0)
    if walk_errors:
        log.warning("walk(%s) on backend %s reported %s error(s); "
                    "falling back to per-file checks",
                    namespace, getattr(target_backend, "name", "?"), walk_errors)
        return None
    return {e["path"]: int(e.get("size", 0)) for e in entries}


def sync_namespace(target_backend, source_roots: list[Path],
                   namespaces=("manifests", "tree", "__backupme_db__"),
                   dry_run: bool = False, progress_cb=None) -> dict:
    """Copy restore-map namespaces from local source roots to a target backend.

    Args:
        target_backend: a connected StorageBackend instance (the copy target).
        source_roots: local backend root directories to copy FROM, in order.
            For identical rel paths the first source wins.
        namespaces: namespace top-level dirs to copy (subset of
            DEFAULT_NAMESPACES; None/empty means all three).
        dry_run: scan and report without uploading anything.
        progress_cb: optional callable(done, total, rel_path) invoked after
            each processed file.

    Returns a dict:
        status: "ok" (everything planned is on the target) or "partial"
            (some files failed / were skipped by the size cap).
        scanned: unique files considered across all sources (after dedupe).
        uploaded / skipped_existing / bytes_uploaded: self-evident. In
            dry_run mode ``uploaded`` stays 0 and ``would_upload`` /
            ``would_upload_bytes`` carry the plan instead.
        failed: list of {"path", "source", "error"} for every file that did
            not make it (including >512 MiB cap skips).
        conflicts: {"count", "examples"} for rel paths whose sources
            disagree on size (first source wins regardless).
    """
    if target_backend is None:
        raise ValueError("sync_namespace needs a connected target backend")
    if not namespaces:
        namespaces = DEFAULT_NAMESPACES
    namespaces = tuple(namespaces)

    result: dict = {
        "status": "ok",
        "scanned": 0,
        "uploaded": 0,
        "skipped_existing": 0,
        "failed": [],
        "conflicts": {"count": 0, "examples": []},
        "bytes_uploaded": 0,
    }

    def fail(rel: str, source: str, error: str):
        result["failed"].append({"path": rel, "source": source, "error": error})
        log.warning("namespace sync: %s (%s): %s", rel, source, error)

    def unupload(rel: str, source: str, size: int, error: str):
        """Move a tentatively-successful upload into the failure list."""
        result["uploaded"] -= 1
        result["bytes_uploaded"] -= size
        fail(rel, source, error)

    # ---- Phase 1: scan sources, dedupe by rel path (first source wins) ----
    plan: dict[str, dict] = {}   # rel -> {"path", "size", "source"}
    for idx, root in enumerate(source_roots):
        label = f"source[{idx}]"
        for ns in namespaces:
            for rel, path, size in _collect(Path(root), ns):
                existing = plan.get(rel)
                if existing is None:
                    plan[rel] = {"path": path, "size": size, "source": label}
                    continue
                # Same rel path from a later source: first source wins.
                # Same-size duplicates are the expected content-addressed
                # case; a size disagreement is a conflict worth recording.
                if (size is None or existing["size"] is None
                        or size == existing["size"]):
                    continue
                result["conflicts"]["count"] += 1
                if len(result["conflicts"]["examples"]) < _CONFLICT_EXAMPLES:
                    result["conflicts"]["examples"].append(
                        f"{rel} ({existing['source']}={existing['size']} B "
                        f"vs {label}={size} B)")
                log.info("namespace sync conflict on %s: keeping %s copy",
                         rel, existing["source"])
    result["scanned"] = len(plan)
    total = len(plan)

    # ---- Phase 2: per-file copy with idempotent skip ----
    existing_maps: dict[str, dict[str, int] | None] = {
        ns: _remote_listing(target_backend, ns) for ns in namespaces
    }
    uploaded_by_ns: dict[str, list[tuple[str, int]]] = {}
    done = 0
    for rel, entry in sorted(plan.items()):
        ns = rel.split("/", 1)[0]
        path: Path = entry["path"]
        size = entry["size"]
        source = entry["source"]
        if size is not None and size > MAX_FILE_BYTES:
            fail(rel, source, f"skipped: {size} B exceeds the "
                              f"{MAX_FILE_BYTES} B single-file cap")
        else:
            _process_file(target_backend, result, fail, rel, ns, path, size,
                          source, existing_maps.get(ns), dry_run,
                          uploaded_by_ns)
        done += 1
        if progress_cb:
            try:
                progress_cb(done, total, rel)
            except Exception:
                log.debug("progress callback raised", exc_info=True)

    # ---- Phase 3: batch size verification of everything uploaded ----
    # Files were tentatively counted in Phase 2; a verification failure here
    # moves them back out of the success counters.
    for ns, entries in uploaded_by_ns.items():
        if not entries:
            continue
        after = _remote_listing(target_backend, ns)
        if after is None:
            # Listing unavailable/partial: per-file existence is the best
            # verification still possible on this backend.
            for rel, size in entries:
                try:
                    if not target_backend.exists(rel):
                        unupload(rel, "target", size,
                                 "verification failed: file missing on "
                                 "target after upload")
                except Exception as e:
                    unupload(rel, "target", size, f"verification failed: {e}")
            continue
        for rel, size in entries:
            remote_size = after.get(rel)
            if remote_size is None:
                unupload(rel, "target", size,
                         "verification failed: file missing on target "
                         "after upload")
            elif remote_size != size:
                unupload(rel, "target", size,
                         f"verification failed: size mismatch on target "
                         f"({remote_size} B vs local {size} B)")

    result["status"] = "partial" if result["failed"] else "ok"
    return result


def _process_file(target_backend, result, fail, rel: str, ns: str,
                  path: Path, size: int | None, source: str,
                  existing: dict[str, int] | None, dry_run: bool,
                  uploaded_by_ns: dict[str, list[tuple[str, int]]]):
    """Copy one planned file. Every step is individually guarded so a single
    bad file is recorded in ``failed`` without aborting the run."""
    try:
        if existing is not None:
            is_new = rel not in existing
        else:
            is_new = not target_backend.exists(rel)
    except Exception as e:
        fail(rel, source, f"target check failed: {e}")
        return
    if not is_new:
        result["skipped_existing"] += 1
        return
    if dry_run:
        result["would_upload"] = result.get("would_upload", 0) + 1
        result["would_upload_bytes"] = result.get("would_upload_bytes", 0) \
            + (size or 0)
        return
    try:
        local_sha = _sha256_file(path)
    except OSError as e:
        fail(rel, source, f"local read failed: {e}")
        return
    try:
        stored_sha = target_backend.upload(str(path), rel)
    except Exception as e:
        fail(rel, source, f"upload failed: {e}")
        return
    # The backend reports the sha of the bytes it stored; a disagreement with
    # the locally computed sha means a corrupt transfer — record a failure,
    # never a success.
    if stored_sha and stored_sha != local_sha:
        fail(rel, source, f"verification failed: target reported sha256 "
                          f"{stored_sha}, local file sha256 is {local_sha}")
        return
    # Best-effort re-read: a backend without hash() support only loses this
    # extra check (the upload-return sha comparison above still held).
    try:
        remote_sha = target_backend.hash(rel)
        if remote_sha != local_sha:
            fail(rel, source, f"verification failed: re-read sha256 "
                              f"{remote_sha} != local {local_sha}")
            return
    except Exception as e:
        log.warning("namespace sync: hash() unavailable for %s on backend "
                    "%s: %s", rel, getattr(target_backend, "name", "?"), e)
    result["uploaded"] += 1
    result["bytes_uploaded"] += size or 0
    uploaded_by_ns.setdefault(ns, []).append((rel, size or 0))

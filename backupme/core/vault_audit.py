"""Read-only vault audit: DB-truth checks over blobs / blob_storage / file_chunks.

Built after an external process rewrote the database and left the index lying
about reality (35,161 phantom refs, 9,701 blob_storage rows pointing at
physically missing paths, 1,527 live files whose chunks have no storage rows
anywhere). This module NEVER writes: every check is a count plus a small
sample list (capped at 10) so a human can see the shape of the damage.

Refcount truth formula (identical to dedup.reconcile_refcounts, which
`db check --repair` already enforces):

    expected(blob) = COUNT(file_chunks rows) + 1 per bundle-member blob row
                     for the member's parent bundle

A raw COUNT(file_chunks) alone would report every healthy parent bundle
(`bundle/<xx>/<yy>/<tarsha>`, ref_count >= 1, never a chunk target) as a
phantom — and reconciling on that formula would zero parent refs, putting
restore metadata on gc's deletion list. Member rows contribute exactly one
reference to their parent (see uploader._flush_batch / dedup.add_ref), so the
tally is part of the truth.

phantom_refs therefore means: ref_count > 0 AND expected == 0 — nothing
references the blob at all, neither a chunk row nor a member row.
"""
import os
import sys
import time

from backupme.log import get_logger

log = get_logger("vault_audit")

_SAMPLE_CAP = 10
_BATCH_SIZE = 500
_MEMBER_LIKE = "bundle/%/%/%/%"
_ARCHIVE_LIKE = "archive/%"


def _parent_blob_id(blob_id: str) -> str | None:
    """Parent bundle blob for a bundle-member id, else None.

    Members look like 'bundle/<xx>/<yy>/<tarsha>/<membersha>' (>= 4 slashes);
    the bundle itself has 4 slash-separated parts and has no parent.
    """
    if not blob_id.startswith("bundle/"):
        return None
    if blob_id.count("/") < 4:
        return None
    return blob_id.rsplit("/", 1)[0]


def compute_expected_refcounts(progress_cb=None) -> dict[str, int]:
    """blob_id -> expected ref_count, in one linear pass (GROUP BY + a Python
    parent tally). Same truth as dedup.reconcile_refcounts; shared by the
    audit and the gated reconcile so both report the same numbers.
    """
    from backupme.core.database import execute

    if progress_cb:
        progress_cb("loading blob ids")
    counts: dict[str, int] = {}
    for (bid,) in execute("SELECT blob_id FROM blobs").fetchall():
        counts[bid] = 0

    if progress_cb:
        progress_cb("tallying file_chunks references")
    for bid, cnt in execute(
        "SELECT blob_id, COUNT(*) FROM file_chunks GROUP BY blob_id"
    ).fetchall():
        counts[bid] = counts.get(bid, 0) + cnt

    if progress_cb:
        progress_cb("tallying bundle member -> parent references")
    for (bid,) in execute(
        "SELECT blob_id FROM blobs WHERE blob_id LIKE 'bundle/%'"
    ).fetchall():
        if bid.count("/") >= 4:  # a member row contributes one ref to its parent
            parent = bid.rsplit("/", 1)[0]
            if parent in counts:
                counts[parent] += 1
    return counts


def _local_roots(config) -> dict[str, str]:
    """backend name -> filesystem root, for local-type backends only."""
    backends_map = None
    if hasattr(config, "backends"):
        backends_map = config.backends
    elif isinstance(config, dict):
        backends_map = config.get("backends")
    roots: dict[str, str] = {}
    for bdef in (backends_map or {}).get("local", []) or []:
        path = getattr(bdef, "path", None) or (bdef.get("path") if isinstance(bdef, dict) else None)
        if path:
            roots[getattr(bdef, "name", None) or bdef.get("name")] = path
    return roots


def _sanitize_parts(rel_path: str) -> list[str]:
    """Same shaping LocalBackend._resolve applies before touching disk."""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != ".."]
    return parts


def _path_checker():
    """Returns an exists(root, rel_path) -> bool closure with a (root, path) cache."""
    cache: dict[tuple[str, str], bool] = {}

    def exists(root: str, rel_path: str) -> bool:
        key = (root, rel_path)
        if key in cache:
            return cache[key]
        full = os.path.join(root, *_sanitize_parts(rel_path))
        if sys.platform == "win32":
            full = "\\\\?\\" + os.path.abspath(full)
        ok = os.path.isfile(full)
        cache[key] = ok
        return ok

    return exists


def audit_vault(config, progress_cb=None) -> dict:
    """Run every vault-truth check. READ-ONLY against the database.

    Returns a flat dict: one count (+ sample, max 10) per check plus status —
    "ok" when every finding is zero, "issues" otherwise.
    """
    from backupme.core.database import execute

    started = time.time()
    exists = _path_checker()
    result: dict = {
        "status": "ok",
        "refcount_drift": 0,
        "refcount_drift_sample": [],
        "phantom_refs": 0,
        "phantom_refs_sample": [],
        "rowless_chunks_live_files": 0,
        "rowless_chunks_live_files_sample": [],
        "fully_rowless": 0,
        "fully_rowless_sample": [],
        "archive_broken": 0,
        "archive_broken_sample": [],
        "rows_missing_paths": {},           # backend -> {"checked": n, "missing": m}
        "rows_missing_paths_sample": [],    # [{"backend", "path"}]
        "rows_missing_paths_skipped_backends": [],  # non-local, not checked
        "rows_missing_paths_skipped_rows": 0,
        "members_rowless_parents": 0,
        "members_rowless_parents_sample": [],  # [{"member", "parent"}]
        "box_coverage": 0,
        "box_coverage_sample": [],
        "box_coverage_status": "skipped",   # "ok" when a 'storagebox' backend exists
        "elapsed": 0.0,
    }

    # 1. refcount drift (expected = chunk refs + member->parent tally)
    if progress_cb:
        progress_cb("checking refcount drift")
    expected = compute_expected_refcounts(progress_cb=progress_cb)
    for bid, ref_count in execute("SELECT blob_id, ref_count FROM blobs").fetchall():
        exp = expected.get(bid, 0)
        if ref_count == exp:
            continue
        result["refcount_drift"] += 1
        if len(result["refcount_drift_sample"]) < _SAMPLE_CAP:
            result["refcount_drift_sample"].append(
                {"blob_id": bid, "ref_count": ref_count, "expected": exp})
        if ref_count > 0 and exp == 0:
            result["phantom_refs"] += 1
            if len(result["phantom_refs_sample"]) < _SAMPLE_CAP:
                result["phantom_refs_sample"].append(bid)

    # 2. live files whose chunks have no blob_storage row at all
    if progress_cb:
        progress_cb("checking chunk -> storage coverage")
    rowless_base = (
        " FROM file_chunks fc"
        " JOIN files f ON f.id = fc.file_id"
        " WHERE f.deleted_at IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM blob_storage bs WHERE bs.blob_id = fc.blob_id)"
    )
    result["rowless_chunks_live_files"] = execute(
        "SELECT COUNT(DISTINCT fc.file_id)" + rowless_base).fetchone()[0]
    for r in execute(
        "SELECT DISTINCT fc.file_id, f.source_root, f.relative_path"
        + rowless_base + " LIMIT ?", (_SAMPLE_CAP,)
    ).fetchall():
        result["rowless_chunks_live_files_sample"].append(
            {"file_id": r["file_id"], "source_root": r["source_root"],
             "relative_path": r["relative_path"]})

    # 3. subset: live files where EVERY chunk is rowless
    fully_base = (
        " FROM files f"
        " WHERE f.deleted_at IS NULL"
        " AND EXISTS (SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id)"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM file_chunks fc2"
        "   JOIN blob_storage bs2 ON bs2.blob_id = fc2.blob_id"
        "   WHERE fc2.file_id = f.id)"
    )
    result["fully_rowless"] = execute("SELECT COUNT(*)" + fully_base).fetchone()[0]
    for r in execute(
        "SELECT f.id, f.source_root, f.relative_path" + fully_base + " LIMIT ?",
        (_SAMPLE_CAP,),
    ).fetchall():
        result["fully_rowless_sample"].append(
            {"file_id": r["id"], "source_root": r["source_root"],
             "relative_path": r["relative_path"]})

    # 4. same rowless damage restricted to the archive namespace
    result["archive_broken"] = execute(
        "SELECT COUNT(DISTINCT fc.file_id)" + rowless_base
        + " AND f.source_root LIKE ?", (_ARCHIVE_LIKE,)).fetchone()[0]
    for r in execute(
        "SELECT DISTINCT fc.file_id, f.source_root, f.relative_path"
        + rowless_base + " AND f.source_root LIKE ? LIMIT ?",
        (_ARCHIVE_LIKE, _SAMPLE_CAP),
    ).fetchall():
        result["archive_broken_sample"].append(
            {"file_id": r["file_id"], "source_root": r["source_root"],
             "relative_path": r["relative_path"]})

    # 5. blob_storage rows whose physical file is missing (local backends only)
    if progress_cb:
        progress_cb("checking physical paths on local backends")
    roots = _local_roots(config)
    skipped_backends: set[str] = set()
    skipped_rows = 0
    per_backend: dict[str, dict[str, int]] = {}
    rows = execute(
        "SELECT backend_name, storage_path FROM blob_storage"
        " WHERE storage_path NOT LIKE '%@%'"
    ).fetchall()
    for r in rows:
        backend = r["backend_name"]
        rel = r["storage_path"]
        if backend not in roots:
            skipped_backends.add(backend)
            skipped_rows += 1
            continue
        entry = per_backend.setdefault(backend, {"checked": 0, "missing": 0})
        entry["checked"] += 1
        if not exists(roots[backend], rel):
            entry["missing"] += 1
            if len(result["rows_missing_paths_sample"]) < _SAMPLE_CAP:
                result["rows_missing_paths_sample"].append(
                    {"backend": backend, "path": rel})
    result["rows_missing_paths"] = per_backend
    result["rows_missing_paths_skipped_backends"] = sorted(skipped_backends)
    result["rows_missing_paths_skipped_rows"] = skipped_rows

    # 6. referenced bundle members whose parent tar has no storage row at all
    if progress_cb:
        progress_cb("checking member -> parent coverage")
    member_ids = []
    for (bid,) in execute(
        "SELECT DISTINCT blob_id FROM file_chunks WHERE blob_id LIKE ?",
        (_MEMBER_LIKE,),
    ).fetchall():
        if bid.count("/") >= 4:
            member_ids.append(bid)
    parents = {bid.rsplit("/", 1)[0] for bid in member_ids}
    parents_with_storage: set[str] = set()
    ordered = sorted(parents)
    for i in range(0, len(ordered), _BATCH_SIZE):
        chunk = ordered[i:i + _BATCH_SIZE]
        placeholders = ",".join("?" * len(chunk))
        for r in execute(
            f"SELECT DISTINCT blob_id FROM blob_storage WHERE blob_id IN ({placeholders})",
            tuple(chunk),
        ).fetchall():
            parents_with_storage.add(r[0])
    rowless_parents = parents - parents_with_storage
    result["members_rowless_parents"] = sum(
        1 for bid in member_ids if bid.rsplit("/", 1)[0] in rowless_parents)
    for bid in member_ids:
        parent = bid.rsplit("/", 1)[0]
        if parent in rowless_parents and len(result["members_rowless_parents_sample"]) < _SAMPLE_CAP:
            result["members_rowless_parents_sample"].append(
                {"member": bid, "parent": parent})

    # 7. coverage of referenced top-level blobs on the 'storagebox' backend
    if progress_cb:
        progress_cb("checking storagebox coverage")
    backends_map = config.backends if hasattr(config, "backends") else (config.get("backends") or {})
    box_exists = any(
        getattr(b, "name", None) == "storagebox"
        for blist in (backends_map or {}).values()
        for b in blist
    )
    if box_exists:
        result["box_coverage_status"] = "ok"
        box_base = (
            " FROM blobs b"
            " WHERE b.ref_count > 0"
            " AND b.blob_id NOT LIKE '" + _MEMBER_LIKE + "'"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM blob_storage bs"
            "   WHERE bs.blob_id = b.blob_id AND bs.backend_name = 'storagebox')"
        )
        result["box_coverage"] = execute("SELECT COUNT(*)" + box_base).fetchone()[0]
        for r in execute(
            "SELECT b.blob_id" + box_base + " LIMIT ?", (_SAMPLE_CAP,)
        ).fetchall():
            result["box_coverage_sample"].append(r["blob_id"])

    # overall status: "ok" only when nothing is wrong
    problems = (
        result["refcount_drift"]
        + result["rowless_chunks_live_files"]
        + result["fully_rowless"]
        + result["archive_broken"]
        + result["members_rowless_parents"]
        + result["box_coverage"]
        + sum(e["missing"] for e in result["rows_missing_paths"].values())
    )
    result["status"] = "ok" if problems == 0 else "issues"
    result["elapsed"] = round(time.time() - started, 3)
    return result

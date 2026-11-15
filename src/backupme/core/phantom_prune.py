"""Prune blob_storage rows that claim objects which do not physically exist
(``backupme backend prune-phantom-rows NAME``).

Scenario: an external database rewrite (or a buggy importer) left
blob_storage rows pointing at storage paths that exist NOWHERE on the
backend. Such phantom rows poison every coverage computation — replication
skip-logic trusts them, so a blob is believed replicated when it is not.

Safety rules (the whole point of this module):
- DB rows are the ONLY thing ever deleted. No file/object is touched —
  ``backend.delete()`` is never called and there is no code path to it.
- Bundle MEMBER rows (``storage_path`` containing ``@offset:len``) are never
  candidates: their content lives inside the parent bundle tar, so they need
  no object of their own.
- A row is only prune-safe when the backend confirms the object is missing
  AND the same blob_id still has another blob_storage row somewhere else
  (another backend, or another path on this one). A phantom row that is the
  LAST row for its blob anywhere is a data-loss signal: it is reported as
  ``last_copy_missing`` and NEVER deleted by --apply.
- Every candidate is re-verified at apply time (object existence + row
  existence + remaining coverage), so a stale candidate list cannot delete
  a row whose object reappeared or whose last sibling vanished meanwhile.
- If EVERY scanned top-level row on a backend looks phantom (>= 100 rows),
  the likely cause is a misconfigured backend root — the apply refuses
  rather than mass-delete (override only via the ``allow_all_phantom``
  argument; the CLI does not expose it).

Existence check: one walk of the backend builds an on-box path set (cheap
membership for rows that obviously exist); every walk-miss is still
confirmed with a per-row ``backend.exists()`` call, so an incomplete or
failed walk can never create a false phantom — it only loses the fast path.
Rows whose ``exists()`` call raises are kept (cannot verify -> keep) and
recorded in the report's ``errors``.
"""
import hashlib
import time

from backupme.core import op_log
from backupme.core.database import commit, execute
from backupme.log import get_logger

log = get_logger("phantom_prune")

_SAMPLE_LIMIT = 20
_ALL_PHANTOM_REFUSE_THRESHOLD = 100


def _walk_set(backend, errors: list) -> set[str]:
    """Best-effort on-box path set (forward-slash normalized). Failure is
    non-fatal: the per-row exists() check remains authoritative."""
    on_box: set[str] = set()
    try:
        for item in backend.walk():
            path = item.get("path") if isinstance(item, dict) else getattr(item, "path", None)
            if path:
                on_box.add(str(path).replace("\\", "/"))
    except Exception as e:  # walk is only an optimization — never a verdict
        errors.append(f"walk failed ({type(e).__name__}: {e}); using per-row exists() only")
    return on_box


def _object_exists(backend, rel: str, on_box: set[str]) -> bool:
    """Walk-set membership first, per-row backend.exists() as the authority."""
    if rel in on_box:
        return True
    return bool(backend.exists(rel))


def _scan(backend, config, progress_cb=None) -> tuple[dict, list[dict]]:
    """Shared scan for find_phantom_rows/apply_prune.

    Returns (report, phantom_rows); phantom_rows carries EVERY phantom row
    (the report only samples _SAMPLE_LIMIT) as dicts with keys backend,
    path, blob_id, stored_size, covered_elsewhere, last_copy_missing.
    """
    started = time.monotonic()
    report: dict = {
        "status": "ok",
        "backend": backend.name,
        "scanned": 0,
        "phantom": 0,
        "last_copy_missing": 0,
        "safe_candidates": 0,
        "member_rows_skipped": 0,
        "errors": [],
        "phantom_sample": [],
        "all_phantom_suspicious": False,
        "elapsed": 0.0,
    }
    if not backend.connected:
        try:
            if not backend.connect():
                report["status"] = "error"
                report["error"] = f"backend {backend.name!r} failed to connect"
                return report, []
        except Exception as e:
            report["status"] = "error"
            report["error"] = f"backend {backend.name!r} connect failed: {e}"
            return report, []

    backend_name = backend.name
    report["member_rows_skipped"] = execute(
        "SELECT COUNT(*) AS c FROM blob_storage "
        "WHERE backend_name = ? AND storage_path LIKE '%@%'",
        (backend_name,),
    ).fetchone()["c"]

    rows = execute(
        "SELECT blob_id, storage_path, stored_size FROM blob_storage "
        "WHERE backend_name = ? AND storage_path NOT LIKE '%@%' "
        "ORDER BY blob_id, storage_path",
        (backend_name,),
    ).fetchall()
    report["scanned"] = len(rows)

    on_box = _walk_set(backend, report["errors"])

    phantom_rows: list[dict] = []
    for done, row in enumerate(rows, 1):
        blob_id = row["blob_id"]
        rel = str(row["storage_path"]).replace("\\", "/")
        missing = False
        try:
            missing = not _object_exists(backend, rel, on_box)
        except Exception as e:
            # Cannot verify -> keep the row (conservative), record why.
            report["errors"].append(f"{rel}: exists() failed: {type(e).__name__}: {e}")
        if missing:
            # What does the DB lose if this row goes? Count the blob's OTHER
            # rows (any backend, any other path on this one). Zero others
            # means this phantom is the last trace of the blob — a data-loss
            # signal.
            other_rows = execute(
                "SELECT COUNT(*) AS c FROM blob_storage "
                "WHERE blob_id = ? AND NOT (backend_name = ? AND storage_path = ?)",
                (blob_id, backend_name, row["storage_path"]),
            ).fetchone()["c"]
            covered = bool(other_rows)
            entry = {
                "backend": backend_name,
                "path": row["storage_path"],
                "blob_id": blob_id,
                "stored_size": row["stored_size"],
                "covered_elsewhere": covered,
                "last_copy_missing": not covered,
            }
            phantom_rows.append(entry)
            if len(report["phantom_sample"]) < _SAMPLE_LIMIT:
                report["phantom_sample"].append(entry)
        if progress_cb is not None:
            progress_cb(blob_id, done, len(rows))

    report["phantom"] = len(phantom_rows)
    report["last_copy_missing"] = sum(1 for p in phantom_rows if p["last_copy_missing"])
    report["safe_candidates"] = report["phantom"] - report["last_copy_missing"]
    report["all_phantom_suspicious"] = bool(
        report["scanned"] >= _ALL_PHANTOM_REFUSE_THRESHOLD
        and report["phantom"] == report["scanned"]
    )
    report["elapsed"] = round(time.monotonic() - started, 3)
    log.info("phantom scan on %s: scanned=%d phantom=%d last_copy_missing=%d errors=%d",
             backend_name, report["scanned"], report["phantom"],
             report["last_copy_missing"], len(report["errors"]))
    return report, phantom_rows


def find_phantom_rows(backend, config, progress_cb=None) -> dict:
    """Report-only scan: which blob_storage rows on this backend point at
    objects that do not physically exist?

    Never writes anything (no DB changes, no files touched). The report has:
    scanned, phantom (+ phantom_sample up to 20 entries with backend/path/
    blob_id/last_copy_missing), safe_candidates (phantom minus
    last_copy_missing), member_rows_skipped ('@' rows are never candidates)
    and errors.
    """
    report, _phantom_rows = _scan(backend, config, progress_cb)
    return report


def apply_prune(backend, config, candidates=None, batch_size: int = 500,
                progress_cb=None, allow_all_phantom: bool = False) -> dict:
    """Delete ONLY the DB rows for phantom candidates with
    last_copy_missing=False. Never deletes a file, never touches member
    rows, never touches rows for objects that exist.

    candidates: list of phantom dicts as produced by find_phantom_rows (or
    None to rescan now). Every candidate is re-verified before its DELETE:
    the object must still be missing, the row must still exist, and the
    blob must still be covered by another blob_storage row. op_log gets one
    ``prune_phantom_rows`` entry (idempotency key derived from backend +
    exact row set) appended before the first delete, marked committed after
    the last batch and failed if the batch loop raises unexpectedly; a no-op
    run (nothing to delete) writes no entry.
    """
    started = time.monotonic()
    batch_size = max(1, int(batch_size))
    report: dict = {
        "status": "ok",
        "backend": backend.name,
        "applied": True,
        "planned": 0,
        "deleted": 0,
        "batches": 0,
        "kept_last_copy": 0,
        "reappeared": 0,
        "uncovered_now": 0,
        "gone_from_db": 0,
        "skipped_member": 0,
        "phantom": 0,
        "last_copy_missing": 0,
        "member_rows_skipped": 0,
        "errors": [],
        "op_log_id": None,
        "elapsed": 0.0,
    }

    if candidates is None:
        scan_report, phantom_rows = _scan(backend, config, progress_cb)
        report["phantom"] = scan_report["phantom"]
        report["last_copy_missing"] = scan_report["last_copy_missing"]
        report["member_rows_skipped"] = scan_report["member_rows_skipped"]
        report["errors"].extend(scan_report["errors"])
        if scan_report["status"] == "error":
            report["status"] = "error"
            report["error"] = scan_report.get("error", "backend scan failed")
            report["elapsed"] = round(time.monotonic() - started, 3)
            return report
        if scan_report["all_phantom_suspicious"] and not allow_all_phantom:
            report["status"] = "refused_all_phantom"
            report["error"] = (
                f"all {scan_report['scanned']} scanned row(s) on "
                f"{backend.name!r} look phantom — that pattern usually means "
                f"the backend root/remote is misconfigured (verify with "
                f"'backend rescan' and 'backend test' first); nothing deleted"
            )
            report["elapsed"] = round(time.monotonic() - started, 3)
            log.warning("apply refused on %s: 100%% of scanned rows phantom",
                        backend.name)
            return report
        targets = [p for p in phantom_rows if not p["last_copy_missing"]]
        report["kept_last_copy"] = scan_report["last_copy_missing"]
    else:
        targets = []
        for cand in candidates:
            if cand.get("last_copy_missing"):
                report["kept_last_copy"] += 1
                continue
            targets.append(cand)

    backend_name = backend.name
    on_box = _walk_set(backend, report["errors"])

    # --- re-verify every candidate right before deleting -------------------
    safe: list[tuple[str, str]] = []
    for cand in targets:
        blob_id = cand.get("blob_id")
        raw_path = cand.get("path") or cand.get("storage_path") or ""
        rel = str(raw_path).replace("\\", "/")
        if not blob_id or not rel:
            report["errors"].append(f"malformed candidate skipped: {cand!r}")
            continue
        if "@" in rel:
            # Belt and suspenders: member rows are never candidates.
            report["skipped_member"] += 1
            continue
        try:
            if _object_exists(backend, rel, on_box):
                report["reappeared"] += 1
                continue
        except Exception as e:
            report["errors"].append(f"{rel}: exists() failed: {type(e).__name__}: {e}")
            continue
        still_there = execute(
            "SELECT COUNT(*) AS c FROM blob_storage "
            "WHERE backend_name = ? AND blob_id = ? AND storage_path = ?",
            (backend_name, blob_id, raw_path),
        ).fetchone()["c"]
        if not still_there:
            report["gone_from_db"] += 1
            continue
        other_rows = execute(
            "SELECT COUNT(*) AS c FROM blob_storage "
            "WHERE blob_id = ? AND NOT (backend_name = ? AND storage_path = ?)",
            (blob_id, backend_name, raw_path),
        ).fetchone()["c"]
        if not other_rows:
            report["uncovered_now"] += 1
            continue
        safe.append((blob_id, raw_path))

    report["planned"] = len(safe)
    if not safe:
        report["status"] = "partial" if report["errors"] else "ok"
        report["elapsed"] = round(time.monotonic() - started, 3)
        return report

    # --- op_log: one entry, appended before the first delete ---------------
    digest = hashlib.sha256()
    for blob_id, path in sorted(safe):
        digest.update(f"{backend_name}\x00{blob_id}\x00{path}\n".encode("utf-8"))
    idempotency_key = f"prune_phantom_rows:{backend_name}:{digest.hexdigest()[:16]}"
    target = {
        "backend": backend_name,
        "planned": len(safe),
        "batch_size": batch_size,
        "sample": [
            {"blob_id": b, "storage_path": p} for b, p in safe[:_SAMPLE_LIMIT]
        ],
    }
    op = op_log.append("prune_phantom_rows", target, idempotency_key=idempotency_key)
    op_id = op["id"]
    report["op_log_id"] = op_id

    total = len(safe)
    try:
        for start in range(0, total, batch_size):
            batch = safe[start:start + batch_size]
            for blob_id, path in batch:
                try:
                    cur = execute(
                        "DELETE FROM blob_storage "
                        "WHERE backend_name = ? AND blob_id = ? AND storage_path = ?",
                        (backend_name, blob_id, path),
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        report["deleted"] += cur.rowcount
                except Exception as e:
                    report["errors"].append(f"{path}: delete failed: {type(e).__name__}: {e}")
            commit()
            report["batches"] += 1
            if progress_cb is not None:
                progress_cb(batch[-1][0], min(start + batch_size, total), total)
    except Exception as e:
        # An unexpected failure (DB error, storage hiccup) must leave the
        # op_log row in 'failed', not a dangling 'pending' (P1-4 parity with
        # db_reconcile). Per-row delete failures above stay non-fatal.
        op_log.mark_failed(op_id, str(e))
        raise

    op_log.mark_committed(op_id)
    if report["errors"]:
        report["status"] = "partial"
    report["elapsed"] = round(time.monotonic() - started, 3)
    log.info("phantom prune on %s: planned=%d deleted=%d batches=%d "
             "kept_last_copy=%d reappeared=%d uncovered_now=%d errors=%d",
             backend_name, report["planned"], report["deleted"],
             report["batches"], report["kept_last_copy"], report["reappeared"],
             report["uncovered_now"], len(report["errors"]))
    return report

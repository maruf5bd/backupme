"""Gated ref-count reconcile: recompute blob ref_counts from DB truth.

Truth formula (identical to dedup.reconcile_refcounts, which `db check
--repair` already enforces — see vault_audit.compute_expected_refcounts):

    expected(blob) = COUNT(file_chunks rows) + 1 per bundle-member blob row
                     for the member's parent bundle

DEFAULT apply=False: report only — drift count, sample, would_fix. The
database is never touched on a dry run.

apply=True: UPDATE blobs SET ref_count = expected, in batches with an
explicit commit() every 500 rows. This module NEVER deletes blobs, files,
chunk rows, or storage objects — it corrects counts only, so a botched
external rewrite cannot be made worse by a reconcile.
"""
import time

from backupme.log import get_logger

log = get_logger("db_reconcile")

from backupme.core.database import execute, executemany, commit
from backupme.core import op_log as op_log_mod
from backupme.core.vault_audit import compute_expected_refcounts

_BATCH_SIZE = 500
_SAMPLE_CAP = 10


def reconcile_refs(config, apply: bool = False, limit: int | None = None,
                   progress_cb=None) -> dict:
    """Recompute blob ref_counts from file_chunks (+ member->parent tally).

    Dry-run by default: reports drift_total / would_fix and changes nothing.
    With apply=True the corrected counts are written batched (commit every
    _BATCH_SIZE rows) and an op_log entry records the run:
    append("reconcile_refs", ...) then mark_committed / mark_failed.

    Returns: apply, drift_total, fixed, would_fix, limit, sample, elapsed
    (seconds), op_log_id (apply only).
    """
    started = time.time()
    if progress_cb:
        progress_cb("computing expected refcounts")
    expected = compute_expected_refcounts(progress_cb=progress_cb)
    if progress_cb:
        progress_cb("diffing against stored ref_counts")
    current = {
        r[0]: r[1]
        for r in execute("SELECT blob_id, ref_count FROM blobs").fetchall()
    }
    drift = [
        (bid, current[bid], expected.get(bid, 0))
        for bid in current
        if current[bid] != expected.get(bid, 0)
    ]
    drift_total = len(drift)

    # Order deterministically so --limit cuts a stable slice across re-runs.
    drift.sort(key=lambda t: t[0])
    targets = [(exp, bid) for bid, _rc, exp in drift]
    if limit is not None:
        targets = targets[:max(0, int(limit))]

    result: dict = {
        "apply": bool(apply),
        "drift_total": drift_total,
        "fixed": 0,
        "would_fix": 0,
        "limit": limit,
        "sample": [
            {"blob_id": bid, "ref_count": rc, "expected": exp}
            for bid, rc, exp in drift[:_SAMPLE_CAP]
        ],
        "elapsed": 0.0,
    }

    if not apply:
        result["would_fix"] = len(targets)
        result["elapsed"] = round(time.time() - started, 3)
        return result

    entry = op_log_mod.append(
        "reconcile_refs",
        {"apply": True, "drift_total": drift_total, "limit": limit,
         "planned": len(targets)},
        idempotency_key=f"reconcile_refs:{int(time.time())}",
    )
    op_id = entry["id"]
    log.info("reconcile_refs op #%s: %d drifted blob(s), planned %d fix(es)",
             op_id, drift_total, len(targets))

    fixed = 0
    try:
        for i in range(0, len(targets), _BATCH_SIZE):
            batch = targets[i:i + _BATCH_SIZE]
            executemany("UPDATE blobs SET ref_count = ? WHERE blob_id = ?", batch)
            commit()
            fixed += len(batch)
            if progress_cb:
                progress_cb(f"reconciled {fixed}/{len(targets)}")
    except Exception as e:
        op_log_mod.mark_failed(op_id, str(e))
        raise

    op_log_mod.mark_committed(op_id)
    log.info("reconcile_refs op #%s committed: %d blob ref_count(s) corrected",
             op_id, fixed)

    result["fixed"] = fixed
    result["would_fix"] = len(targets)
    result["op_log_id"] = op_id
    result["elapsed"] = round(time.time() - started, 3)
    return result

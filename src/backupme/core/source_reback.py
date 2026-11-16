"""Original-source re-back: heal DB-loss residue through the real pipeline.

A live ``files`` row is ROWLESS when none of its file_chunks' blob_ids has
any blob_storage row — the storage metadata that located the physical object
is gone (DB-loss residue) while the index row still references the blobs.
Such a file cannot be restored from storage, but when the ORIGINAL source
file still exists on disk with the same size AND sha256, it can be re-backed
IN PLACE through the real pipeline:

    uploader.upload_file(source_root=same, relative_path=same,
                         local_path=<resolved source>, ...)

index_file matches on (source_root, relative_path, version_seq) and updates
the row in place; _replace_file_chunks replaces the chunk mapping (unref-ing
displaced blobs). No new version, no row churn.

This depends on the uploader donor guard: the donor dedup branch must never
resolve to a donor whose chunks have no storage coverage (typically the
rowless file itself — oldest id with its sha256), or the "upload" would
silently re-map onto the same dead blobs without uploading anything. With
the guard, a healthy same-sha donor heals the file by mapping copy;
otherwise the file is really uploaded and the missing blob_storage rows are
recreated.

Nothing is deleted by this module: rowless blobs stay untouched (gc
territory). Because bundle/member blob ids are content-derived, a re-created
member can reuse its old blob id; the pipeline's add_ref accounting may then
leave a small drift on parent bundles, so one dedup.reconcile_refcounts()
pass runs at the end of every applied run that healed at least one file.

Files whose source is gone or changed are reported honestly and skipped —
never guessed, never partially healed:

- VIRTUAL_ROOT        source_root is not a real local path ('noroot', backend
                      names, 'archive/<fp>' virtual roots, 'cpanel/<acct>')
- SOURCE_GONE         original source file missing (and no usable state-file
                      copy — see --state-file)
- SOURCE_SIZE_CHANGED local size != files.size
- SOURCE_CHANGED      local sha256 != files.sha256 (hashed only when the
                      size matches, streamed)
- STATE_MISMATCH      a state-file copy exists but its size/sha256 does not
                      match the DB row
- ALREADY_COVERED     the candidate gained storage coverage before it was
                      processed (e.g. a same-sha file healed it first)

Applied-only failures are reported per file, NOT checkpointed, and are
therefore retried on the next run:
- UPLOAD_FAILED       upload_bundle returned an error (e.g. all backends down)
- VERIFY_FAILED       post-upload the chunks still lack storage coverage, or
                      files.sha256 no longer matches

Resumability: applied runs checkpoint healed/skipped file ids to
``reback_checkpoint.json`` (atomic tmp+replace, next to the CWD like the
rebalance checkpoints). Reruns skip processed ids; failed ids are retried.
Dry runs never write — no uploads, no DB writes, no checkpoint, no op_log.
"""
import json
import time
from pathlib import Path

from backupme.core import state as state_mod
from backupme.log import get_logger

# --- data helpers ----------------------------------------------------------

from backupme.core.database import execute
from backupme.core import op_log

log = get_logger("source_reback")

_SAMPLE_CAP = 20
_CHECKPOINT_NAME = "reback_checkpoint.json"
_CHECKPOINT_EVERY = 25
_PROGRESS_EVERY = 100

# Live files that HAVE chunk rows but none of their chunk blobs has any
# blob_storage row (same shape as the vault-audit "fully rowless" query).
_ROWLESS_WHERE = (
    " WHERE f.deleted_at IS NULL"
    " AND EXISTS (SELECT 1 FROM file_chunks fc0 WHERE fc0.file_id = f.id)"
    " AND NOT EXISTS ("
    "   SELECT 1 FROM file_chunks fc2"
    "   JOIN blob_storage bs ON bs.blob_id = fc2.blob_id"
    "   WHERE fc2.file_id = f.id)"
)


def find_rowless_live_files(config=None, limit=None) -> dict:
    """Count live files whose chunks have no blob_storage rows anywhere and
    return a capped sample with the fields needed to judge healability.

    ``limit`` caps the sample size (default ``_SAMPLE_CAP``); ``total`` is
    always the full count. ``config`` is accepted for signature symmetry and
    unused (the hot store is process-global).
    """
    total = execute("SELECT COUNT(*) FROM files f" + _ROWLESS_WHERE).fetchone()[0]
    cap = _SAMPLE_CAP if limit is None else max(0, int(limit))
    rows = execute(
        "SELECT f.id, f.source_root, f.relative_path, f.sha256, f.size,"
        " (SELECT COUNT(*) FROM file_chunks c WHERE c.file_id = f.id) AS chunk_count"
        " FROM files f" + _ROWLESS_WHERE +
        " ORDER BY f.id ASC LIMIT ?", (cap,)
    ).fetchall()
    sample = [{
        "file_id": r["id"],
        "source_root": r["source_root"],
        "relative_path": r["relative_path"],
        "sha256": r["sha256"],
        "size": r["size"],
        "chunk_count": r["chunk_count"],
        "virtual_root": _is_virtual_root(r["source_root"]),
    } for r in rows]
    return {"total": total, "sample": sample, "sample_cap": cap}


def _rowless_candidates() -> list[dict]:
    """All rowless live file rows, oldest id first (heal order)."""
    rows = execute(
        "SELECT f.id, f.source_root, f.relative_path, f.sha256, f.size,"
        " f.version_seq, f.version_group"
        " FROM files f" + _ROWLESS_WHERE +
        " ORDER BY f.id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def _is_virtual_root(source_root: str) -> bool:
    """True when source_root cannot name a real local directory.

    Legacy 'noroot' markers, backend names, and virtual namespaces
    ('archive/<fingerprint>', 'cpanel/<account>') are all relative paths;
    a real original source root is always an absolute filesystem path.
    """
    sr = (source_root or "").strip()
    if not sr:
        return True
    if sr.lower().startswith("archive/"):
        return True
    try:
        return not Path(sr).is_absolute()
    except (OSError, ValueError):
        return True


def _load_state_map(state_file) -> dict:
    """Load a campaign-style state file: {"ok": {file_id: {path, sha256, size}}}.

    Returns {} for a missing/unreadable/malformed file (logged, never raised —
    the fallback is best-effort by design).
    """
    if not state_file:
        return {}
    path = Path(state_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("state file %s not found — no state-copy fallback", path)
        return {}
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("unreadable state file %s — no state-copy fallback: %s", path, e)
        return {}
    ok = data.get("ok") if isinstance(data, dict) else None
    if not isinstance(ok, dict):
        return {}
    return {
        str(k): v for k, v in ok.items()
        if isinstance(v, dict) and v.get("path")
    }


def _verify_state_copy(entry: dict, row: dict) -> tuple[bool, str | None, str]:
    """Validate a state-file copy against the DB row (the truth).

    Size is compared first; the sha256 is streamed only when the size
    matches. Returns (usable, local_path, note).
    """
    path = Path(str(entry.get("path") or ""))
    if not path.is_file():
        return False, None, "state copy missing on disk"
    try:
        actual_size = path.stat().st_size
    except OSError as e:
        return False, None, f"state copy unreadable: {e}"
    if actual_size != row["size"]:
        return False, None, f"state copy size {actual_size} != DB size {row['size']}"
    from backupme.core.hasher import sha256_file
    actual_sha = sha256_file(str(path)).lower()
    if actual_sha != (row["sha256"] or "").lower():
        return False, None, "state copy sha256 does not match the DB row"
    return True, str(path), "state copy verified"


def _classify(row: dict, state_map: dict) -> tuple[str, dict]:
    """Resolve one candidate's original source.

    Returns ("heal", {"local_path", "path", "source_kind"}) or
    ("skip", {"reason", "path", ...}). Read-only: hashing is allowed in
    dry runs, nothing is written.
    """
    file_id = row["id"]
    source_root = (row["source_root"] or "").strip()
    rel_path = row["relative_path"] or ""

    if not _is_virtual_root(source_root):
        source = Path(source_root) / rel_path
        if source.is_file():
            try:
                actual_size = source.stat().st_size
            except OSError as e:
                return "skip", {"reason": "SOURCE_GONE", "path": str(source),
                                "note": f"source unreadable: {e}"}
            if actual_size != row["size"]:
                return "skip", {"reason": "SOURCE_SIZE_CHANGED",
                                "path": str(source),
                                "note": f"source size {actual_size} != DB size {row['size']}"}
            from backupme.core.hasher import sha256_file
            actual_sha = sha256_file(str(source)).lower()
            if actual_sha != (row["sha256"] or "").lower():
                return "skip", {"reason": "SOURCE_CHANGED", "path": str(source),
                                "note": "source content differs from the DB row"}
            return "heal", {"local_path": str(source), "path": str(source),
                            "source_kind": "original"}
        # original missing -> fall through to the state-file fallback

    entry = state_map.get(str(file_id))
    if entry is not None:
        usable, local_path, note = _verify_state_copy(entry, row)
        if usable:
            return "heal", {"local_path": local_path, "path": local_path,
                            "source_kind": "state_file"}
        if not _is_virtual_root(source_root):
            reason = "SOURCE_GONE" if not source.exists() else "SOURCE_SIZE_CHANGED"
            return "skip", {"reason": "STATE_MISMATCH", "path": str(entry.get("path")),
                            "note": f"original unusable ({reason}); state copy: {note}"}
        return "skip", {"reason": "STATE_MISMATCH", "path": str(entry.get("path")),
                        "note": f"virtual root '{source_root}'; state copy: {note}"}

    if _is_virtual_root(source_root):
        return "skip", {"reason": "VIRTUAL_ROOT",
                        "path": f"{source_root}/{rel_path}",
                        "note": "not a real local path and no state-file copy"}
    return "skip", {"reason": "SOURCE_GONE", "path": str(Path(source_root) / rel_path),
                    "note": "original missing and no usable state-file copy"}


def _load_checkpoint(path: Path) -> dict:
    data = state_mod.read_json_checkpoint(path, lambda: {"healed": {}, "skipped": {}})
    if not isinstance(data, dict):
        log.warning("unreadable checkpoint %s — starting fresh", path)
        return {"healed": {}, "skipped": {}}
    healed = data.get("healed")
    skipped = data.get("skipped")
    if not isinstance(healed, dict) or not isinstance(skipped, dict):
        log.warning("malformed checkpoint %s — starting fresh", path)
        return {"healed": {}, "skipped": {}}
    return {"healed": {str(k): v for k, v in healed.items()},
            "skipped": {str(k): v for k, v in skipped.items()}}


def _save_checkpoint(path: Path, ck: dict) -> None:
    state_mod.write_json_checkpoint(path, ck, sort_keys=True)


def _file_has_storage(file_id: int) -> bool:
    row = execute(
        "SELECT 1 FROM file_chunks fc"
        " JOIN blob_storage bs ON bs.blob_id = fc.blob_id"
        " WHERE fc.file_id = ? LIMIT 1", (file_id,)
    ).fetchone()
    return row is not None


def _verify_healed(file_id: int, expected_sha: str) -> tuple[bool, str]:
    """Post-upload DB check: the file's chunks must all have storage coverage
    and files.sha256 must be unchanged."""
    row = execute("SELECT sha256 FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return False, "file row missing after upload"
    if (row["sha256"] or "").lower() != (expected_sha or "").lower():
        return False, f"files.sha256 changed after upload: {row['sha256']}"
    uncovered = execute(
        "SELECT COUNT(*) FROM file_chunks fc WHERE fc.file_id = ?"
        " AND NOT EXISTS (SELECT 1 FROM blob_storage bs WHERE bs.blob_id = fc.blob_id)",
        (file_id,)
    ).fetchone()[0]
    if uncovered:
        return False, f"{uncovered} chunk(s) still lack blob_storage rows"
    chunks = execute(
        "SELECT COUNT(*) FROM file_chunks WHERE file_id = ?", (file_id,)
    ).fetchone()[0]
    if not chunks:
        return False, "no chunk rows after upload"
    return True, ""


def reback_sources(config=None, dry_run: bool = True, limit: int | None = None,
                   progress_cb=None, state_file=None,
                   checkpoint_path=None) -> dict:
    """Re-back rowless live files from their original sources, in place.

    dry_run=True (default) classifies every candidate read-only and reports
    would_heal / would_skip with reasons — zero uploads, zero DB writes, no
    checkpoint, no op_log entry. dry_run=False performs the real uploads,
    checkpoints progress, appends one 'reback_sources' op_log entry
    (mark_committed on success, mark_failed if the run raises) and, when at
    least one file healed, runs one dedup.reconcile_refcounts() pass.
    """
    started = time.monotonic()
    if config is None:
        from backupme.config import load_config
        config = load_config()

    candidates = _rowless_candidates()
    total = len(candidates)
    state_map = _load_state_map(state_file)
    ck_path = Path(checkpoint_path) if checkpoint_path else Path(_CHECKPOINT_NAME)
    ck = {"healed": {}, "skipped": {}}
    if not dry_run:
        ck = _load_checkpoint(ck_path)

    def progress(message: str) -> None:
        log.info("%s", message)
        if progress_cb:
            try:
                progress_cb(message)
            except Exception:
                log.debug("progress_cb failed", exc_info=True)

    healed = 0
    skipped = 0
    already_processed = 0
    processed = 0
    failures: list[dict] = []
    skip_reasons: dict[str, int] = {}
    sample: list[dict] = []

    def record_sample(file_id: int, outcome: str, path: str,
                      source_kind: str | None = None,
                      reason: str | None = None) -> None:
        if len(sample) < _SAMPLE_CAP:
            sample.append({
                "file_id": file_id, "outcome": outcome, "path": path,
                "source_kind": source_kind, "reason": reason,
            })

    def note_skip(file_id: int, reason: str, path: str,
                  source_kind: str | None = None, note: str | None = None) -> None:
        nonlocal skipped
        skipped += 1
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        record_sample(file_id, "would_skip" if dry_run else "skipped",
                      path, source_kind, reason)
        if not dry_run:
            ck["skipped"][str(file_id)] = {
                "reason": reason, "path": path, "note": note,
            }

    op = None
    if not dry_run:
        op = op_log.append("reback_sources", {
            "apply": True,
            "limit": limit,
            "candidates": total,
            "state_file": str(state_file) if state_file else None,
        })

    try:
        for row in candidates:
            if limit is not None and processed >= max(0, int(limit)):
                break
            file_id = row["id"]
            if not dry_run and (
                str(file_id) in ck["healed"] or str(file_id) in ck["skipped"]
            ):
                already_processed += 1
                continue
            processed += 1

            status, detail = _classify(row, state_map)
            path = detail.get("path") or ""
            if status == "skip":
                note_skip(file_id, detail["reason"], path,
                          detail.get("source_kind"), detail.get("note"))
                continue

            source_kind = detail.get("source_kind")
            if dry_run:
                healed += 1  # would heal
                record_sample(file_id, "would_heal", path, source_kind)
                continue

            # A same-sha file processed earlier in this run may already have
            # restored coverage for this candidate (shared member blob).
            if _file_has_storage(file_id):
                note_skip(file_id, "ALREADY_COVERED", path, source_kind,
                          "chunks gained storage coverage before processing")
                continue

            try:
                from backupme.core.uploader import upload_file
                from backupme.backends import get_all_backends
                result = upload_file(
                    source_root=row["source_root"],
                    relative_path=row["relative_path"],
                    local_path=detail["local_path"],
                    backends=get_all_backends(config),
                    config=config,
                    redundancy=1,
                    version_seq=row["version_seq"] or 1,
                    version_group=row["version_group"],
                )
            except Exception as e:
                log.warning("reback upload failed for file %s: %s", file_id, e)
                failures.append({"file_id": file_id, "reason": "UPLOAD_FAILED",
                                 "error": str(e)})
                continue  # not checkpointed — retried on the next run

            if result.get("status") not in ("ok", "partial"):
                error = result.get("error") or json.dumps(result, default=str)
                log.warning("reback upload rejected for file %s: %s", file_id, error)
                failures.append({"file_id": file_id, "reason": "UPLOAD_FAILED",
                                 "error": error})
                continue

            ok, verify_error = _verify_healed(file_id, row["sha256"])
            if not ok:
                log.warning("reback verify failed for file %s: %s", file_id, verify_error)
                failures.append({"file_id": file_id, "reason": "VERIFY_FAILED",
                                 "error": verify_error})
                continue

            healed += 1
            ck["healed"][str(file_id)] = {
                "path": path, "sha256": row["sha256"], "source_kind": source_kind,
            }
            record_sample(file_id, "healed", path, source_kind)
            if processed % _CHECKPOINT_EVERY == 0:
                _save_checkpoint(ck_path, ck)
            if processed % _PROGRESS_EVERY == 0:
                progress(f"reback: processed {processed}/{min(total, limit or total)}"
                         f" healed {healed} skipped {skipped} failed {len(failures)}")

        if not dry_run:
            _save_checkpoint(ck_path, ck)

        refcounts_reconciled = 0
        if not dry_run and healed > 0:
            from backupme.core.dedup import reconcile_refcounts
            refcounts_reconciled = reconcile_refcounts()
            if refcounts_reconciled:
                log.info("reback: reconciled %d ref_count(s) after healing",
                         refcounts_reconciled)
        if op is not None:
            op_log.mark_committed(op["id"])
    except Exception as e:
        if op is not None:
            op_log.mark_failed(op["id"], str(e))
        raise

    elapsed = round(time.monotonic() - started, 2)
    result = {
        "status": "dry-run" if dry_run else "applied",
        "apply": not dry_run,
        "candidates": total,
        "processed": processed,
        "already_processed": already_processed,
        "skip_reasons": skip_reasons,
        "failures": failures,
        "sample": sample,
        "elapsed": elapsed,
        "checkpoint": str(ck_path) if not dry_run else None,
        "refcounts_reconciled": refcounts_reconciled if not dry_run else 0,
    }
    if dry_run:
        result["would_heal"] = healed
        result["would_skip"] = skipped
    else:
        result["healed"] = healed
        result["skipped"] = skipped
    progress(f"reback: done status={result['status']} processed={processed} "
             f"healed={healed} skipped={skipped} failed={len(failures)} "
             f"elapsed={elapsed}s")
    return result

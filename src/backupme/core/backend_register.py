"""Verify-and-register backend objects the DB has no blob_storage rows for
(``backupme backend rescan NAME --register``).

Scenario: a database loss/rewrite (or external tooling filling the backend)
left blob objects on a storage backend that the index no longer knows about.
The objects are content-addressed, so a row may only be registered AFTER
verification: the sha256 of the stored bytes is recorded AND the decompressed
plaintext sha256 must equal the blob_id tail (zstd when the path ends
.zstd, raw bytes otherwise).

Layout candidates per blob (tail == plaintext sha256):
- plain chunk blob:  ``objects/<t2>/<t34>/<tail>`` or ``...<tail>.zstd``
- legacy envelope:   ``<tail>.zstd`` at the backend root
- bundle parent:     ``bundle/<t2>/<t34>/<tail>.zstd`` (or without .zstd)

Safety rules:
- Only REFERENCED top-level blobs (ref_count > 0, blob_id NOT LIKE
  'bundle/%/%/%/%') are planned — unreferenced objects are GC candidates
  and are never resurrected into the index; bundle MEMBER rows
  (5-part ``bundle/a/b/tar/mem`` ids, ``path@offset:len`` storage) are
  ride-along metadata that need no object of their own — registering the
  parent carries them, and NO member rows are ever created here.
- Nothing is ever deleted or overwritten; the only write is
  INSERT OR IGNORE into blob_storage on the scanned backend.
- Resumable: a per-backend checkpoint file records done blob ids and is
  committed every 500 blobs (right AFTER the DB commit, so a crash between
  the two only costs re-verification of uncommitted blobs, never
  double-registration — the blob_storage anti-join is the primary filter,
  the checkpoint is the belt-and-suspenders for the commit window).
  Delete the checkpoint file to force re-verification of "done" blobs that
  later lost their rows again.
"""
import hashlib
import shutil
import tempfile
import time
from pathlib import Path

from backupme.core import compress as compress_mod
from backupme.core import state as state_mod
from backupme.core.database import commit, execute
from backupme.core.flock import path_lock, safe_name
from backupme.log import get_logger

log = get_logger("backend_register")

_CHECKPOINT_EVERY = 500
_CHECKPOINT_KEEP = 1000
_ZSTD_LEVEL = 3


def _checkpoint_file(backend_name: str) -> Path:
    """Per-backend checkpoint — a shared file would make blobs verified for
    backend A be silently skipped when registering backend B."""
    return Path(f"register_checkpoint_{safe_name(backend_name)}.json")


def _load_checkpoint(backend_name: str) -> list[str]:
    ck = _checkpoint_file(backend_name)
    if not ck.exists():
        return []
    data = state_mod.read_json_checkpoint(ck, dict)
    if not data and ck.exists():
        # read_json_checkpoint returns default on corrupt/unreadable files too.
        log.warning("register: unreadable checkpoint %s — starting fresh", ck)
        return []
    done = data.get("done", [])
    return [str(d) for d in done] if isinstance(done, list) else []


def _save_checkpoint(backend_name: str, done: list[str]) -> None:
    ck = _checkpoint_file(backend_name)
    state_mod.write_json_checkpoint(ck, {"done": done[-_CHECKPOINT_KEEP:]})


def planned_blobs(backend_name: str) -> list:
    """Referenced top-level blobs with NO row on this backend, oldest first.

    Mirrors rebalance._top_level_missing: bundle member ids (4-slash
    ``bundle/a/b/tar/mem``) are excluded — they ride their parent object.
    """
    return execute(
        """SELECT b.blob_id, b.pt_size
           FROM blobs b
           WHERE b.ref_count > 0
             AND b.blob_id NOT IN (
                         SELECT blob_id FROM blob_storage WHERE backend_name = ?)
             AND b.blob_id NOT LIKE 'bundle/%/%/%/%'
           ORDER BY b.first_seen ASC, b.blob_id ASC""",
        (backend_name,),
    ).fetchall()


def candidate_paths(blob_id: str) -> list[str]:
    """Content-addressed object paths to probe for one blob, most likely first."""
    tail = blob_id.rsplit("/", 1)[-1]
    t2, t34 = tail[:2], tail[2:4]
    if blob_id.startswith("bundle/"):
        return [
            f"bundle/{t2}/{t34}/{tail}.zstd",
            f"bundle/{t2}/{t34}/{tail}",
            f"{tail}.zstd",  # legacy root envelope
        ]
    return [
        f"objects/{t2}/{t34}/{tail}.zstd",
        f"objects/{t2}/{t34}/{tail}",
        f"{tail}.zstd",  # legacy root envelope
    ]


def _verify_stored(data: bytes, blob_id: str, path: str) -> tuple[str, str | None]:
    """Hash + decompress-verify stored bytes against the blob_id tail.

    Returns (stored_sha256, error). The recorded stored_sha256 is ALWAYS the
    sha of the stored bytes; for zstd paths the PLAINTEXT sha must equal the
    tail, for raw paths the stored sha itself must.
    """
    tail = blob_id.rsplit("/", 1)[-1]
    stored_sha = hashlib.sha256(data).hexdigest()
    if path.endswith(".zstd"):
        try:
            plaintext = compress_mod.decompress(data, "zstd")
        except Exception as e:
            return stored_sha, f"zstd decompress failed: {type(e).__name__}: {e}"
        plain_sha = hashlib.sha256(plaintext).hexdigest()
        if plain_sha.lower() != tail.lower():
            return stored_sha, (
                f"plaintext sha mismatch: got {plain_sha[:12]}, want {tail[:12]}"
            )
        return stored_sha, None
    if stored_sha.lower() != tail.lower():
        return stored_sha, f"stored sha mismatch: got {stored_sha[:12]}, want {tail[:12]}"
    return stored_sha, None


def _verify_one_blob(backend, blob_id: str) -> tuple[str, str | None, dict | None]:
    """Probe the backend for one blob's candidate paths and verify.

    Returns (outcome, reason, info): outcome is 'ok' (info carries the row
    fields), 'failed' (reason explains why every existing candidate was
    rejected) or 'not_found' (no candidate path exists on the backend).
    """
    attempts: list[dict] = []
    for path in candidate_paths(blob_id):
        try:
            present = backend.exists(path)
        except Exception as e:
            # exists() is only a cheap skip; a flaky implementation must not
            # hide a downloadable object — fall through to the download try.
            log.warning("register: exists(%s) failed on %s: %s", path, backend.name, e)
            present = True
        if not present:
            continue
        tmp_dir = Path(tempfile.mkdtemp(prefix="bkreg_"))
        tmp = tmp_dir / "blob.bin"
        try:
            backend.download(path, str(tmp))
            if not tmp.exists() or tmp.stat().st_size == 0:
                attempts.append({"path": path, "error": "download produced an empty/missing file"})
                continue
            data = tmp.read_bytes()
        except Exception as e:
            attempts.append({"path": path, "error": f"download failed: {type(e).__name__}: {e}"})
            continue
        finally:
            shutil.rmtree(str(tmp_dir), ignore_errors=True)

        is_zstd = path.endswith(".zstd")
        stored_sha, err = _verify_stored(data, blob_id, path)
        if err:
            attempts.append({"path": path, "error": err})
            continue
        info = {
            "storage_path": path,
            "stored_size": len(data),
            "stored_sha256": stored_sha,
            "compression_algo": "zstd" if is_zstd else None,
            "compression_level": _ZSTD_LEVEL if is_zstd else None,
        }
        return "ok", None, info
    if attempts:
        reason = "; ".join(f"{a['path']}: {a['error']}" for a in attempts)
        return "failed", reason, None
    return "not_found", None, None


def register_backend_objects(backend, config, dry_run: bool = False,
                             limit: int | None = None, progress_cb=None) -> dict:
    """Verify and register this backend's orphaned objects into blob_storage.

    Honest report dict: planned (blobs attempted this run), registered,
    would_register (dry-run only), failed (with reasons), not_found,
    skipped_checkpoint, elapsed. Never deletes anything; in dry-run mode
    nothing is written at all (no DB rows, no checkpoint).
    """
    started = time.monotonic()
    report: dict = {
        "status": "ok",
        "backend": backend.name,
        "dry_run": bool(dry_run),
        "planned": 0,
        "registered": 0,
        "would_register": 0,
        "bundle_parents": 0,
        "failed": [],
        "not_found": [],
        "skipped_checkpoint": 0,
        "elapsed": 0.0,
    }

    # --- connect -----------------------------------------------------------
    if not backend.connected:
        try:
            if not backend.connect():
                report["status"] = "error"
                report["error"] = f"backend {backend.name!r} failed to connect"
                report["elapsed"] = round(time.monotonic() - started, 3)
                return report
        except Exception as e:
            report["status"] = "error"
            report["error"] = f"backend {backend.name!r} connect failed: {e}"
            report["elapsed"] = round(time.monotonic() - started, 3)
            return report

    # --- plan --------------------------------------------------------------
    all_rows = planned_blobs(backend.name)
    done: list[str] = []
    if not dry_run:
        done = _load_checkpoint(backend.name)
        done_set = set(done)
    else:
        done_set = set()
    rows = [r for r in all_rows if r["blob_id"] not in done_set]
    report["skipped_checkpoint"] = len(all_rows) - len(rows)
    if limit:
        rows = rows[: int(limit)]

    report["planned"] = len(rows)
    if not rows:
        report["elapsed"] = round(time.monotonic() - started, 3)
        return report

    if dry_run:
        return _register_dry_run(backend, rows, report, progress_cb, started)

    # --- serialized run (a web + CLI double-run would clobber the ckpt) ----
    ck_file = _checkpoint_file(backend.name)
    try:
        with path_lock(ck_file.with_suffix(".lock"), stale_seconds=3600.0, wait=0.0):
            return _register_locked(backend, rows, report, done,
                                    progress_cb, started)
    except TimeoutError:
        log.warning("another register run for %s is running", backend.name)
        report["status"] = "busy"
        report["error"] = f"another register run for {backend.name!r} is running"
        report["elapsed"] = round(time.monotonic() - started, 3)
        return report


def _register_dry_run(backend, rows, report, progress_cb, started) -> dict:
    """Verify-only pass: identical acceptance criteria, zero writes."""
    for i, row in enumerate(rows, 1):
        blob_id = row["blob_id"]
        outcome, reason, _info = _verify_one_blob(backend, blob_id)
        if outcome == "ok":
            report["would_register"] += 1
            if blob_id.startswith("bundle/"):
                report["bundle_parents"] += 1
        elif outcome == "failed":
            report["failed"].append({"blob_id": blob_id, "reason": reason})
            log.warning("register dry-run: would REJECT %s on %s: %s",
                        blob_id, backend.name, reason)
        else:
            report["not_found"].append({"blob_id": blob_id})
            log.warning("register dry-run: %s on %s: no candidate object found",
                        blob_id, backend.name)
        if progress_cb is not None:
            progress_cb(blob_id, i, len(rows))
        if i % _CHECKPOINT_EVERY == 0:
            log.info("register dry-run: %d/%d verified on %s",
                     i, len(rows), backend.name)
    if report["failed"] or report["not_found"]:
        report["status"] = "partial"
    report["elapsed"] = round(time.monotonic() - started, 3)
    return report


def _register_locked(backend, rows, report, done, progress_cb, started) -> dict:
    """The write pass: verify, INSERT OR IGNORE, commit + checkpoint every
    _CHECKPOINT_EVERY blobs (commit first, checkpoint second — a crash in
    between only re-verifies blobs whose commit already landed, which the
    anti-join filters out on the next run)."""
    backend_name = backend.name
    processed = 0
    for row in rows:
        blob_id = row["blob_id"]
        try:
            outcome, reason, info = _verify_one_blob(backend, blob_id)
        except Exception as e:  # per-blob isolation, keep going
            outcome, reason, info = "failed", f"{type(e).__name__}: {e}", None

        if outcome == "ok":
            execute(
                "INSERT OR IGNORE INTO blob_storage "
                "(blob_id, backend_name, storage_path, stored_size, stored_sha256, "
                " compression_algo, compression_level, encrypted, verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))",
                (blob_id, backend_name, info["storage_path"], info["stored_size"],
                 info["stored_sha256"], info["compression_algo"],
                 info["compression_level"]),
            )
            report["registered"] += 1
            if blob_id.startswith("bundle/"):
                # Bundle member rows (storage_path '...@offset:len') are
                # ride-along metadata — they need NO objects and get NO rows;
                # the registered parent carries their content.
                report["bundle_parents"] += 1
            done.append(blob_id)
        elif outcome == "failed":
            report["failed"].append({"blob_id": blob_id, "reason": reason})
            log.warning("register: REJECTED %s on %s: %s", blob_id, backend_name, reason)
        else:
            report["not_found"].append({"blob_id": blob_id})
            log.warning("register: %s on %s: no candidate object found",
                        blob_id, backend_name)

        processed += 1
        if progress_cb is not None:
            progress_cb(blob_id, processed, len(rows))
        if processed % _CHECKPOINT_EVERY == 0:
            log.info("register: %d/%d processed on %s (registered=%d failed=%d)",
                     processed, len(rows), backend_name, report["registered"],
                     len(report["failed"]))
            commit()
            _save_checkpoint(backend_name, done)

    commit()
    _save_checkpoint(backend_name, done)
    if report["failed"] or report["not_found"]:
        report["status"] = "partial"
    report["elapsed"] = round(time.monotonic() - started, 3)
    log.info("register done on %s: planned=%d registered=%d failed=%d not_found=%d "
             "elapsed=%.1fs", backend_name, report["planned"], report["registered"],
             len(report["failed"]), len(report["not_found"]), report["elapsed"])
    return report

"""Serial files-ingest helper: download files from a backend one at a time,
process each through the real pipeline (dedup/compress/chunk/register), and
store the processed result back — via converter.convert_backend.

Used by both the CLI (`backupme process-files`) and the dashboard
(POST /api/backends/{name}/process-files). Single-flight per backend via a
path lock; op_log audited; progress written to files_ingest_progress.json so
any monitor can read live state.
"""
import json
import time
import uuid
from pathlib import Path

from backupme.core.flock import path_lock, safe_name
from backupme.core.op_log import append as op_append
from backupme.core.op_log import mark_committed, mark_failed
from backupme.log import get_logger

log = get_logger("files_ingest")

PROGRESS_FILE = "files_ingest_progress.json"


def _write_progress(payload: dict) -> None:
    try:
        tmp = Path(PROGRESS_FILE + ".tmp")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        tmp.replace(Path(PROGRESS_FILE))
    except OSError:
        pass


def ingest_files(
    backend_name: str,
    config,
    prefix: str = "",
    target_backend_name: str | None = None,
    delete: bool = False,
    progress_cb=None,
) -> dict:
    """Process a backend's loose files through the pipeline, serially.

    Returns an honest report dict; status 'busy' when another ingest on the
    same backend is already running. Never deletes anything unless
    delete=True, and converter only deletes a source file after its
    processed replacement is stored and replicated.
    """
    from backupme.core.converter import convert_backend

    lock_path = Path(f"files_ingest_{safe_name(backend_name)}.lock")
    with path_lock(lock_path, stale_seconds=3600, wait=0):
        op = op_append(
            "process_files",
            {"backend": backend_name, "prefix": prefix,
             "target": target_backend_name, "delete": delete},
            idempotency_key=f"process_files:{backend_name}:{uuid.uuid4().hex}",
        )
        log.info("ingest start: backend=%s prefix=%r target=%s delete=%s op=%s",
                 backend_name, prefix, target_backend_name, delete, op["id"])
        set_progress = lambda **kw: _write_progress({  # noqa: E731
            "backend": backend_name, "prefix": prefix,
            "target": target_backend_name, "delete": delete,
            "op_id": op["id"], "started_epoch": t0, **kw})

        t0 = time.time()

        def cb(done, skipped, errors, total):
            set_progress(done=done, skipped=skipped, errors=errors, total=total)
            if progress_cb is not None:
                try:
                    progress_cb(done, skipped, errors, total)
                except Exception:
                    pass

        set_progress(running=True, done=0, skipped=0, errors=0, total=0)
        try:
            result = convert_backend(
                backend_name=backend_name,
                prefix=prefix,
                target_backend_name=target_backend_name,
                delete=delete,
                resume=True,
                progress_cb=cb,
            )
            result["status"] = "ok"
            result["op_id"] = op["id"]
            result["elapsed_min"] = round((time.time() - t0) / 60, 2)
            set_progress(running=False, completed=True, result=result)
            try:
                mark_committed(op["id"])
            except Exception:
                log.warning("op_log mark_committed failed for op %s", op.get("id"))
            log.info("ingest done: %s", result)
            return result
        except Exception as e:
            result = {"status": "error", "error": f"{type(e).__name__}: {e}",
                      "op_id": op["id"]}
            set_progress(running=False, completed=True, result=result)
            try:
                mark_failed(op["id"], result["error"])
            except Exception:
                log.warning("op_log mark_failed failed for op %s", op["id"])
            log.error("ingest failed: %s", result["error"])
            return result

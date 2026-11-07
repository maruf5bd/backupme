"""cPanel cpmove ingest engine.

Streams members of ``cpmove-*.tar.gz`` archives found under a drop root
through the standard dedup pipeline (upload_bundle), keeping plaintext in a
staging dir only for the lifetime of a single member upload. Per-archive
op_log idempotency, checkpoints, raw-archive retention input, restore (loose
files or retar'd archive) and dry-run estimation live here.
"""
import dataclasses
import fnmatch
import json
import os
import re
import shutil
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from backupme.log import get_logger

log = get_logger("cpanel_ingest")

from backupme.core import hasher as hasher_mod
from backupme.core import index as index_mod
from backupme.core import op_log as op_log_mod
from backupme.core import state as state_mod
from backupme.core.archives import sanitize_member
from backupme.core.database import execute
from backupme.core.uploader import upload_bundle, upload_file

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")
_CPMOVE_NAME_RE = re.compile(r"^cpmove-(\w+)\.tar\.(?:gz|bz2)$", re.IGNORECASE)
_ACCOUNT_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")
# Windows reserved device names are invalid as directory names — an account
# derived from e.g. "cpmove-CON.tar.gz" would make the .done mkdir fail.
_WINDOWS_RESERVED_RE = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
_CHECKPOINT_EVERY = 25

# Ingest failure notification hooks (Plan 05): failures are always recorded in
# op_log; registered hooks additionally receive every archive failure (used by
# the notification config path if a dispatcher is configured).
_FAILURE_HOOKS: list = []


def register_failure_hook(fn) -> None:
    """Register a callable ``fn(details: dict)`` invoked on archive failures."""
    if fn not in _FAILURE_HOOKS:
        _FAILURE_HOOKS.append(fn)


def _notify_failure(details: dict) -> None:
    for fn in list(_FAILURE_HOOKS):
        try:
            fn(details)
        except Exception:
            log.warning("ingest failure hook raised", exc_info=True)


def _sanitize_account(name: str) -> str:
    """Keep [A-Za-z0-9._-]; strip leading/trailing separators; fall back to default.

    Windows reserved device names (CON, NUL, COM1, ...) are rejected so a
    derived account can never be an invalid directory name."""
    cleaned = _ACCOUNT_SANITIZE_RE.sub("_", name).strip("_-. ")
    if not cleaned or _WINDOWS_RESERVED_RE.match(cleaned):
        return "default"
    return cleaned


def _account_from_relpath(rel_parts: list[str], filename: str, pattern: str | None) -> str:
    # (a) configured regex, first group or whole match, applied to rel path
    if pattern:
        try:
            m = re.search(pattern, "/".join(rel_parts))
            if m:
                return _sanitize_account(m.group(1) if m.groups() else m.group(0))
        except re.error:
            log.warning("invalid account_pattern %r — ignoring", pattern)
    # (b) first relative subfolder under the drop root
    if len(rel_parts) > 1 and rel_parts[0]:
        return _sanitize_account(rel_parts[0])
    # (c) cpmove-<acct>.(tar.)?(gz|bz2) style file name
    m = _CPMOVE_NAME_RE.match(filename)
    if m:
        return _sanitize_account(m.group(1))
    # (d) fallback
    return "default"


def discover(drop_root: str) -> list[dict]:
    """Recursively list ingestible items under drop_root.

    Archives (.tar.gz/.tgz) become ``{"path", "account", "type": "archive"}``;
    every other file is listed with ``type == "file"`` (queued but not processed
    by ingest_tree — reported as files_ignored). Dot-prefixed path components
    (incl. ``.done``) and anything under a ``staging`` dir are skipped.
    """
    from backupme.config import load_config

    try:
        cfg = load_config()
        pattern = getattr(cfg.cpanel_ingest, "account_pattern", "") or ""
    except Exception:
        pattern = ""

    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(drop_root):
        rel_dir = os.path.relpath(dirpath, drop_root)
        parts = [] if rel_dir == "." else rel_dir.replace("\\", "/").split("/")
        # Prune hidden dirs and staging dirs in place so os.walk skips them.
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d != "staging"
        ]
        if any(p.startswith(".") or p == "staging" for p in parts):
            continue
        for fname in filenames:
            fparts = parts + [fname]
            if fname.startswith("."):
                continue
            full = os.path.join(dirpath, fname)
            account = _account_from_relpath(fparts, fname, pattern)
            if fname.endswith(ARCHIVE_SUFFIXES):
                out.append({"path": os.path.abspath(full), "account": account,
                            "type": "archive"})
            else:
                out.append({"path": os.path.abspath(full), "account": account,
                            "type": "file"})
    return out


def estimate_archive(archive_path: str) -> dict:
    """Cheap pre-pass WITHOUT extracting anything.

    members/bytes count only regular files that would survive ingest (safe
    names); skipped counts non-regular entries (symlinks, dirs, devices).
    Unsafe-named regular members are excluded from members (never ingestible)
    but not tallied as skipped — the engine warns on them during ingest.
    """
    members = 0
    total_bytes = 0
    skipped = 0
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar:
            if member.isfile():
                if sanitize_member(member.name) is not None:
                    members += 1
                    total_bytes += member.size
            else:
                skipped += 1
    return {"members": members, "bytes": total_bytes, "skipped": skipped}


# ---------------------------------------------------------------------------
# Staging budget gate / bandwidth pacing (Phase 3)
# ---------------------------------------------------------------------------

_PACE_LOCK = threading.Lock()
_PACE_STATE = {"tokens": 0.0, "last": 0.0}  # tokens in bytes


def check_staging_budget(config) -> bool:
    """Refuse new archives when the plaintext staging dir exceeds its budget."""
    cfg = config if isinstance(config, dict) else dataclasses.asdict(config)
    ingest_cfg = _ingest_cfg(cfg)
    budget_gb = float(ingest_cfg.get("staging_budget_gb", 10.0) or 10.0)
    if budget_gb <= 0:
        return True
    staging_root = Path(ingest_cfg.get("staging_dir", "staging") or "staging")
    if not staging_root.exists():
        return True
    used = 0
    for dirpath, _dirnames, filenames in os.walk(staging_root):
        for fname in filenames:
            try:
                used += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                continue
    ok = used < budget_gb * (1024 ** 3)
    if not ok:
        log.warning("staging budget exceeded: %d bytes >= %.1f GB — deferring archives",
                    used, budget_gb)
    return ok


def pace() -> None:
    """Token-bucket bandwidth pacing (bandwidth_limit_mbps; 0 = unlimited)."""
    cfg = None
    try:
        from backupme.config import load_config
        cfg = load_config()
    except Exception:
        return
    ingest_cfg = getattr(cfg, "cpanel_ingest", None)
    mbps = float(getattr(ingest_cfg, "bandwidth_limit_mbps", 0.0) or 0.0) if ingest_cfg else 0.0
    if mbps <= 0:
        return

    with _PACE_LOCK:
        now = time.monotonic()
        last = _PACE_STATE["last"] or now
        rate = mbps * (1024 * 1024) / 8.0  # bytes per second
        capacity = max(rate, rate * 1.0)  # 1s burst bucket
        _PACE_STATE["tokens"] = min(capacity, _PACE_STATE["tokens"] + rate * (now - last))
        _PACE_STATE["last"] = now
        if _PACE_STATE["tokens"] < rate:
            deficit = (rate - _PACE_STATE["tokens"]) / rate
            wait = min(deficit, 1.0)
            if wait > 0:
                time.sleep(wait)
        _PACE_STATE["tokens"] = max(0.0, _PACE_STATE["tokens"] - rate)


# ---------------------------------------------------------------------------
# Core ingest
# ---------------------------------------------------------------------------

def _as_config_dict(config) -> dict:
    if isinstance(config, dict):
        return config
    return dataclasses.asdict(config)


def _ingest_cfg(config_dict: dict) -> dict:
    ing = config_dict.get("cpanel_ingest", {}) or {}
    if ing and hasattr(ing, "get"):
        return ing
    if dataclasses.is_dataclass(ing):
        return dataclasses.asdict(ing)
    return dict(ing) if ing else {}


def _extract_member_to_staging(tar, member, staging_dir: Path, member_name: str) -> Path:
    """Extractfile + manual write — NEVER tar.extract (perms/setuid/traversal)."""
    dest = staging_dir / member_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = tar.extractfile(member)
    if src is None:
        raise RuntimeError(f"member {member_name!r} has no data")
    with open(dest, "wb") as fout:
        shutil.copyfileobj(src, fout, length=1024 * 1024)
    return dest


def _member_allowed(sanitized: str, exclude_globs: list[str]) -> bool:
    """True unless any glob matches the full sanitized path or its basename."""
    base = sanitized.rsplit("/", 1)[-1]
    for glob in exclude_globs:
        if fnmatch.fnmatch(sanitized, glob) or fnmatch.fnmatch(base, glob):
            return False
    return True


def _write_generation_manifest(env_dir: Path, archive_sha256: str, payload: dict) -> Path:
    manifest_dir = env_dir / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / f"{archive_sha256}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def ingest_archive(
    archive_path: str,
    account: str,
    backends: list,
    config,
    redundancy: int = 1,
    progress_cb=None,
    drop_root: str | None = None,
) -> dict:
    """Ingest one cpmove archive through the dedup pipeline.

    Members are staged one at a time (deleted immediately after upload), a
    small checkpoint is written every 25 members, a generation manifest is
    written on success and the archive is moved to drop_root/.done/<account>/.
    """
    config_dict = _as_config_dict(config)
    ingest_cfg = _ingest_cfg(config_dict)
    cwd = Path.cwd()

    # --- Idempotency -------------------------------------------------------
    archive_path = str(Path(archive_path).resolve())
    archive_sha256 = hasher_mod.sha256_file(archive_path)
    ik = f"cpanel_ingest:{archive_sha256}"
    existing = execute(
        "SELECT target_json FROM op_log WHERE idempotency_key = ? AND status = 'committed'",
        (ik,),
    ).fetchone()
    if existing:
        return {"status": "skipped", "reason": "already ingested",
                "archive": archive_path, "account": account}

    op = op_log_mod.append(
        "cpanel_ingest",
        target={"archive": archive_path, "account": account,
                "sha256": archive_sha256},
        idempotency_key=ik,
    )

    staging_root = Path(ingest_cfg.get("staging_dir", "staging"))
    staging_dir = staging_root / archive_sha256[:12]

    done_members: list[dict] = []
    excluded = 0
    oversize_skipped = 0
    error_count = 0
    dupe_count = 0
    total = 0
    est: dict = {"members": 0, "bytes": 0, "skipped": 0}

    try:
        est = estimate_archive(archive_path)
        total = est["members"]
        staging_dir.mkdir(parents=True, exist_ok=True)

        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar:
                if not member.isfile() or member.issym() or member.islnk() or member.isdev():
                    continue
                sanitized = sanitize_member(member.name)
                if sanitized is None:
                    log.warning("rejecting unsafe member name %r in %s",
                                member.name, archive_path)
                    continue
                globs = ingest_cfg.get("exclude_members", []) or []
                if not _member_allowed(sanitized, globs):
                    excluded += 1
                    continue
                max_gb = float(ingest_cfg.get("max_member_size_gb", 5.0) or 5.0)
                if max_gb > 0 and member.size > max_gb * (1024 ** 3):
                    log.warning("skipping oversize member %s (%d bytes)",
                                sanitized, member.size)
                    oversize_skipped += 1
                    continue

                staging_path = None
                try:
                    staging_path = _extract_member_to_staging(
                        tar, member, staging_dir, sanitized)
                    file_info = {
                        "local_path": str(staging_path),
                        "source_root": f"cpanel/{account}",
                        "relative_path": sanitized,
                        "version_group": f"{account}|{sanitized}",
                        "version_seq": index_mod.next_version_seq(
                            f"cpanel/{account}", sanitized),
                    }
                    results = upload_bundle(
                        [file_info], backends, config_dict, redundancy)
                    res = results[0] if results else {"status": "error"}
                    if res.get("status") in ("error",):
                        error_count += 1
                    elif res.get("dedup"):
                        dupe_count += 1
                    done_members.append({
                        "name": sanitized,
                        "sha256": hasher_mod.sha256_file(str(staging_path))
                        if staging_path.exists() else res.get("sha256", ""),
                        "version_seq": file_info["version_seq"],
                        "file_id": res.get("file_id"),
                    })
                finally:
                    # Never leak plaintext: staging file goes away even on error.
                    if staging_path is not None:
                        try:
                            staging_path.unlink(missing_ok=True)
                        except OSError:
                            log.warning("could not remove staging file %s",
                                        staging_path, exc_info=True)

                if progress_cb is not None:
                    progress_cb(sanitized, len(done_members), total)

                if len(done_members) % _CHECKPOINT_EVERY == 0:
                    state_mod.save_checkpoint("cpanel_ingest", {
                        "archive": archive_path,
                        "sha256": archive_sha256,
                        "account": account,
                        "members": done_members[-1:],
                        "done": len(done_members),
                        "total": total,
                    })

        if len(done_members) != est["members"]:
            log.warning(
                "archive %s: indexed %d members, estimated %d "
                "(excluded %d, oversize skipped %d)",
                archive_path, len(done_members), est["members"], excluded,
                oversize_skipped)

        if total > 0 and error_count == total:
            # Every member failed to upload (backend outage). Committing the
            # op and moving the archive to .done would strand its data behind
            # the idempotency skip forever (qa P1). Raise into the failure
            # path: op marked failed, archive left in the drop root for a
            # retry — dedup makes the retry cheap for any member that landed.
            raise RuntimeError(
                f"all {total} member(s) of {archive_path} failed to upload "
                "(backend outage) — archive left in place for retry")

        payload = {
            "account": account,
            "archive": archive_path,
            "archive_sha256": archive_sha256,
            "members": done_members,
            "indexed": len(done_members),
            "estimated": est["members"],
            "excluded": excluded,
            "skipped_oversize": oversize_skipped,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_generation_manifest(cwd, archive_sha256, payload)

        state_mod.save_checkpoint("cpanel_ingest", {
            "archive": archive_path,
            "sha256": archive_sha256,
            "account": account,
            "members": done_members[-1:],
            "done": len(done_members),
            "total": total,
        })

        op_log_mod.mark_committed(op["id"])
    except Exception as e:
        log.error("ingest_archive failed for %s: %s", archive_path, e,
                  exc_info=True)
        try:
            op_log_mod.mark_failed(op["id"], str(e))
        except Exception:
            log.error("could not mark op failed", exc_info=True)
        _notify_failure({"archive": archive_path, "account": account,
                         "error": str(e), "sha256": archive_sha256})
        try:
            from backupme.core import notify
            notify.notify_failure("cPanel ingest failure",
                                  f"{archive_path}: {e}", config)
        except Exception:
            log.warning("ingest failure notification could not be dispatched",
                        exc_info=True)
        return {"status": "failed", "error": str(e),
                "account": account, "archive": archive_path}

    # --- Post-commit disposition (raw upload + .done move) -----------------
    # The op is already committed: a disposition failure must NOT turn the
    # result into "failed" (that would desync op_log vs worker status and,
    # via the idempotency skip, strand the archive in the drop root forever).
    disposition_errors: list[str] = []

    keep_raw = int(ingest_cfg.get("keep_raw", 0) or 0)
    if keep_raw > 0:
        try:
            raw_backends = backends
            raw_backend_name = ingest_cfg.get("raw_backend", "") or ""
            if raw_backend_name:
                matching = [b for b in backends if b.name == raw_backend_name]
                if matching:
                    raw_backends = matching
                else:
                    log.warning("raw_backend %s not found — using all backends",
                                raw_backend_name)
            basename = os.path.basename(archive_path)
            upload_file(
                source_root=f"cpanel-raw/{account}",
                relative_path=basename,
                local_path=archive_path,
                backends=raw_backends,
                config=config_dict,
                redundancy=redundancy,
                version_seq=index_mod.next_version_seq(
                    f"cpanel-raw/{account}", basename),
            )
        except Exception as e:
            # Raw copy is best-effort: the ingested generation stays valid.
            log.error("raw archive upload failed for %s: %s",
                      archive_path, e, exc_info=True)
            disposition_errors.append(f"raw upload: {e}")

    try:
        done_base = drop_root or _guess_drop_root(archive_path, account)
        done_dir = Path(done_base) / ".done" / account
        done_dir.mkdir(parents=True, exist_ok=True)
        target = done_dir / Path(archive_path).name
        if target.exists():
            target.unlink()
        shutil.move(archive_path, str(target))
    except Exception as e:
        log.error("could not move %s to .done: %s", archive_path, e,
                  exc_info=True)
        from backupme.core.database import execute as _exec, commit as _commit
        try:
            _exec(
                "INSERT INTO recovery_events (event_type, status, details) VALUES (?, ?, ?)",
                ("ingest_disposition", "failed",
                 f"archive {archive_path} ingested (op committed) but could not "
                 f"be moved to .done: {e}"),
            )
            _commit()
        except Exception:
            log.error("could not record recovery event", exc_info=True)
        disposition_errors.append(f".done move: {e}")

    return {
        "status": "ok",
        "account": account,
        "archive": archive_path,
        "members": len(done_members),
        "bytes": est["bytes"],
        "dupe": dupe_count,
        "errors": error_count,
        "excluded": excluded,
        "skipped_oversize": oversize_skipped,
        "disposition_errors": disposition_errors,
    }


def _guess_drop_root(archive_path: str, account: str) -> str:
    """Best-effort drop root for the .done move when discover() didn't supply one.

    Uses the config drop_root when the archive lives under it; otherwise the
    archive's parent dir when it is not itself the account dir, else the
    grandparent (archive directly in the drop root)."""
    from backupme.config import load_config
    try:
        cfg = load_config()
        configured = getattr(cfg.cpanel_ingest, "drop_root", "") or ""
    except Exception:
        configured = ""
    apath = Path(archive_path).resolve()
    if configured:
        try:
            apath.relative_to(Path(configured).resolve())
            return str(Path(configured).resolve())
        except ValueError:
            pass
    parent = apath.parent
    if parent.name == account and parent.parent != parent:
        return str(parent.parent)
    return str(parent)


def ingest_tree(
    drop_root: str,
    backends: list,
    config,
    redundancy: int = 1,
    progress_cb=None,
) -> dict:
    """Discover archives under drop_root and ingest them in parallel.

    Serialized across processes AND components (web worker vs daemon tick)
    with an exclusive gate file under the staging root — two runners would
    otherwise ingest the same archive into the same staging dir and race
    version_seq allocation."""
    config_dict = _as_config_dict(config)
    ingest_cfg = _ingest_cfg(config_dict)
    staging_root = Path(ingest_cfg.get("staging_dir", "staging") or "staging")

    from backupme.core.flock import path_lock
    try:
        with path_lock(staging_root / ".ingest.lock", stale_seconds=3600.0, wait=0.0):
            return _ingest_tree_locked(drop_root, backends, config, config_dict,
                                       ingest_cfg, redundancy, progress_cb)
    except TimeoutError:
        log.warning("another ingest scan is running — skipping this tick")
        return {"status": "busy", "reason": "another ingest scan is running",
                "archives": 0, "results": [], "files_ignored": 0}


def _ingest_tree_locked(drop_root, backends, config, config_dict, ingest_cfg,
                        redundancy, progress_cb) -> dict:
    discovered = discover(str(drop_root))
    archives = [d for d in discovered if d["type"] == "archive"]
    files_ignored = sum(1 for d in discovered if d["type"] == "file")

    if not archives:
        return {"status": "idle", "archives": 0, "results": [],
                "files_ignored": files_ignored}

    max_parallel = int(ingest_cfg.get("max_parallel", 2) or 2)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        futures = {}
        for item in archives:
            if not check_staging_budget(config):
                results.append({"status": "deferred",
                                "reason": "staging budget",
                                "archive": item["path"],
                                "account": item["account"]})
                continue
            futures[pool.submit(
                ingest_archive, item["path"], item["account"],
                backends, config, redundancy, progress_cb, str(drop_root),
            )] = item
        for fut, item in futures.items():
            try:
                pace()
                results.append(fut.result())
            except Exception as e:
                # ingest_archive catches its own errors; this is a last resort.
                log.error("ingest worker crashed for %s: %s",
                          item["path"], e, exc_info=True)
                results.append({"status": "failed", "error": str(e),
                                "account": item["account"],
                                "archive": item["path"]})

    bad = all(r.get("status") in ("ok", "skipped", "idle") for r in results)
    return {
        "status": "ok" if bad else "partial",
        "archives": len(archives),
        "results": results,
        "files_ignored": files_ignored,
    }


def restore_cpanel(
    account: str,
    generation: int,
    member_filter: str | None = None,
    out: str = "restore_out",
    retar: str | None = None,
    backends=None,
    config=None,
) -> dict:
    """Restore generation ``generation`` of a cPanel account's ingested files.

    Downloads each member to ``out/<account>/<relative_path>``; optionally
    writes a tar.gz (``retar``) containing the members under their original
    archive member names.
    """
    from backupme.core.downloader import download_file

    source_root = f"cpanel/{account}"
    rows = execute(
        "SELECT id, relative_path, version_seq FROM files "
        "WHERE source_root = ? AND version_seq = ? AND deleted_at IS NULL",
        (source_root, generation),
    ).fetchall()

    if not rows:
        # Unknown account or generation: scripts must be able to detect a
        # failed restore, so this is an error (CLI exits 1), not ok/0.
        return {
            "status": "error",
            "error": f"no ingested generation {generation} for account '{account}'",
            "account": account,
            "generation": generation,
            "members": 0,
            "out": str(Path(out) / account),
            "detail": [],
        }

    if member_filter:
        rows = [r for r in rows if fnmatch.fnmatch(r["relative_path"], member_filter)]

    out_base = Path(out) / account
    restored: list[dict] = []
    for row in rows:
        rel = row["relative_path"].replace("/", os.sep)
        # Defense in depth: ingest sanitizes member names, but a poisoned or
        # legacy index row must not write outside the restore root.
        p = Path(rel)
        if ".." in p.parts or p.is_absolute() or (len(rel) >= 2 and rel[1] == ":"):
            restored.append({"relative_path": row["relative_path"],
                             "status": "error",
                             "error": "unsafe member path rejected"})
            continue
        dest = out_base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            download_file(row["id"], str(dest), version=generation)
            restored.append({"path": str(dest),
                             "relative_path": row["relative_path"],
                             "status": "ok"})
        except Exception as e:
            log.error("restore failed for %s: %s", row["relative_path"], e,
                      exc_info=True)
            restored.append({"path": str(dest),
                             "relative_path": row["relative_path"],
                             "status": "error", "error": str(e)})

    result = {
        "status": "ok" if all(r["status"] == "ok" for r in restored) else "partial",
        "account": account,
        "generation": generation,
        "members": len([r for r in restored if r["status"] == "ok"]),
        "out": str(out_base),
        "detail": restored,
    }

    if retar:
        retar_path = Path(retar)
        retar_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(retar_path, "w:gz") as tar:
            for item in restored:
                if item["status"] != "ok":
                    continue
                p = Path(item["path"])
                tar.add(p, arcname=item["relative_path"])
        result["retar"] = str(retar_path)

    return result

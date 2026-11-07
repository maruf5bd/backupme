"""Extract-first archive ingest (Phase 1).

BackupMe never stores a recognized ARCHIVE file (tar, tar.gz, tar.bz2,
tar.xz, zip — detected by magic bytes) as an opaque blob. Instead the archive
is extracted, every member is hashed and uploaded through the REAL pipeline
(upload_bundle) so members dedup against the whole existing index, and the
archive is recorded as a VIRTUAL manifest:

- Archive identity is a FINGERPRINT: sha256 over the sorted member manifest
  (each line ``relative_path\\0member_sha256\\0size\\n``, UTF-8). Identical
  content twice → identical fingerprint → archive-level dedup (no-op).
- Members live as normal ``files`` rows under ``source_root =
  archive/<fingerprint>`` with the path inside the archive as
  ``relative_path`` — so search, browse, per-file restore, verify and FTS
  work on members with zero new code.
- NESTED archives (magic bytes) are extracted recursively: each gets its own
  fingerprint/archives row and appears in the parent's manifest with a
  ``nested_archive`` field. The nested archive file itself is never stored.
- Caps: depth <= MAX_DEPTH, <= MAX_MEMBERS members per archive, <=
  MAX_TOTAL_UNCOMPRESSED uncompressed bytes per archive (nested members'
  uncompressed totals count toward the parent's budget).
- FALLBACK RULE (never lose data): corrupt archive, encrypted/password
  archive, unsupported format (rar/7z in Phase 1), any extraction error or
  cap exceeded → the file takes the EXISTING whole-blob path exactly as
  today and the result reports ``stored_whole`` with the reason. For a
  nested member the fallback keeps the nested file's bytes and uploads them
  as a regular member of the parent.
- MANIFEST SIDECAR (DB-loss insurance): after a successful ingest a JSON
  restore recipe is written to EVERY target backend at
  ``manifests/<fp[:2]>/<fp[2:4]>/<fp>.json``. Skipped when encryption is
  enabled (plaintext member names must not leak to backends — mirrors the
  content_text rule); the DB manifest still records everything.
"""
import calendar
import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backupme.log import get_logger

log = get_logger("archives")

from backupme.core.database import execute, write_txn
from backupme.core import index as index_mod
from backupme.core.uploader import upload_bundle, upload_file

# ---------------------------------------------------------------------------
# Caps (module constants so tests can override them)
# ---------------------------------------------------------------------------

MAX_DEPTH = 5
MAX_MEMBERS = 200_000
MAX_TOTAL_UNCOMPRESSED = 20 * 1024 ** 3
MANIFEST_FORMAT_VERSION = 1

_COPY_BLOCK = 1024 * 1024
_MAGIC_PEEK = 262  # enough for the ustar magic at offset 257

_TAR_OPEN_MODE = {
    "tar": "r:",
    "tar.gz": "r:gz",
    "tar.bz2": "r:bz2",
    "tar.xz": "r:xz",
}

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
# Known archive formats WITHOUT extract-first support in Phase 1: a file with
# one of these magics is reported as stored_whole (unsupported) and uploaded
# through the normal whole-blob path.
_UNSUPPORTED_MAGICS = (
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
)


class ArchiveFallback(Exception):
    """The archive cannot be extracted within the rules — store it whole."""


# ---------------------------------------------------------------------------
# Member-name sanitization (shared with cpanel_ingest)
# ---------------------------------------------------------------------------

def sanitize_member(name: str) -> str | None:
    """Return an archive member name safe to write under a staging root.

    Rejects absolute paths, drive letters, traversal (``..``) and empty names.
    Backslashes are normalized to forward slashes first; the ORIGINAL name is
    checked for traversal before normalization so ``..\\..`` cannot smuggle
    through. Returns None for anything rejected.

    (Extracted from cpanel_ingest so both ingest engines share one
    implementation; behavior is identical to the original.)
    """
    import re

    if not name or not name.strip():
        return None
    original = name
    normalized = name.replace("\\", "/").strip()
    if not normalized:
        return None
    # Reject absolute / drive-letter forms on the ORIGINAL and the normalized name.
    if original.startswith(("/", "\\")):
        return None
    stripped = normalized.lstrip("/")
    if stripped == normalized and re.match(r"^[A-Za-z]:", stripped):
        return None
    if re.match(r"^[A-Za-z]:", original):
        return None
    # Traversal check against the ORIGINAL components (both separators).
    for token in original.replace("\\", "/").split("/"):
        if token == "..":
            return None
    # Traversal check post-strip as well (belt and braces).
    for token in stripped.split("/"):
        if token == "..":
            return None
    if not stripped or stripped == ".":
        return None
    parts = [p for p in stripped.split("/") if p not in ("", ".")]
    if not parts:
        return None
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Magic-byte detection
# ---------------------------------------------------------------------------

def _format_from_magic(head: bytes) -> str | None:
    """Archive format suggested by magic bytes alone, or None."""
    if head.startswith(_ZIP_MAGICS):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "tar.gz"
    if head.startswith(b"BZh"):
        return "tar.bz2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "tar.xz"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return None


def _head_of(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(_MAGIC_PEEK)


def detect_archive_format(path: str) -> str | None:
    """Archive format of a file by magic bytes, or None if not an archive.

    gzip/bzip2/xz wrap many non-tar payloads (plain .gz/.bz2/.xz log files are
    ordinary backups), so those formats are only classified as archives when a
    tar stream is actually inside. Zip is classified by magic alone: a
    truncated/corrupt zip must still be recognized as an archive ATTEMPT so it
    takes the whole-blob fallback with an explicit reason (never silently
    stored without a word).
    """
    try:
        head = _head_of(path)
    except OSError:
        return None
    fmt = _format_from_magic(head)
    if fmt in _TAR_OPEN_MODE and fmt != "tar":
        try:
            tarfile.open(path, _TAR_OPEN_MODE[fmt]).close()
        except (tarfile.TarError, EOFError, OSError):
            return None
    return fmt


def unsupported_archive_format(path: str) -> str | None:
    """Name of a KNOWN archive format we cannot extract (rar/7z), else None."""
    try:
        head = _head_of(path)
    except OSError:
        return None
    for magic, name in _UNSUPPORTED_MAGICS:
        if head.startswith(magic):
            return name
    return None


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def compute_fingerprint(entries: list["MemberPlan"]) -> str:
    """sha256 over the sorted member manifest.

    Each line is ``relative_path\\0member_sha256\\0size\\n`` (UTF-8); entries
    are sorted by relative_path. Same archive content twice → same
    fingerprint, regardless of container (tar.gz vs zip).
    """
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e.path):
        digest.update(f"{entry.path}\0{entry.sha256}\0{entry.size}\n".encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Planning (pass 1: hash every member, no uploads)
# ---------------------------------------------------------------------------

@dataclass
class MemberPlan:
    path: str
    sha256: str
    size: int
    mtime: float
    nested_archive: str | None = None   # fingerprint of the nested archive
    staged_path: Path | None = None     # bytes on disk (nested whole-file fallback)
    file_id: int | None = None          # files.id after the upload pass


@dataclass
class ArchivePlan:
    format: str
    entries: list[MemberPlan] = field(default_factory=list)
    unsafe_names: int = 0
    duplicate_names: int = 0
    fingerprint: str = ""
    staging_dirs: list[Path] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        return len(self.entries)

    @property
    def total_uncompressed(self) -> int:
        return sum(e.size for e in self.entries)

    @property
    def nested_count(self) -> int:
        return sum(1 for e in self.entries if e.nested_archive is not None)


@dataclass
class Budget:
    """Running cap counters for one container (nested totals included)."""
    members: int = 0
    bytes: int = 0


@dataclass
class IngestContext:
    """Carries the upload environment through (nested) ingest recursion."""
    backends: list
    config: dict
    redundancy: int = 1
    fallback_upload: bool = True     # whole-blob fallback performed here?
    source_root: str | None = None   # context for the whole-blob fallback
    relative_path: str | None = None


def _check_budget(budget: Budget, add_members: int, add_bytes: int) -> None:
    new_members = budget.members + add_members
    new_bytes = budget.bytes + add_bytes
    if new_members > MAX_MEMBERS:
        raise ArchiveFallback(
            f"member cap exceeded ({new_members} > {MAX_MEMBERS})")
    if new_bytes > MAX_TOTAL_UNCOMPRESSED:
        raise ArchiveFallback(
            f"uncompressed size cap exceeded ({new_bytes} > "
            f"{MAX_TOTAL_UNCOMPRESSED} bytes)")
    budget.members = new_members
    budget.bytes = new_bytes


def _stage_temp(plan: ArchivePlan) -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix="backupme-arch-"))
    plan.staging_dirs.append(tmp_dir)
    return tmp_dir


def _discard_staged(plan: ArchivePlan, staged: Path) -> None:
    """Remove a no-longer-needed staged file and its temp dir."""
    staged.unlink(missing_ok=True)
    plan.staging_dirs[:] = [d for d in plan.staging_dirs if d != staged.parent]
    try:
        staged.parent.rmdir()
    except OSError:
        pass


def _zip_mtime(info: zipfile.ZipInfo) -> float:
    try:
        return float(calendar.timegm(tuple(info.date_time) + (0, 0, -1)))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _plan_member_stream(src, sanitized: str, size: int, mtime: float,
                        depth: int, budget: Budget, plan: ArchivePlan,
                        ctx: IngestContext) -> MemberPlan:
    """Hash one member's stream; recurse into nested archive candidates.

    Caps are checked BEFORE any bytes are staged, so a cap abort never leaves
    partially uploaded state behind. Nested candidates are staged to a temp
    file (their bytes are needed twice: inner plan pass + inner upload pass).
    """
    _check_budget(budget, 1, size)

    hasher = hashlib.sha256()
    # Peek enough bytes for EVERY magic (plain tar carries "ustar" at 257),
    # then keep streaming; hashed bytes are never read twice.
    head = src.read(_MAGIC_PEEK)
    hasher.update(head)
    head_fmt = _format_from_magic(head)

    nested_candidate = head_fmt is not None
    can_recurse = nested_candidate and depth < MAX_DEPTH
    staged: Path | None = None
    fout = None
    try:
        if nested_candidate:
            # Bytes are needed on disk: for the inner ingest when recursing,
            # or as the whole-file fallback member at the depth cap.
            staged = _stage_temp(plan) / "nested.bin"
            fout = open(staged, "wb")
            fout.write(head)
        while True:
            block = src.read(_COPY_BLOCK)
            if not block:
                break
            hasher.update(block)
            if fout is not None:
                fout.write(block)
        if fout is not None:
            fout.close()
            fout = None

        if not nested_candidate:
            return MemberPlan(path=sanitized, sha256=hasher.hexdigest(),
                              size=size, mtime=mtime)

        if not can_recurse:
            # Depth cap: keep the nested archive's bytes as a regular member
            # (fallback rule — the file itself is still preserved whole).
            log.warning("nesting depth cap (%d) reached inside %s — member %r "
                        "will be stored whole", MAX_DEPTH, plan.format, sanitized)
            return MemberPlan(path=sanitized, sha256=hasher.hexdigest(),
                              size=size, mtime=mtime, staged_path=staged)

        inner_fmt = detect_archive_format(str(staged))
        if inner_fmt is None:
            # gzip/bzip2/xz member that does not contain a tar: plain file.
            _discard_staged(plan, staged)
            return MemberPlan(path=sanitized, sha256=hasher.hexdigest(),
                              size=size, mtime=mtime)

        inner = _ingest_archive(str(staged), inner_fmt, ctx,
                                original_name=sanitized, depth=depth + 1,
                                fallback_upload=False)
        if inner.get("status") == "archive_ingested":
            # The nested archive's uncompressed total counts toward the
            # parent's budget.
            _check_budget(budget, inner.get("member_count", 0),
                          inner.get("total_uncompressed", 0))
            _discard_staged(plan, staged)
            log.info("nested archive %r -> %s (%d members)", sanitized,
                     inner["fingerprint"][:16], inner.get("member_count", 0))
            return MemberPlan(path=sanitized, sha256=hasher.hexdigest(),
                              size=size, mtime=mtime,
                              nested_archive=inner["fingerprint"])
        # Inner archive could not be extracted (corrupt/caps): keep its bytes
        # and upload them as a regular member of the parent (fallback rule).
        log.warning("nested archive %r falls back to whole-file (%s)",
                    sanitized, inner.get("reason"))
        return MemberPlan(path=sanitized, sha256=hasher.hexdigest(),
                          size=size, mtime=mtime, staged_path=staged)
    finally:
        if fout is not None:
            fout.close()


def _plan_tar(path: str, fmt: str, depth: int, budget: Budget,
              plan: ArchivePlan, ctx: IngestContext) -> None:
    with tarfile.open(path, _TAR_OPEN_MODE[fmt]) as tar:
        seen: set[str] = set()
        for member in tar:
            if not member.isfile():
                continue  # dirs, symlinks, devices: nothing to hash
            sanitized = sanitize_member(member.name)
            if sanitized is None:
                log.warning("rejecting unsafe member name %r in %s",
                            member.name, path)
                plan.unsafe_names += 1
                continue
            if sanitized in seen:
                plan.duplicate_names += 1
                continue
            seen.add(sanitized)
            src = tar.extractfile(member)
            if src is None:
                plan.unsafe_names += 1
                continue
            entry = _plan_member_stream(
                src, sanitized, member.size, float(member.mtime),
                depth, budget, plan, ctx)
            plan.entries.append(entry)


def _plan_zip(path: str, depth: int, budget: Budget,
              plan: ArchivePlan, ctx: IngestContext) -> None:
    with zipfile.ZipFile(path) as zf:
        seen: set[str] = set()
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise ArchiveFallback(
                    "encrypted or password-protected zip member "
                    f"({info.filename!r})")
            sanitized = sanitize_member(info.filename)
            if sanitized is None:
                log.warning("rejecting unsafe member name %r in %s",
                            info.filename, path)
                plan.unsafe_names += 1
                continue
            if sanitized in seen:
                plan.duplicate_names += 1
                continue
            seen.add(sanitized)
            with zf.open(info) as src:
                entry = _plan_member_stream(
                    src, sanitized, info.file_size, _zip_mtime(info),
                    depth, budget, plan, ctx)
            plan.entries.append(entry)


def _plan_container(path: str, fmt: str, depth: int, budget: Budget,
                    plan: ArchivePlan, ctx: IngestContext) -> None:
    try:
        if fmt == "zip":
            _plan_zip(path, depth, budget, plan, ctx)
        else:
            _plan_tar(path, fmt, depth, budget, plan, ctx)
    except ArchiveFallback:
        raise
    except Exception as e:
        raise ArchiveFallback(f"extraction failed for {fmt} archive: {e}") from e


# ---------------------------------------------------------------------------
# Upload pass (pass 2) + DB recording + manifest sidecar
# ---------------------------------------------------------------------------

def _upload_one(entry: MemberPlan, local_path: str, fp: str,
                ctx: IngestContext) -> None:
    """Upload one member through the real pipeline and record its file_id."""
    item = {
        "local_path": str(local_path),
        "source_root": f"archive/{fp}",
        "relative_path": entry.path,
        "version_seq": 1,
    }
    results = upload_bundle([item], ctx.backends, ctx.config, ctx.redundancy)
    res = results[0] if results else {"status": "error", "error": "no result"}
    if res.get("status") == "error":
        raise RuntimeError(
            f"member {entry.path!r} failed to upload: "
            f"{res.get('error', 'upload failed')}")
    file_id = res.get("file_id")
    if file_id is None:
        raise RuntimeError(f"member {entry.path!r} upload returned no file_id")
    entry.file_id = file_id


def _upload_members(path: str, fmt: str, fp: str, plan: ArchivePlan,
                    ctx: IngestContext, staging_root: Path) -> None:
    """Stage + upload every regular member; nested members stay virtual.

    Pre-staged members (nested whole-file fallback) are uploaded from their
    staging files; everything else is re-extracted from the container one
    member at a time and the staging file deleted right after its upload.
    """
    pending = {e.path: e for e in plan.entries
               if e.nested_archive is None and e.file_id is None}

    for entry in plan.entries:
        if entry.staged_path is None or entry.path not in pending:
            continue
        _upload_one(entry, str(entry.staged_path), fp, ctx)
        pending.pop(entry.path, None)

    def _stage_from(src) -> Path:
        staged = staging_root / "member.bin"
        with open(staged, "wb") as fout:
            shutil.copyfileobj(src, fout, _COPY_BLOCK)
        return staged

    if pending and fmt == "zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not pending:
                    break
                if info.is_dir():
                    continue
                sanitized = sanitize_member(info.filename)
                entry = pending.pop(sanitized, None) if sanitized else None
                if entry is None or entry.file_id is not None:
                    continue
                with zf.open(info) as src:
                    staged = _stage_from(src)
                try:
                    _upload_one(entry, str(staged), fp, ctx)
                finally:
                    staged.unlink(missing_ok=True)
    elif pending:
        with tarfile.open(path, _TAR_OPEN_MODE[fmt]) as tar:
            for member in tar:
                if not pending:
                    break
                if not member.isfile():
                    continue
                sanitized = sanitize_member(member.name)
                entry = pending.pop(sanitized, None) if sanitized else None
                if entry is None or entry.file_id is not None:
                    continue
                src = tar.extractfile(member)
                if src is None:
                    raise RuntimeError(f"member {sanitized!r} has no data")
                staged = _stage_from(src)
                try:
                    _upload_one(entry, str(staged), fp, ctx)
                finally:
                    staged.unlink(missing_ok=True)

    if pending:
        missing = ", ".join(sorted(pending))[:200]
        raise RuntimeError(
            f"{len(pending)} member(s) disappeared between plan and upload "
            f"passes: {missing}")


def _member_chunks(file_id: int) -> list[dict]:
    """Chunk recipe for a member: (blob_id, plaintext offset, length) rows."""
    rows = execute(
        "SELECT seq, blob_id, pt_offset, pt_size FROM file_chunks "
        "WHERE file_id = ? ORDER BY seq",
        (file_id,),
    ).fetchall()
    return [{"blob_id": r["blob_id"], "offset": r["pt_offset"],
             "length": r["pt_size"]} for r in rows]


def _build_manifest(fp: str, fmt: str, original_name: str, original_size: int,
                    plan: ArchivePlan) -> dict:
    members = []
    for entry in sorted(plan.entries, key=lambda e: e.path):
        member = {"path": entry.path, "sha256": entry.sha256,
                  "size": entry.size, "mtime": entry.mtime}
        if entry.nested_archive is not None:
            member["nested_archive"] = entry.nested_archive
        else:
            member["chunks"] = _member_chunks(entry.file_id)
        members.append(member)
    return {
        "format_version": MANIFEST_FORMAT_VERSION,
        "fingerprint": fp,
        "format": fmt,
        "original_name": original_name,
        "original_size": original_size,
        "member_count": plan.member_count,
        "total_uncompressed": plan.total_uncompressed,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "members": members,
    }


def _record_archive(fp: str, fmt: str, original_name: str, original_size: int,
                    plan: ArchivePlan, ctx: IngestContext) -> int:
    """Insert the archives row + archive_members rows; returns the archive id.

    Nested members get a virtual files row (archive_members.file_id is NOT
    NULL) with NO chunk mapping: their content is deliberately not stored —
    it is re-constructible from the nested archive's own members.
    """
    with write_txn():
        for entry in plan.entries:
            if entry.nested_archive is not None:
                entry.file_id = index_mod.index_file(
                    f"archive/{fp}", entry.path, entry.sha256, entry.size,
                    mtime=entry.mtime, chunked=False,
                    redundancy=ctx.redundancy, version_seq=1,
                    auto_commit=False)
        cur = execute(
            "INSERT INTO archives (fingerprint, format, original_name, "
            "original_size, member_count, total_uncompressed) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fp, fmt, original_name, original_size,
             plan.member_count, plan.total_uncompressed),
        )
        archive_id = cur.lastrowid
        for entry in plan.entries:
            execute(
                "INSERT INTO archive_members (archive_id, relative_path, "
                "file_id, member_sha256, member_size, mtime, nested_archive) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (archive_id, entry.path, entry.file_id, entry.sha256,
                 entry.size, entry.mtime, entry.nested_archive),
            )
    return archive_id


def _write_manifest_sidecars(backends: list, manifest: dict) -> list[str]:
    """Write the manifest JSON to EVERY backend. Best-effort: failures are
    logged, never fatal (the DB manifest remains authoritative)."""
    fp = manifest["fingerprint"]
    remote_path = f"manifests/{fp[:2]}/{fp[2:4]}/{fp}.json"
    tmp_dir = Path(tempfile.mkdtemp(prefix="backupme-arch-"))
    try:
        tmp = tmp_dir / "manifest.json"
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        written = []
        for backend in backends:
            try:
                backend.upload(str(tmp), remote_path)
                written.append(backend.name)
            except Exception as e:
                log.warning("manifest sidecar upload failed on backend %s "
                            "for %s: %s", backend.name, fp[:16], e)
        return written
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ---------------------------------------------------------------------------
# Whole-blob fallback (never lose data)
# ---------------------------------------------------------------------------

def _fallback_whole(path: str, ctx: IngestContext, reason: str) -> dict:
    """Store the archive through the EXISTING whole-blob path, exactly as the
    regular backup flow would (same source_root/relative_path/version_seq)."""
    source_root = ctx.source_root or str(Path(path).parent)
    relative_path = ctx.relative_path or os.path.basename(path)
    log.warning("archive %s stored whole: %s", path, reason)
    res = upload_file(
        source_root=source_root,
        relative_path=relative_path,
        local_path=path,
        backends=ctx.backends,
        config=ctx.config,
        redundancy=ctx.redundancy,
        version_seq=1,
    )
    if res.get("status") == "error":
        return {"status": "error", "stored_whole": True, "reason": reason,
                "error": res.get("error", "upload failed"), "local_path": path,
                "original_name": os.path.basename(path)}
    return {"status": "stored_whole", "stored_whole": True, "reason": reason,
            "file_id": res.get("file_id"), "sha256": res.get("sha256"),
            "dedup": res.get("dedup", False), "local_path": path,
            "original_name": os.path.basename(path)}


# ---------------------------------------------------------------------------
# Ingest engine
# ---------------------------------------------------------------------------

def _dedup_result(fp: str, fmt: str, original_name: str, original_size: int,
                  member_count: int, plan: ArchivePlan | None = None) -> dict:
    result = {
        "status": "archive_ingested", "stored_whole": False, "dedup": True,
        "fingerprint": fp, "format": fmt, "original_name": original_name,
        "original_size": original_size, "member_count": member_count,
    }
    if plan is not None:
        result["total_uncompressed"] = plan.total_uncompressed
        result["unsafe_members"] = plan.unsafe_names
        result["duplicate_members"] = plan.duplicate_names
        result["nested_archives"] = plan.nested_count
    return result


def _ingest_archive(path: str, fmt: str, ctx: IngestContext,
                    original_name: str | None = None, depth: int = 1,
                    fallback_upload: bool = True) -> dict:
    """Plan -> dedup-check -> upload -> record -> sidecar for one container.

    All cap/extraction failures raise ArchiveFallback from the PLAN pass
    (before any of THIS container's members are uploaded), so the whole-blob
    fallback never leaves a half-ingested archive behind.
    """
    name = original_name or os.path.basename(path)
    original_size = os.path.getsize(path)
    plan = ArchivePlan(format=fmt)
    try:
        budget = Budget()
        _plan_container(path, fmt, depth, budget, plan, ctx)

        fp = compute_fingerprint(plan.entries)
        plan.fingerprint = fp

        existing = execute(
            "SELECT id, member_count FROM archives WHERE fingerprint = ?",
            (fp,),
        ).fetchone()
        if existing:
            # Same archive content ingested before: pure no-op (members were
            # uploaded then; nothing new hits the backends).
            return _dedup_result(fp, fmt, name, original_size,
                                 existing["member_count"], plan)

        staging_root = Path(tempfile.mkdtemp(prefix="backupme-arch-"))
        plan.staging_dirs.append(staging_root)
        try:
            _upload_members(path, fmt, fp, plan, ctx, staging_root)
        except ArchiveFallback:
            raise
        except Exception as e:
            raise ArchiveFallback(f"member upload failed: {e}") from e

        manifest = _build_manifest(fp, fmt, name, original_size, plan)
        try:
            _record_archive(fp, fmt, name, original_size, plan, ctx)
        except sqlite3.IntegrityError:
            # Lost a fingerprint race with a concurrent ingest; member rows
            # dedup anyway, so report the archive-level dedup.
            log.info("archive %s already recorded by a concurrent ingest",
                     fp[:16])
            return _dedup_result(fp, fmt, name, original_size,
                                 plan.member_count, plan)

        manifest_backends: list[str] = []
        encryption_cfg = ctx.config.get("encryption", {}) or {}
        if encryption_cfg.get("enabled", False):
            log.info("encryption enabled — manifest sidecar for %s skipped "
                     "(plaintext member names must not reach backends)",
                     fp[:16])
        else:
            manifest_backends = _write_manifest_sidecars(ctx.backends, manifest)

        return {
            "status": "archive_ingested", "stored_whole": False, "dedup": False,
            "fingerprint": fp, "format": fmt, "original_name": name,
            "original_size": original_size,
            "member_count": plan.member_count,
            "total_uncompressed": plan.total_uncompressed,
            "unsafe_members": plan.unsafe_names,
            "duplicate_members": plan.duplicate_names,
            "nested_archives": plan.nested_count,
            "manifest_backends": manifest_backends,
        }
    except ArchiveFallback as fb:
        if not fallback_upload:
            return {"status": "stored_whole", "stored_whole": True,
                    "reason": str(fb), "fingerprint": None, "format": fmt,
                    "original_name": name}
        return _fallback_whole(path, ctx, str(fb))
    except Exception as e:  # defensive: the fallback rule has no exceptions
        log.exception("unexpected error ingesting archive %s", path)
        if not fallback_upload:
            return {"status": "stored_whole", "stored_whole": True,
                    "reason": f"internal error: {e}", "fingerprint": None,
                    "format": fmt, "original_name": name}
        return _fallback_whole(path, ctx, f"internal error: {e}")
    finally:
        for staging_dir in plan.staging_dirs:
            shutil.rmtree(str(staging_dir), ignore_errors=True)


def ingest_archive_file(
    archive_path: str,
    backends: list,
    config,
    redundancy: int = 1,
    source_root: str | None = None,
    relative_path: str | None = None,
    original_name: str | None = None,
) -> dict:
    """Ingest one archive file extract-first.

    Returns either::

        {"status": "archive_ingested", "stored_whole": False,
         "fingerprint": ..., "member_count": N, ...}
    or, when the archive could not be extracted (corrupt/encrypted/caps)::

        {"status": "stored_whole", "stored_whole": True, "reason": ...,
         "file_id": ..., "sha256": ...}   # already stored via the whole path

    ``source_root``/``relative_path`` describe where the file came from so a
    fallback lands on exactly the same files row the normal backup path
    would have produced.
    """
    config_dict = config if isinstance(config, dict) else dataclasses.asdict(config)
    path = str(archive_path)
    context = IngestContext(
        backends=backends, config=config_dict, redundancy=redundancy,
        fallback_upload=True, source_root=source_root,
        relative_path=relative_path)
    archive_format = detect_archive_format(path)
    if archive_format is None:
        return _fallback_whole(path, context, "not a recognized archive format")
    return _ingest_archive(path, archive_format, context,
                           original_name=original_name)


# ---------------------------------------------------------------------------
# Queries + restore
# ---------------------------------------------------------------------------

_HEX_DIGITS = set("0123456789abcdefABCDEF")


def _is_hex(text: str) -> bool:
    return bool(text) and all(c in _HEX_DIGITS for c in text)


def resolve_fingerprint(query: str):
    """Resolve a fingerprint: exact match first, then a unique hex prefix.

    Returns ``(row, match_type, candidates)`` with match_type in
    ``{"exact", "prefix", "prefix-ambiguous", None}``; ``row`` is None unless
    exactly one archive matched.
    """
    from backupme.core.database import execute as _execute

    needle = (query or "").strip().lower()
    row = _execute(
        "SELECT * FROM archives WHERE fingerprint = ?", (needle,)
    ).fetchone()
    if row:
        return row, "exact", []
    if _is_hex(needle) and len(needle) < 64:
        rows = _execute(
            "SELECT * FROM archives WHERE fingerprint LIKE ? ORDER BY id",
            (needle + "%",),
        ).fetchall()
        if len(rows) == 1:
            return rows[0], "prefix", []
        if rows:
            return None, "prefix-ambiguous", [dict(r) for r in rows]
    return None, None, []


def get_archive_members(archive_id: int) -> list[dict]:
    """Full member list of an archive, with the referenced files-row state."""
    rows = execute(
        "SELECT am.id, am.relative_path, am.file_id, am.member_sha256, "
        "am.member_size, am.mtime, am.nested_archive, "
        "f.sha256 AS file_sha256, f.deleted_at AS file_deleted_at "
        "FROM archive_members am LEFT JOIN files f ON f.id = am.file_id "
        "WHERE am.archive_id = ? ORDER BY am.relative_path",
        (archive_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def restore_archive(fingerprint_query: str, output_dir: str) -> dict:
    """Restore an archive's members under ``output_dir``.

    Every regular member is written via the normal download path (its sha256
    is verified against the manifest). A nested archive member is restored as
    a DIRECTORY at its path containing the nested archive's own members
    (nested structure preserved; the nested container file itself was never
    stored).
    """
    from backupme.core.downloader import download_file

    row, match_type, candidates = resolve_fingerprint(fingerprint_query)
    if row is None:
        if match_type == "prefix-ambiguous":
            return {
                "status": "error",
                "error": (f"fingerprint prefix matches {len(candidates)} "
                          "archives; not unique"),
                "candidates": [{"fingerprint": c["fingerprint"],
                                "original_name": c["original_name"]}
                               for c in candidates],
            }
        return {"status": "error",
                "error": f"no archive with fingerprint {fingerprint_query!r}"}

    fp = row["fingerprint"]
    out_base = Path(output_dir)
    members = get_archive_members(row["id"])
    detail: list[dict] = []
    error_count = 0

    for member in members:
        rel = member["relative_path"]
        path_obj = Path(rel)
        # Defense in depth: ingest sanitizes member names, but a poisoned or
        # legacy row must never write outside the restore root.
        unsafe = (".." in path_obj.parts or path_obj.is_absolute()
                  or (len(rel) >= 2 and rel[1] == ":"))
        if unsafe:
            error_count += 1
            detail.append({"relative_path": rel, "status": "error",
                           "error": "unsafe member path rejected"})
            continue

        if member["nested_archive"]:
            nested_dir = out_base / rel.replace("/", os.sep)
            inner = restore_archive(member["nested_archive"], str(nested_dir))
            inner_ok = inner.get("status") == "ok"
            error_count += 0 if inner_ok else 1
            detail.append({
                "relative_path": rel,
                "status": "ok" if inner_ok else "error",
                "nested_archive": member["nested_archive"],
                "members": inner.get("members", 0),
                "error": None if inner_ok else inner.get("error",
                                                         "nested restore failed"),
            })
            continue

        if member["file_deleted_at"] is not None:
            error_count += 1
            detail.append({"relative_path": rel, "status": "error",
                           "error": "member file row is soft-deleted"})
            continue

        dest = out_base / rel.replace("/", os.sep)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = download_file(member["file_id"], str(dest))
            restored_sha = res.get("sha256", "")
            if restored_sha != member["member_sha256"]:
                raise RuntimeError(
                    f"member sha mismatch: expected "
                    f"{member['member_sha256'][:16]}..., got "
                    f"{restored_sha[:16]}...")
            detail.append({"relative_path": rel, "status": "ok",
                           "path": str(dest), "sha256": restored_sha})
        except Exception as e:
            log.error("restore failed for member %s: %s", rel, e,
                      exc_info=True)
            error_count += 1
            detail.append({"relative_path": rel, "status": "error",
                           "error": str(e)})

    ok_members = sum(1 for d in detail if d["status"] == "ok")
    return {
        "status": "ok" if error_count == 0 else "partial",
        "fingerprint": fp,
        "format": row["format"],
        "original_name": row["original_name"],
        "members": ok_members,
        "errors": error_count,
        "out": str(out_base),
        "detail": detail,
    }

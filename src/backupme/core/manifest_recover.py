"""Archive-level vault recovery from manifest sidecars (``manifests/**``).

SAFETY CONTRACT (the reason this module exists the way it does): recovery
NEVER opens, reads or writes the live BackupMe database. ``backupme.db`` (or
whatever ``--db`` the process carries) is not touched here at all — this
module only

- READS the manifest sidecars and blob objects from ONE backend, and
- WRITES the ``--into`` SQLite file (a brand-new database built through
  ``schema.apply_migrations``).

If the live database is lost, the blob store plus the manifest sidecars are
enough to rebuild the archive vault map. The rebuild is complete:
``files`` / ``file_chunks`` / ``blobs`` / ``blob_storage`` /
``archives`` / ``archive_members`` rows for everything the manifests
describe, so the recovered file can be dropped in as a working BackupMe
database (restore paths read those rows only).

What is scanned: the ``manifests/`` remote namespace carries TWO JSON
families. Files whose stem is 32 hex chars (`<uuid>.manifest.json`) are
per-FILE receipts written by the pre-existing ``core/manifest.py`` upload
path; they already drive the FILE-level ``db rebuild`` recovery
(``core/recover.py:rebuild_from_manifests``) and are NOT archive manifests —
they are skipped silently here (counted as ``skipped_receipts``, never
errors). Only files whose stem is 64 hex chars AND whose parsed JSON carries
``fingerprint`` + ``members`` are archive sidecars. Conversely, a
receipt-rebuild run sees archive sidecars as its own noise; the two tools are
complementary, not duplicates: ``db rebuild`` restores ordinary file rows
from receipts, ``db recover-from-manifests`` restores the archive-level map
from sidecars.

Verification semantics (--verify):
- ``full`` (the honest default): every chunk is downloaded from the backend,
  DECOMPRESSED, and its PLAINTEXT bytes are sha256-checked against the blob
  id tail (the D8 lesson: the sha gate lives on plaintext content, not on
  the compressed object). Bundle parent tars are additionally parsed
  member-by-member (52-byte header + payload) so each bundle-member blob's
  ``@offset:length`` byte range is rebuilt exactly, and every member range is
  hashed against its blob id tail too.
- ``none`` trusts listings: standalone chunk objects are only probed for
  existence (+ size via a best-effort directory listing), no downloads and
  no hashes. Bundle parent tars still MUST be downloaded and parsed — the
  member byte ranges inside them cannot be inferred any other way — but no
  sha256 gate is applied to them. Output is clearly labeled ``verify:
  "none"``.

Members whose chunks are missing or fail verification are reported in the
``problems`` section (fingerprint, path, missing blob ids) and their file
row is NOT written — nothing in the rebuilt database claims to restore from
nothing. Their archive row is still recorded (report-level flag); the gap is
visible as ``archives.member_count > COUNT(archive_members)``.

Refcount semantics mirror the live pipeline exactly
(``dedup.reconcile_refcounts``): one reference per ``file_chunks`` row, plus
one reference per bundle-member blob id to its parent bundle blob.

Blobs are deduplicated by blob id across manifests: a blob referenced by N
members gets ref_count N (+ parent tallies for bundle members).
"""
import hashlib
import json
import re
import shutil
import sqlite3
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from backupme.log import get_logger

log = get_logger("manifest_recover")

_BUNDLE_HEADER_FMT = struct.Struct(">32sQQI")
_BUNDLE_HEADER_LEN = _BUNDLE_HEADER_FMT.size

# Storage-object suffixes the uploader may append to a content-addressed
# path, most-likely first ("" = stored uncompressed, files <= 256 B).
_STORE_SUFFIXES = (".zstd", ".gz", ".bz2", ".xz", ".lzma", "")

_STANDALONE_ID_RE = re.compile(r"^[0-9a-f]{64}$")

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ManifestRecoverError(Exception):
    """Clean, user-facing recovery failure (bad --into path, unreachable
    backend). The CLI turns this into exit 1."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _default_into_path() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"recover-{ts}.db"


def _resolve_into_path(into: str | None) -> Path:
    """Validate the --into target. An existing NON-EMPTY file is refused:
    recovery only ever creates a fresh database and must never be able to
    clobber one (the live backupme.db is non-empty, so it is refused by the
    same rule)."""
    raw = (into or "").strip() or _default_into_path()
    path = Path(raw)
    if path.is_dir():
        raise ManifestRecoverError(
            f"--into points at a directory: {path} (pass a new sqlite file path)")
    if path.exists():
        if path.stat().st_size > 0:
            raise ManifestRecoverError(
                f"refusing to overwrite existing non-empty file: {path} "
                "(recovery never touches an existing database — pick a new "
                "--into path, e.g. recover-<UTC ts>.db)")
        # Empty placeholder file: creating the sqlite db over it is safe.
    return path


def _blob_tail(blob_id: str) -> str:
    """The sha256 a blob id commits to: the last path component."""
    return blob_id.rsplit("/", 1)[-1]


def _parent_bundle_id(blob_id: str) -> str | None:
    """Parent bundle id of a 5-part bundle member id, else None."""
    if not blob_id.startswith("bundle/"):
        return None
    if blob_id.count("/") < 4:
        return None
    return blob_id.rsplit("/", 1)[0]


def _decompress(data: bytes, suffix: str) -> bytes:
    from backupme.core import compress as compress_mod
    algo = suffix.lstrip(".").strip()
    if not algo or algo == "none":
        return data
    return compress_mod.decompress(data, algo)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _problem(kind: str, fingerprint: str | None, path: str | None,
             missing: list[str] | None = None, error: str | None = None) -> dict:
    return {"kind": kind, "fingerprint": fingerprint, "path": path,
            "missing": missing or [], "error": error}


# ---------------------------------------------------------------------------
# Backend walking / object lookup
# ---------------------------------------------------------------------------

def _list_manifest_entries(bk) -> list[dict]:
    """Every file entry under manifests/ (recursive when the backend can)."""
    try:
        entries = bk.walk("manifests")
    except Exception as e:
        log.warning("manifest walk failed on %s, falling back to list_files: %s",
                    bk.name, e)
        entries = bk.list_files("manifests")
    return [e for e in entries if isinstance(e, dict) and e.get("path")]


def _is_archive_sidecar(entry_path: str) -> bool:
    """Family filter for the manifests/ namespace.

    64-hex stem  -> archive sidecar candidate (``<fp>.json``).
    32-hex stem  -> per-file receipt (``<uuid>.manifest.json``) from the
                    pre-existing upload path — skipped, not an error.
    Anything else -> not ours, skipped as a receipt too.
    """
    stem = entry_path.rsplit("/", 1)[-1]
    if stem.endswith(".manifest.json"):
        return False
    if not stem.endswith(".json"):
        return False
    return bool(_STANDALONE_ID_RE.match(stem[: -len(".json")]))


def _find_stored_object(bk, blob_id: str) -> dict | None:
    """Locate the stored object for a content-addressed blob id.

    The uploader stores ``<blob_id>`` plus a compression suffix (or bare for
    uncompressed small payloads). Suffixes are probed with ``exists()``;
    matching by full path keeps every backend shape happy.
    """
    base = blob_id
    if not blob_id.startswith("bundle/"):
        base = f"objects/{blob_id[:2]}/{blob_id[2:4]}/{blob_id}"
    for suffix in _STORE_SUFFIXES:
        candidate = base + suffix
        try:
            if bk.exists(candidate):
                return {"path": candidate, "suffix": suffix}
        except Exception as e:
            log.warning("exists() probe failed on %s for %s: %s",
                        bk.name, candidate, e)
    return None


def _listed_size(bk, stored_path: str) -> int | None:
    """Best-effort listed size for a stored path (verify=none bookkeeping)."""
    parent = stored_path.rsplit("/", 1)[0] if "/" in stored_path else ""
    try:
        for entry in bk.list_files(parent):
            entry_path = str(entry.get("path") or "")
            base = entry_path.rsplit("/", 1)[-1]
            if base == stored_path.rsplit("/", 1)[-1]:
                try:
                    return int(entry.get("size"))
                except (TypeError, ValueError):
                    return None
    except Exception as e:
        log.debug("size listing failed on %s for %s: %s", bk.name, stored_path, e)
    return None


def _download(bk, remote_path: str, tmp_dir: Path, name: str) -> bytes:
    local = tmp_dir / name
    bk.download(remote_path, str(local))
    try:
        return local.read_bytes()
    finally:
        local.unlink(missing_ok=True)


def _parse_bundle_members(plain: bytes) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Parse a bundle tar layout: [52-byte header][payload] repeated.

    Header format (uploader._BUNDLE_HEADER_FMT, big-endian): 32s member name,
    Q payload length, Q (repeat), I flags. Returns
    ({member_sha256: (offset, length)}, problems) — the sha256 of each
    payload is its identity, so the map keys are member blob id tails.
    """
    member_map: dict[str, tuple[int, int]] = {}
    problems: list[str] = []
    pos = 0
    total = len(plain)
    while pos < total:
        if pos + _BUNDLE_HEADER_LEN > total:
            problems.append(f"bundle truncated at offset {pos}: header cut short")
            break
        _name, plen, _plen2, _flags = _BUNDLE_HEADER_FMT.unpack_from(plain, pos)
        start = pos + _BUNDLE_HEADER_LEN
        end = start + plen
        if end > total:
            problems.append(
                f"bundle truncated at offset {pos}: payload needs {plen} bytes")
            break
        member_map[_sha256_hex(plain[start:end])] = (start, plen)
        pos = end
    return member_map, problems


# ---------------------------------------------------------------------------
# Planner: scan + verify everything into memory first
# ---------------------------------------------------------------------------

class _BlobRow:
    """Accumulated DB state for one blob id across all manifests."""
    __slots__ = ("blob_id", "pt_size", "storage", "verified", "member_map")

    def __init__(self, blob_id: str, pt_size: int):
        self.blob_id = blob_id
        self.pt_size = pt_size
        self.storage: dict[tuple, dict] = {}   # (backend, storage_path) -> row
        self.verified = False
        self.member_map: dict[str, tuple[int, int]] | None = None  # bundle parents only


class _RecoveryPlan:
    def __init__(self, backend_name: str, verify: str):
        self.backend_name = backend_name
        self.verify = verify
        self.manifests_scanned = 0
        self.skipped_receipts = 0
        self.archives: list[dict] = []          # {manifest, members: [...]}
        self.blobs: dict[str, _BlobRow] = {}    # every blob id we will write
        self.refs: dict[str, int] = {}          # refcount per blob id
        self.problems: list[dict] = []
        self.archives_recovered = 0
        self.members_recovered = 0
        self.bytes_recoverable = 0

    def ensure_blob(self, blob_id: str, pt_size: int) -> _BlobRow:
        row = self.blobs.get(blob_id)
        if row is None:
            row = _BlobRow(blob_id, max(0, int(pt_size or 0)))
            self.blobs[blob_id] = row
        elif pt_size and row.pt_size == 0:
            row.pt_size = int(pt_size)
        return row

    def add_chunk_ref(self, blob_id: str) -> None:
        self.refs[blob_id] = self.refs.get(blob_id, 0) + 1
        parent = _parent_bundle_id(blob_id)
        if parent is not None:
            # reconcile_refcounts rule: each member blob id contributes one
            # reference to its parent bundle.
            self.refs[parent] = self.refs.get(parent, 0) + 1


def _verify_standalone_chunk(bk, plan: _RecoveryPlan, blob_id: str,
                             expected_len: int, tmp_dir: Path) -> bool:
    """Verify (or, in none-mode, trust) one standalone object chunk."""
    row = plan.ensure_blob(blob_id, expected_len)
    if row.storage:
        return True
    found = _find_stored_object(bk, blob_id)
    if found is None:
        return False
    suffix = found["suffix"]
    algo = suffix.lstrip(".") or None
    if plan.verify == "full":
        try:
            raw = _download(bk, found["path"], tmp_dir, "chunk_dl")
        except Exception as e:
            log.warning("chunk download failed for %s on %s: %s",
                        blob_id[:16], bk.name, e)
            return False
        try:
            plain = _decompress(raw, suffix)
        except Exception as e:
            log.warning("chunk decompress failed for %s: %s", blob_id[:16], e)
            return False
        # THE sha gate: plaintext content must hash to the blob id tail.
        if _sha256_hex(plain) != _blob_tail(blob_id):
            return False
        row.verified = True
        stored_size, stored_sha = len(raw), _sha256_hex(raw)
    else:
        stored_size = _listed_size(bk, found["path"])
        stored_size = stored_size if stored_size is not None else 0
        stored_sha = ""   # unknown in trust-listing mode
    row.storage[(plan.backend_name, found["path"])] = {
        "storage_path": found["path"], "stored_size": stored_size,
        "stored_sha256": stored_sha, "algo": algo, "level": None,
        "verified": plan.verify == "full",
    }
    return True


def _verify_bundle_parent(bk, plan: _RecoveryPlan, parent_id: str,
                          tmp_dir: Path) -> dict | None:
    """Download + decompress + parse a bundle parent tar.

    Returns {"member_map": {sha: (offset, len)}, "plain_len": int} or None.
    In full mode the decompressed tar must hash to the parent id tail (the
    whole-tar sha the blob id commits to).
    """
    cached = plan.blobs.get(parent_id)
    if cached is not None and getattr(cached, "member_map", None) is not None:
        return {"member_map": cached.member_map, "plain_len": cached.pt_size}

    found = _find_stored_object(bk, parent_id)
    if found is None:
        return None
    suffix = found["suffix"]
    try:
        raw = _download(bk, found["path"], tmp_dir, "bundle_dl")
    except Exception as e:
        log.warning("bundle download failed for %s on %s: %s",
                    parent_id[:24], bk.name, e)
        return None
    try:
        plain = _decompress(raw, suffix)
    except Exception as e:
        log.warning("bundle decompress failed for %s: %s", parent_id[:24], e)
        return None

    member_map, parse_problems = _parse_bundle_members(plain)
    if plan.verify == "full":
        if _sha256_hex(plain) != _blob_tail(parent_id):
            log.warning("bundle %s failed the plaintext sha gate",
                        parent_id[:24])
            return None

    row = plan.ensure_blob(parent_id, len(plain))
    row.member_map = member_map
    row.verified = plan.verify == "full"
    algo = suffix.lstrip(".") or None
    row.storage[(plan.backend_name, found["path"])] = {
        "storage_path": found["path"], "stored_size": len(raw),
        "stored_sha256": _sha256_hex(raw), "algo": algo, "level": None,
        "verified": plan.verify == "full",
    }
    for text in parse_problems:
        plan.problems.append(_problem("bundle", parent_id, found["path"],
                                      error=text))
    return {"member_map": member_map, "plain_len": len(plain)}


def _verify_member_chunks(bk, plan: _RecoveryPlan, fingerprint: str,
                          member: dict, tmp_dir: Path) -> list[tuple] | None:
    """Resolve + verify every chunk of one manifest member.

    Returns [(blob_id, pt_offset, pt_length)] on success, None when any
    chunk is missing/unverifiable (the member must then claim nothing).
    """
    resolved: list[tuple] = []
    for chunk in member.get("chunks") or []:
        blob_id = str(chunk.get("blob_id") or "")
        offset = int(chunk.get("offset") or 0)
        length = int(chunk.get("length") or 0)
        parent_id = _parent_bundle_id(blob_id)

        if blob_id.startswith("bundle/") and parent_id is None:
            # 4-part id: the chunk points at a whole bundle object.
            parent = _verify_bundle_parent(bk, plan, blob_id, tmp_dir)
            if parent is None:
                plan.problems.append(_problem(
                    "member", fingerprint, member.get("path"),
                    missing=[blob_id], error="bundle object missing or corrupt"))
                return None
            plan.add_chunk_ref(blob_id)
            resolved.append((blob_id, offset, length))
            continue

        if parent_id is not None:
            parent = _verify_bundle_parent(bk, plan, parent_id, tmp_dir)
            if parent is None:
                plan.problems.append(_problem(
                    "member", fingerprint, member.get("path"),
                    missing=[parent_id], error="parent bundle missing or corrupt"))
                return None
            tail = _blob_tail(blob_id)
            entry = parent["member_map"].get(tail)
            if entry is None:
                plan.problems.append(_problem(
                    "member", fingerprint, member.get("path"),
                    missing=[blob_id],
                    error="member range not found inside parent bundle"))
                return None
            stored_offset, stored_len = entry
            if plan.verify == "full" and stored_len != length:
                plan.problems.append(_problem(
                    "member", fingerprint, member.get("path"),
                    missing=[blob_id],
                    error=(f"bundle member length {stored_len} != manifest "
                           f"length {length}")))
                return None
            blob_row = plan.ensure_blob(blob_id, length)
            parent_row = plan.blobs[parent_id]
            parent_storage = next(iter(parent_row.storage.values()))
            blob_row.storage[(plan.backend_name,
                              f"{parent_storage['storage_path']}@{stored_offset}:{stored_len}")] = {
                "storage_path": (f"{parent_storage['storage_path']}"
                                 f"@{stored_offset}:{stored_len}"),
                "stored_size": stored_len,
                "stored_sha256": parent_storage["stored_sha256"],
                "algo": parent_storage["algo"], "level": None,
                "verified": plan.verify == "full",
            }
            plan.add_chunk_ref(blob_id)
            resolved.append((blob_id, offset, length))
            continue

        if not _STANDALONE_ID_RE.match(blob_id):
            plan.problems.append(_problem(
                "member", fingerprint, member.get("path"), missing=[blob_id],
                error="unrecognized blob id shape"))
            return None
        if not _verify_standalone_chunk(bk, plan, blob_id, length, tmp_dir):
            plan.problems.append(_problem(
                "member", fingerprint, member.get("path"), missing=[blob_id],
                error="chunk object missing or failed plaintext sha gate"
                if plan.verify == "full" else "chunk object not found on backend"))
            return None
        plan.add_chunk_ref(blob_id)
        resolved.append((blob_id, offset, length))
    return resolved


def _scan(bk, config, verify: str) -> _RecoveryPlan:
    """Walk manifests/**, classify, verify and plan everything in memory."""
    from backupme.core.archives import sanitize_member

    plan = _RecoveryPlan(bk.name, verify)
    tmp_dir = Path(tempfile.mkdtemp(prefix="backupme-recover-"))
    try:
        entries = _list_manifest_entries(bk)
        manifests: dict[str, dict] = {}
        for entry in entries:
            entry_path = str(entry.get("path"))
            if not _is_archive_sidecar(entry_path):
                plan.skipped_receipts += 1
                continue
            try:
                raw = _download(bk, entry_path, tmp_dir, "manifest_dl")
                payload = json.loads(raw.decode("utf-8"))
            except Exception as e:
                plan.problems.append(_problem(
                    "manifest", None, entry_path,
                    error=f"unreadable sidecar: {e}"))
                continue
            fp = payload.get("fingerprint")
            if (not isinstance(fp, str)
                    or not _STANDALONE_ID_RE.match(fp)
                    or not isinstance(payload.get("members"), list)):
                # Receipts and foreign JSON must never become errors here.
                plan.skipped_receipts += 1
                continue
            if fp in manifests:
                if manifests[fp].get("members") != payload.get("members"):
                    plan.problems.append(_problem(
                        "manifest", fp, entry_path,
                        error="conflicting sidecar for a known fingerprint"))
                continue
            manifests[fp] = payload

        plan.manifests_scanned = len(manifests)

        for fp in sorted(manifests):
            manifest = manifests[fp]
            planned_members = []
            for member in manifest.get("members", []):
                rel = str(member.get("path") or "")
                if not sanitize_member(rel):
                    plan.problems.append(_problem(
                        "member", fp, rel,
                        error="unsafe member path rejected"))
                    continue
                if member.get("nested_archive"):
                    planned_members.append({
                        "member": member, "chunks": None,
                        "nested": str(member["nested_archive"]),
                    })
                    nested_fp = str(member["nested_archive"])
                    if nested_fp not in manifests:
                        plan.problems.append(_problem(
                            "nested", fp, rel, missing=[nested_fp],
                            error=("nested archive sidecar not present on this "
                                   "backend; the member stays virtual")))
                    continue
                chunks = _verify_member_chunks(bk, plan, fp, member, tmp_dir)
                if chunks is None:
                    continue
                planned_members.append({
                    "member": member, "chunks": chunks, "nested": None,
                })
            plan.archives.append({"manifest": manifest, "members": planned_members})

        # Pre-write counts (what WILL be / in dry-run WOULD be written).
        plan.archives_recovered = len(plan.archives)
        plan.members_recovered = sum(len(a["members"]) for a in plan.archives)
        plan.bytes_recoverable = sum(int(m["member"].get("size") or 0)
                                     for a in plan.archives
                                     for m in a["members"])
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
    return plan


# ---------------------------------------------------------------------------
# Writer: build the fresh sqlite file from the plan
# ---------------------------------------------------------------------------

def _write_recovered_db(plan: _RecoveryPlan, into: Path) -> None:
    from backupme.core.schema import apply_migrations

    con = sqlite3.connect(str(into))
    try:
        apply_migrations(con)
        cur = con.cursor()
        backend_name = plan.backend_name
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for blob_id, row in plan.blobs.items():
            cur.execute(
                "INSERT OR IGNORE INTO blobs (blob_id, pt_size, ref_count) "
                "VALUES (?, ?, 0)",
                (blob_id, row.pt_size),
            )
            for (bname, storage_path), s in row.storage.items():
                cur.execute(
                    """INSERT OR IGNORE INTO blob_storage
                       (blob_id, backend_name, storage_path, stored_size,
                        stored_sha256, compression_algo, compression_level,
                        encrypted, nonce, auth_tag, verified_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?)""",
                    (blob_id, bname, storage_path, s["stored_size"],
                     s["stored_sha256"], s["algo"], s["level"],
                     now_utc if s["verified"] else None),
                )

        archives_recovered = 0
        members_recovered = 0
        bytes_recoverable = 0
        for entry in plan.archives:
            manifest = entry["manifest"]
            fp = manifest["fingerprint"]
            cur.execute(
                "INSERT OR IGNORE INTO archives (fingerprint, format, "
                "original_name, original_size, member_count, "
                "total_uncompressed) VALUES (?, ?, ?, ?, ?, ?)",
                (fp, manifest.get("format"),
                 manifest.get("original_name"),
                 int(manifest.get("original_size") or 0),
                 int(manifest.get("member_count") or 0),
                 int(manifest.get("total_uncompressed") or 0)),
            )
            cur.execute("SELECT id FROM archives WHERE fingerprint = ?", (fp,))
            arc_row = cur.fetchone()
            if arc_row is None:
                continue
            archive_id = arc_row[0]
            for planned in entry["members"]:
                member = planned["member"]
                rel = str(member.get("path"))
                sha = str(member.get("sha256") or "")
                size = int(member.get("size") or 0)
                mtime = float(member.get("mtime") or 0.0)
                chunks = planned["chunks"]
                cur.execute(
                    """INSERT INTO files (source_root, relative_path, sha256,
                       size, mtime, chunked, redundancy, version_group,
                       version_seq, content_text)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?, 1, NULL)""",
                    (f"archive/{fp}", rel, sha, size, mtime,
                     1 if chunks and len(chunks) > 1 else 0, sha[:16]),
                )
                cur.execute("SELECT last_insert_rowid()")
                file_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO files_fts(rowid, relative_path, content_text) "
                    "VALUES (?, ?, NULL)", (file_id, rel),
                )
                if chunks:
                    for seq, (blob_id, offset, length) in enumerate(chunks):
                        cur.execute(
                            "INSERT INTO file_chunks (file_id, seq, blob_id, "
                            "pt_offset, pt_size) VALUES (?, ?, ?, ?, ?)",
                            (file_id, seq, blob_id, offset, length),
                        )
                cur.execute(
                    """INSERT INTO archive_members (archive_id, relative_path,
                       file_id, member_sha256, member_size, mtime,
                       nested_archive) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (archive_id, rel, file_id, sha, size, mtime,
                     planned["nested"]),
                )
                members_recovered += 1
                bytes_recoverable += size
            archives_recovered += 1

        # Refcounts: exactly the reconcile_refcounts semantics over the rows
        # actually written (chunks written + member->parent tallies).
        for blob_id, count in plan.refs.items():
            cur.execute(
                "UPDATE blobs SET ref_count = ? WHERE blob_id = ?",
                (count, blob_id),
            )
        con.commit()

        plan.archives_recovered = archives_recovered
        plan.members_recovered = members_recovered
        plan.bytes_recoverable = bytes_recoverable
    except Exception:
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def recover_from_manifests(config, backend_name: str, into: str | None = None,
                           verify: str = "full", dry_run: bool = False) -> dict:
    """Rebuild a fresh database from the archive sidecars on ONE backend.

    Never touches the live DB: see the module docstring. Returns the result
    dict consumed by the CLI's emit() helper.
    """
    if verify not in ("full", "none"):
        raise ManifestRecoverError(f"unknown verify mode: {verify!r}")

    from backupme.backends import get_backend
    bk = get_backend(backend_name, config)
    if bk is None:
        raise ManifestRecoverError(
            f"backend '{backend_name}' not found or not reachable")

    into_path = _resolve_into_path(into)

    plan = _scan(bk, config, verify)

    if not dry_run:
        _write_recovered_db(plan, into_path)

    status = "dry-run" if dry_run else ("ok" if not plan.problems else "partial")
    return {
        "status": status,
        "dry_run": bool(dry_run),
        "manifests_scanned": plan.manifests_scanned,
        "skipped_receipts": plan.skipped_receipts,
        "archives_recovered": plan.archives_recovered,
        "members_recovered": plan.members_recovered,
        "blobs_recovered": len(plan.blobs),
        "bytes_recoverable": plan.bytes_recoverable,
        "problems": plan.problems,
        "into": str(into_path),
        "verify": plan.verify,
        "backend": backend_name,
    }

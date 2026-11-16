"""QA3 core-engine black-box sweep (agent 3 of 3).

Area: backupme/core/ + backupme/backends/, exercised through the public
APIs (upload_bundle / download_file / dedup / retention / db_lifecycle /
cpanel_ingest / crypto / compress / data_health / rebalance / scheduler).

Rules of this file:
- Only NEW qa3_* files; no existing test or source file is modified.
- Defects found by the sweep keep the suite green via
  @pytest.mark.xfail(reason="qa defect N", strict=False) and are described
  in qa_report_core.md (P0/P1/P2 + reproduction + evidence).
- Every scenario runs in an isolated scratch env (conftest env/enc_env).
"""
import io
import json
import os
import tarfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from backupme.core import cpanel_ingest
from backupme.core import compress as compress_mod
from backupme.core import crypto as crypto_mod
from backupme.core import data_health
from backupme.core import db_lifecycle
from backupme.core import dedup as dedup_mod
from backupme.core import index as index_mod
from backupme.core.database import execute, commit
from backupme.core.downloader import download_file
from backupme.core.uploader import upload_bundle
from backupme.backends import invalidate_cache, get_all_backends
from backupme.backends.base import StorageBackend
from backupme.config import load_config

from tests.helpers import add_backend


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def qa_backup(root: Path, rel: str, data: bytes, backends, cfg=None,
              redundancy: int = 1, **extra) -> dict:
    """backup_bytes twin that accepts an explicit config (chunking overrides)."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    config = cfg if cfg is not None else load_config()
    info = {"local_path": str(p), "source_root": str(root),
            "relative_path": rel}
    info.update(extra)
    results = upload_bundle([info], backends, config, redundancy)
    return results[0]


def qa_restore(file_id: int, root: Path, backend_name: str | None = None) -> bytes:
    dest = Path(root) / "restore_out" / f"qa_{file_id}_{backend_name or 'any'}"
    if dest.exists():
        dest.unlink()
    result = download_file(file_id, str(dest), backend_name)
    assert result.get("status") == "ok", result
    return dest.read_bytes()


def chunky_cfg(root: Path, min_size: str = "64KB", chunk_size: str = "256KB",
               retries: int = 1):
    """Scratch config with small chunking so chunked-path tests stay fast."""
    cfg = load_config()
    cfg.chunking.min_size = min_size
    cfg.chunking.chunk_size = chunk_size
    cfg.verification.retries = retries
    return cfg


def storage_root(backends) -> Path:
    """Local backend base path (where objects physically land)."""
    for b in backends:
        if getattr(b, "type", "") == "local":
            return Path(b.base_path)
    raise AssertionError("no local backend in fixture")


class FlakyBackend(StorageBackend):
    """Backend that fails every write (simulated outage). Unique name per
    instance so the module-global circuit breaker never leaks across tests."""
    type = "flaky"

    def __init__(self, name=None):
        super().__init__(name or f"flaky_{uuid.uuid4().hex[:8]}", {})
        self.upload_calls = 0

    def connect(self):
        return True

    def disconnect(self):
        pass

    def upload(self, local_path, remote_rel_path):
        self.upload_calls += 1
        raise OSError(f"flaky backend {self.name} unavailable")

    def download(self, remote_rel_path, local_path):
        raise OSError("flaky backend unavailable")

    def delete(self, remote_rel_path):
        raise OSError("flaky backend unavailable")

    def exists(self, remote_rel_path):
        return False

    def list_files(self, prefix=""):
        return []

    def walk(self, prefix=""):
        return []

    def free_space(self):
        return 0

    def total_space(self):
        return 0

    def hash(self, remote_rel_path):
        raise OSError("flaky backend unavailable")

    def health(self):
        return False

    def fingerprint(self, remote_rel_path):
        raise OSError("flaky backend unavailable")


def _bundle_member_row(file_id: int):
    """(member_blob_id, parent_blob_id, parent_obj_path, offset, length) for a
    batched file. The parent object path carries the compression/encryption
    suffix (.zstd/.aes); the parent BLOB id does not."""
    chunk = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ?", (file_id,)
    ).fetchone()
    assert chunk is not None
    st = execute(
        "SELECT storage_path FROM blob_storage WHERE blob_id = ?", (chunk[0],)
    ).fetchone()
    parent_path, rng = st[0].split("@", 1)
    off, ln = (int(x) for x in rng.split(":"))
    # bundle/<xx>/<yy>/<64-hex-sha>[.zstd][.aes] -> blob id drops the suffixes
    parts = parent_path.split("/")
    parent_blob_id = "/".join(parts[:3] + [parts[3].split(".")[0]])
    return chunk[0], parent_blob_id, parent_path, off, ln


# ---------------------------------------------------------------------------
# Uploader: batching, dedup donor path, D9 consistency, chunking, concurrency
# ---------------------------------------------------------------------------

def test_batch_bundle_and_dedup_donor_refcounts(env, backends):
    root = Path(env)
    a = qa_backup(root, "docs/a.txt", b"alpha content " * 40, backends)
    b = qa_backup(root, "docs/b.txt", b"beta content " * 40, backends)
    assert a["status"] == "ok" and b["status"] == "ok"
    assert a["bundled"] and b["bundled"]

    # Same content under different names -> donor dedup path.
    a2 = qa_backup(root, "docs/a_copy.txt", b"alpha content " * 40, backends)
    b2 = qa_backup(root, "docs/b_copy.txt", b"beta content " * 40, backends)
    assert a2.get("dedup") and b2.get("dedup")
    assert a2["file_id"] != a["file_id"]  # separate rows, shared chunks

    # Each donor-copied mapping adds one ref to the shared member blob.
    member_a = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ?", (a["file_id"],)
    ).fetchone()[0]
    refs = execute(
        "SELECT COUNT(*) FROM file_chunks WHERE blob_id = ?", (member_a,)
    ).fetchone()[0]
    refcnt = execute(
        "SELECT ref_count FROM blobs WHERE blob_id = ?", (member_a,)
    ).fetchone()[0]
    assert refcnt == refs == 2

    # The parent bundle carries exactly one ref per member blob row.
    _, parent_id, _, _, _ = _bundle_member_row(a["file_id"])
    parent_refs = execute(
        "SELECT ref_count FROM blobs WHERE blob_id = ?", (parent_id,)
    ).fetchone()[0]
    member_rows = execute(
        "SELECT COUNT(*) FROM blobs WHERE blob_id LIKE ? AND blob_id != ?",
        (parent_id + "/%", parent_id),
    ).fetchone()[0]
    assert parent_refs == member_rows

    assert dedup_mod.reconcile_refcounts() == 0
    assert dedup_mod.list_orphans() == []
    assert qa_restore(a["file_id"], root) == b"alpha content " * 40
    assert qa_restore(a2["file_id"], root) == b"alpha content " * 40
    assert qa_restore(b["file_id"], root) == b"beta content " * 40


@pytest.mark.parametrize("mode", ["batch", "chunked", "donor"])
def test_d9_changed_reupload_consistency_all_paths(env, backends, mode):
    """files.sha256 must equal the content just uploaded after EVERY re-upload
    (defect D9 regression guard) — batch, chunked and donor paths alike."""
    import hashlib
    root = Path(env)
    cfg = chunky_cfg(root)
    rel = "seed/doc.bin"

    def db_sha(fid):
        return execute("SELECT sha256 FROM files WHERE id = ?",
                       (fid,)).fetchone()[0]

    if mode == "batch":
        v1, v2 = b"batch version one " * 10, b"batch version two! " * 10
    elif mode == "chunked":
        v1 = bytes(range(256)) * 1024          # 256 KB -> 1 chunk
        v2 = bytes(range(255, -1, -1)) * 1024  # changed -> new chunk
    else:  # donor: rel is re-backed with content already stored elsewhere
        v1, v2 = b"donor payload " * 30, b"other payload " * 30

    if mode == "donor":
        qa_backup(root, "seed/donor.txt", v1, backends, cfg)
        r1 = qa_backup(root, rel, v2, backends, cfg)
        assert db_sha(r1["file_id"]) == r1["sha256"]
        assert qa_restore(r1["file_id"], root) == v2
        r2 = qa_backup(root, rel, v1, backends, cfg)  # cross-file donor hit
        expected, r3 = v1, r2
    else:
        r1 = qa_backup(root, rel, v1, backends, cfg)
        assert db_sha(r1["file_id"]) == r1["sha256"]
        assert qa_restore(r1["file_id"], root) == v1
        r2 = qa_backup(root, rel, v2, backends, cfg)
        assert db_sha(r2["file_id"]) == r2["sha256"]   # after 2nd upload
        assert qa_restore(r2["file_id"], root) == v2
        r3 = qa_backup(root, rel, v2, backends, cfg)   # unchanged re-scan
        expected = v2

    assert r1["file_id"] == r2["file_id"] == r3["file_id"]
    assert db_sha(r3["file_id"]) == r3["sha256"]       # after 3rd upload
    assert qa_restore(r3["file_id"], root) == expected
    row = execute("SELECT sha256, size FROM files WHERE id = ?",
                  (r3["file_id"],)).fetchone()
    assert row["sha256"] == hashlib.sha256(expected).hexdigest()
    assert row["size"] == len(expected)
    assert dedup_mod.reconcile_refcounts() == 0
    assert dedup_mod.list_orphans() == []


@pytest.mark.xfail(reason="qa defect 1: 0-byte files are accepted but their "
                          "bundle member row is @off:0, which the downloader "
                          "rejects (UnrecoverableChunkError)", strict=False)
def test_empty_file_roundtrip(env, backends):
    """A 0-byte file must back up AND restore as 0 bytes."""
    root = Path(env)
    r = qa_backup(root, "seed/empty.txt", b"", backends)
    assert r["status"] == "ok", r
    data = qa_restore(r["file_id"], root)
    assert data == b""


def test_large_file_multichunk_reconcile(env, backends):
    root = Path(env)
    cfg = chunky_cfg(root)
    data = (b"0123456789abcdef" * 4096) * 15  # 960 KB -> 4 chunks of 256 KB
    r = qa_backup(root, "seed/big.bin", data, backends, cfg)
    assert r["status"] == "ok"
    chunks = execute(
        "SELECT seq, blob_id, pt_offset, pt_size FROM file_chunks "
        "WHERE file_id = ? ORDER BY seq", (r["file_id"],)
    ).fetchall()
    assert len(chunks) == 4
    assert [c["pt_offset"] for c in chunks] == [0, 262144, 524288, 786432]
    assert sum(c["pt_size"] for c in chunks) == len(data)
    assert execute("SELECT chunked FROM files WHERE id = ?",
                   (r["file_id"],)).fetchone()[0] == 1
    # Standalone chunk objects (not bundle members) live on the backend.
    for c in chunks:
        st = execute("SELECT COUNT(*) FROM blob_storage WHERE blob_id = ?",
                     (c["blob_id"],)).fetchone()[0]
        assert st >= 1
    assert dedup_mod.reconcile_refcounts() == 0
    assert qa_restore(r["file_id"], root) == data


def test_concurrent_upload_bundles_thread_safety(env, backends):
    root = Path(env)
    cfg = load_config()
    n_threads, per_thread = 4, 4
    results: list[list[dict]] = []
    lock = threading.Lock()

    def worker(t: int):
        files = []
        for i in range(per_thread):
            rel = f"t{t}/file_{i}.bin"
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(f"thread {t} file {i} payload".encode() * 50)
            files.append({"local_path": str(p), "source_root": str(root),
                          "relative_path": rel})
        out = upload_bundle(files, backends, cfg, 1)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not any(t.is_alive() for t in threads)

    flat = [r for out in results for r in out]
    assert len(flat) == n_threads * per_thread
    assert all(r["status"] == "ok" for r in flat), flat
    assert dedup_mod.reconcile_refcounts() == 0
    for r in flat:
        data = qa_restore(r["file_id"], root)
        assert execute("SELECT sha256 FROM files WHERE id = ?",
                       (r["file_id"],)).fetchone()[0] == r["sha256"]


def test_redundancy_two_with_failing_backend_partial(env, backends):
    root = Path(env)
    cfg = chunky_cfg(root)
    flaky = FlakyBackend()
    r = qa_backup(root, "seed/redundant.txt", b"redundant payload " * 20,
                  backends + [flaky], cfg, redundancy=2)
    assert r["status"] == "partial", r
    assert flaky.name in r.get("failed_backends", [])
    assert flaky.upload_calls >= 1
    # Data is still readable from the healthy replica set.
    assert qa_restore(r["file_id"], root) == b"redundant payload " * 20
    # DB only recorded rows for backends that actually accepted the data.
    stored_backends = {row[0] for row in execute(
        "SELECT DISTINCT backend_name FROM blob_storage").fetchall()}
    assert flaky.name not in stored_backends
    assert dedup_mod.reconcile_refcounts() == 0


def test_all_backends_fail_leaves_no_rows(env):
    root = Path(env)
    cfg = chunky_cfg(root)
    flaky = FlakyBackend()
    r_batch = qa_backup(root, "seed/x.txt", b"payload " * 20, [flaky], cfg)
    assert r_batch["status"] == "error"
    r_chunk = qa_backup(root, "seed/y.bin", b"z" * 300_000, [flaky], cfg)
    assert r_chunk["status"] == "error"
    assert execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
    assert execute("SELECT COUNT(*) FROM blob_storage").fetchone()[0] == 0
    assert dedup_mod.reconcile_refcounts() == 0


def test_same_content_twice_in_one_batch(env, backends):
    """Two identical files inside ONE upload_bundle call: one member blob row,
    two chunk mappings, one parent ref — refcounts must stay exact."""
    root = Path(env)
    data = b"duplicated payload " * 40
    files = []
    for name in ("same_one.txt", "same_two.txt"):
        p = root / "same" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        files.append({"local_path": str(p), "source_root": str(root),
                      "relative_path": f"same/{name}"})
    results = upload_bundle(files, backends, load_config(), 1)
    assert all(r["status"] == "ok" for r in results), results
    assert results[0]["file_id"] != results[1]["file_id"]

    member_ids = {row[0] for row in execute(
        "SELECT blob_id FROM file_chunks WHERE file_id IN (?, ?)",
        (results[0]["file_id"], results[1]["file_id"])).fetchall()}
    assert len(member_ids) == 1  # same content -> same member blob
    member_id = member_ids.pop()
    assert execute("SELECT ref_count FROM blobs WHERE blob_id = ?",
                   (member_id,)).fetchone()[0] == 2
    assert dedup_mod.reconcile_refcounts() == 0
    assert qa_restore(results[0]["file_id"], root) == data
    assert qa_restore(results[1]["file_id"], root) == data


def test_source_file_vanishing_mid_batch_reports_error(env, backends):
    """A source that disappears between scan and upload yields a per-file
    error result, never a crash or a ghost DB row."""
    root = Path(env)
    ghost_path = root / "vanish" / "gone.txt"
    results = upload_bundle(
        [{"local_path": str(ghost_path), "source_root": str(root),
          "relative_path": "vanish/gone.txt"}],
        backends, load_config(), 1)
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert "error" in results[0]
    assert execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert dedup_mod.reconcile_refcounts() == 0


# ---------------------------------------------------------------------------
# Downloader: encrypted roundtrip, corrupted object, missing blob
# ---------------------------------------------------------------------------

def test_encrypted_roundtrip_small_and_chunked(enc_env):
    from backupme.backends import get_all_backends
    root = Path(enc_env)
    cfg = chunky_cfg(root)
    backends = get_all_backends(cfg)
    small = qa_backup(root, "enc/small.txt", b"secret small payload " * 5,
                      backends, cfg)
    big = qa_backup(root, "enc/big.bin", bytes(range(256)) * 1024,
                    backends, cfg)
    assert small["status"] == "ok" and big["status"] == "ok"
    assert qa_restore(small["file_id"], root) == b"secret small payload " * 5
    assert qa_restore(big["file_id"], root) == bytes(range(256)) * 1024
    # Everything on disk is encrypted: plaintext markers must not exist.
    marker_small = b"secret small payload"
    leaked = [str(f) for f in storage_root(backends).rglob("*")
              if f.is_file() and f.suffix != ".json"
              and marker_small in f.read_bytes()]
    assert leaked == [], leaked
    n_enc = execute("SELECT COUNT(*) FROM blob_storage WHERE encrypted = 1").fetchone()[0]
    n_all = execute("SELECT COUNT(*) FROM blob_storage").fetchone()[0]
    assert n_all >= 2 and n_enc == n_all
    assert dedup_mod.reconcile_refcounts() == 0


def test_corrupted_stored_object_fails_loudly(env, backends):
    root = Path(env)
    r = qa_backup(root, "seed/victim.txt", b"integrity matters " * 30, backends)
    _, parent_id, parent_path, _, _ = _bundle_member_row(r["file_id"])
    obj = storage_root(backends) / parent_path
    assert obj.exists()
    orig = obj.read_bytes()

    # Flip the tail of the compressed object (payload region, not the 52-byte
    # bundle header) and truncate it — both must fail loudly.
    for damage in ("flip-tail", "truncate"):
        if damage == "flip-tail":
            bad = bytearray(orig)
            bad[-1] ^= 0xFF
            bad[-2] ^= 0xFF
            obj.write_bytes(bytes(bad))
        else:
            obj.write_bytes(orig[: len(orig) // 2])

        dest = root / "restore_out" / "corrupt_victim"
        if dest.exists():
            dest.unlink()
        with pytest.raises(RuntimeError):
            download_file(r["file_id"], str(dest))
        # No partial file may be left that could pass as restored data.
        assert not dest.exists()
        leftovers = list((root / "restore_out").glob("corrupt_victim.part-*"))
        assert leftovers == []
    obj.write_bytes(orig)


def test_missing_blob_clear_error(env, backends):
    root = Path(env)
    r = qa_backup(root, "seed/ghosted.txt", b"soon to vanish " * 30, backends)
    blob_id = execute("SELECT blob_id FROM file_chunks WHERE file_id = ?",
                      (r["file_id"],)).fetchone()[0]
    execute("DELETE FROM blob_storage WHERE blob_id = ?", (blob_id,))
    commit()
    dest = root / "restore_out" / "ghosted"
    with pytest.raises(RuntimeError, match="Failed to download chunk"):
        download_file(r["file_id"], str(dest))
    assert not dest.exists()
    # Unknown id -> FileNotFoundError, not a silent ok.
    with pytest.raises(FileNotFoundError):
        download_file(999999, str(root / "restore_out" / "nope"))
    # data_health agrees the file is now unrecoverable.
    assert data_health.purge_candidate_count() >= 1


# ---------------------------------------------------------------------------
# Dedup: unref cascade, find_unrecoverable, orphans
# ---------------------------------------------------------------------------

def test_unref_cascade_member_to_parent(env, backends):
    root = Path(env)
    r = qa_backup(root, "seed/cascade.txt", b"cascade payload " * 30, backends)
    member_id, parent_id, _, _, _ = _bundle_member_row(r["file_id"])
    assert execute("SELECT COUNT(*) FROM blobs WHERE blob_id = ?",
                   (parent_id,)).fetchone()[0] == 1

    index_mod.hard_delete_file(r["file_id"])  # chunk rows + unref

    # Member row gone; the cascade must also drop the parent bundle row.
    assert execute("SELECT COUNT(*) FROM blobs WHERE blob_id = ?",
                   (member_id,)).fetchone()[0] == 0
    assert execute("SELECT COUNT(*) FROM blobs WHERE blob_id = ?",
                   (parent_id,)).fetchone()[0] == 0
    assert execute("SELECT COUNT(*) FROM blob_storage WHERE blob_id IN (?, ?)",
                   (member_id, parent_id)).fetchone()[0] == 0
    assert dedup_mod.reconcile_refcounts() == 0
    assert dedup_mod.list_orphans() == []


def test_find_unrecoverable_and_orphans(env, backends):
    root = Path(env)
    r = qa_backup(root, "seed/healthy.txt", b"healthy payload " * 30, backends)
    assert dedup_mod.find_unrecoverable() == []
    # Seed legacy damage: a chunk row whose blob row vanished (pre-FK import).
    ghost = "objects/zz/zz/" + "f" * 64
    dedup_mod.ensure_blob(ghost, 10)
    commit()
    execute("INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) "
            "VALUES (?, 99, ?, 0, 10)", (r["file_id"], ghost))
    commit()
    from backupme.core.database import get_connection
    conn = get_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM blobs WHERE blob_id = ?", (ghost,))
    conn.commit()
    lost = dedup_mod.find_unrecoverable()
    assert any(row["blob_id"] == ghost for row in lost)
    conn.execute("PRAGMA foreign_keys = ON")
    # Seed: an orphan blob (row without any reference).
    dedup_mod.ensure_blob("objects/aa/aa/orphan", 5)
    commit()
    orphans = {o["blob_id"] for o in dedup_mod.list_orphans()}
    assert "objects/aa/aa/orphan" in orphans
    # Clean up the seeded damage; pipeline-produced state must be drift-free.
    execute("DELETE FROM file_chunks WHERE file_id = ? AND blob_id = ?",
            (r["file_id"], ghost))
    execute("DELETE FROM blobs WHERE blob_id = 'objects/aa/aa/orphan'")
    commit()
    assert dedup_mod.reconcile_refcounts() == 0
    assert {o["blob_id"] for o in dedup_mod.list_orphans()} == set()


# ---------------------------------------------------------------------------
# Retention: keep-N, soft delete drops refs, hard purge grace semantics
# ---------------------------------------------------------------------------

def _mk_version(root: Path, rel: str, data: bytes, backends, cfg,
                version_seq: int, version_group: str) -> int:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    results = upload_bundle([{
        "local_path": str(p), "source_root": str(root), "relative_path": rel,
        "version_seq": version_seq, "version_group": version_group,
    }], backends, cfg, 1)
    assert results[0]["status"] == "ok", results
    return results[0]["file_id"]


def test_retention_keep_n_versions_drops_blob_refs(env, backends):
    root = Path(env)
    cfg = chunky_cfg(root)
    ids = []
    for seq in range(1, 5):
        ids.append(_mk_version(root, "seed/v.txt",
                               f"version {seq} payload ".encode() * 20,
                               backends, cfg, version_seq=seq,
                               version_group="qa3-vg"))
    # All four versions have live chunks and zero refcount drift.
    assert dedup_mod.reconcile_refcounts() == 0

    # Backdate the two oldest versions so the daily/weekly/monthly windows
    # do not protect them.
    for fid in ids[:2]:
        execute("UPDATE files SET created_at = datetime('now', '-60 days') "
                "WHERE id = ?", (fid,))
    commit()

    from backupme.scheduler import apply_retention
    apply_retention({"retention": {"enabled": True, "versions": 2,
                                   "daily": 0, "weekly": 0, "monthly": 0}})

    deleted = [execute("SELECT deleted_at FROM files WHERE id = ?",
                       (fid,)).fetchone()[0] for fid in ids[:2]]
    assert all(d is not None for d in deleted)
    for fid in ids[:2]:
        assert execute("SELECT COUNT(*) FROM file_chunks WHERE file_id = ?",
                       (fid,)).fetchone()[0] == 0
    # Their unique payloads vanished from the vault; refcounts stayed clean.
    assert dedup_mod.reconcile_refcounts() == 0
    assert dedup_mod.list_orphans() == []
    assert qa_restore(ids[2], root) == f"version 3 payload ".encode() * 20
    assert qa_restore(ids[3], root) == f"version 4 payload ".encode() * 20
    # The unique blobs of the purged versions are gone.
    for fid in ids[:2]:
        chunks = execute("SELECT blob_id FROM file_chunks WHERE file_id = ?",
                         (fid,)).fetchall()
        assert chunks == []


def test_hard_purge_grace_and_watermark(env, backends):
    root = Path(env)
    cfg = load_config()
    a = qa_backup(root, "seed/old.txt", b"old payload " * 30, backends, cfg)
    b = qa_backup(root, "seed/fresh.txt", b"fresh payload " * 30, backends, cfg)

    export = db_lifecycle.cold_export_to_parquet(config=cfg)
    assert export["status"] == "ok" and export["rows"] >= 2
    watermark = export["watermark"]

    index_mod.delete_file(a["file_id"])  # soft delete now
    execute("UPDATE files SET deleted_at = datetime('now', '-30 days') "
            "WHERE id = ?", (a["file_id"],))
    # A row ABOVE the watermark, old and soft-deleted: must be skipped.
    execute(
        "INSERT INTO files (id, source_root, relative_path, sha256, size, mtime, "
        "chunked, redundancy, version_group, version_seq, deleted_at) "
        "VALUES (?, 'seed', 'above_watermark.txt', 'deadbeef', 1, 0, 0, 1, "
        "'aw', 1, datetime('now', '-30 days'))", (watermark + 500,))
    commit()

    out = db_lifecycle.hard_purge_versions(grace_days=14, config=cfg)
    assert out["purged"] == 1
    assert execute("SELECT COUNT(*) FROM files WHERE id = ?",
                   (a["file_id"],)).fetchone()[0] == 0
    assert execute("SELECT COUNT(*) FROM files WHERE id = ?",
                   (b["file_id"],)).fetchone()[0] == 1
    assert execute("SELECT COUNT(*) FROM files WHERE id = ?",
                   (watermark + 500,)).fetchone()[0] == 1

    # DuckDB history still sees the purged row from the Parquet partition.
    hist = db_lifecycle.query_history_duckdb(
        "SELECT relative_path, sha256 FROM files WHERE relative_path = ?",
        ["seed/old.txt"], config=cfg)
    assert hist and hist[0]["sha256"] == a["sha256"]
    cnt = db_lifecycle.query_history_duckdb("SELECT COUNT(*) AS n FROM files",
                                            config=cfg)
    assert cnt[0]["n"] >= 2


# ---------------------------------------------------------------------------
# cpanel ingest: member rejection matrix, staging cleanup, failed-archive op
# ---------------------------------------------------------------------------

def _tar_bytes(members: list[tuple[str, bytes | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            if data is None:  # symlink
                info.type = tarfile.SYMTYPE
                info.linkname = "target"
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_ingest_member_rejection_matrix(env, backends):
    root = Path(env)
    ingest_cfg = load_config()
    ingest_cfg.cpanel_ingest.enabled = True
    ingest_cfg.cpanel_ingest.drop_root = str(root / "drop")
    members = [
        ("homedir/ok.php", b"<?php ok;"),
        ("homedir\\backslash_ok.php", b"<?php bs;"),  # normalized -> ingested
        ("../evil_traversal.txt", b"EVIL1"),
        ("/abs_evil.txt", b"EVIL2"),
        ("C:\\abs_drive_evil.txt", b"EVIL3"),
        ("safe/../../evil_sneak.txt", b"EVIL4"),
        ("homedir/link", None),  # symlink skipped
    ]
    dest = root / "drop" / "acct" / "cpmove-acct.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_tar_bytes(members))

    out = cpanel_ingest.ingest_tree(str(root / "drop"), backends, ingest_cfg)
    assert out["status"] == "ok"
    rows = [r[0] for r in execute(
        "SELECT relative_path FROM files WHERE source_root = 'cpanel/acct'"
    ).fetchall()]
    assert sorted(rows) == ["homedir/backslash_ok.php", "homedir/ok.php"]
    for evil in ("evil_traversal.txt", "abs_evil.txt", "abs_drive_evil.txt",
                 "evil_sneak.txt"):
        assert not any(evil in r for r in rows)
    # Nothing escaped the staging root either.
    staged = [str(f) for f in root.rglob("evil*")]
    assert staged == []


def test_ingest_staging_cleanup_on_member_upload_failure(env):
    root = Path(env)
    cfg = chunky_cfg(root)
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(root / "drop")
    flaky = FlakyBackend()
    dest = root / "drop" / "acct" / "cpmove-acct.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_tar_bytes([
        ("homedir/a.php", b"<?php a;"),
        ("mysql/b.sql", b"SELECT 1;"),
    ]))

    out = cpanel_ingest.ingest_tree(str(root / "drop"), [flaky], cfg)
    r = out["results"][0]
    # Every member failed to upload; no plaintext may remain staged.
    staging = Path(cfg.cpanel_ingest.staging_dir)
    if staging.exists():
        assert all(not p.is_file() for p in staging.rglob("*") if p.is_file())
    assert r.get("errors", 0) == 2 or r.get("status") == "failed"


@pytest.mark.xfail(reason="qa defect 4: ingest_archive marks the op committed "
                          "and moves the archive to .done even when every "
                          "member upload failed — the archive is then skipped "
                          "forever on retry", strict=False)
def test_ingest_all_members_failed_still_marks_committed(env):
    """qa defect: an archive whose members ALL fail to upload is still
    marked committed in op_log and moved to .done — a retry then skips it
    forever (silent data-loss window during a backend outage)."""
    root = Path(env)
    cfg = chunky_cfg(root)
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(root / "drop")
    flaky = FlakyBackend()
    dest = root / "drop" / "acct" / "cpmove-acct.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_tar_bytes([("homedir/a.php", b"<?php a;")]))

    out = cpanel_ingest.ingest_tree(str(root / "drop"), [flaky], cfg)
    r = out["results"][0]
    # Correct behavior: not "ok", archive stays in the drop root for a retry.
    assert r["status"] != "ok"
    assert dest.exists()


def test_ingest_two_accounts_isolated(env, backends):
    root = Path(env)
    ingest_cfg = load_config()
    ingest_cfg.cpanel_ingest.enabled = True
    ingest_cfg.cpanel_ingest.drop_root = str(root / "drop")
    for acct, marker in (("acct1", b"one"), ("acct2", b"two")):
        dest = root / "drop" / acct / f"cpmove-{acct}.tar.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_tar_bytes([
            ("homedir/index.php", b"<?php echo " + marker + b";"),
            ("mysql/db.sql", b"INSERT INTO t VALUES (" + marker + b");"),
        ]))
    out = cpanel_ingest.ingest_tree(str(root / "drop"), backends, ingest_cfg)
    assert out["status"] == "ok" and out["archives"] == 2
    for acct in ("acct1", "acct2"):
        rows = [r[0] for r in execute(
            "SELECT relative_path FROM files WHERE source_root = ?",
            (f"cpanel/{acct}",)).fetchall()]
        assert sorted(rows) == ["homedir/index.php", "mysql/db.sql"]
    assert dedup_mod.reconcile_refcounts() == 0


# ---------------------------------------------------------------------------
# Crypto: capabilities report, roundtrip, wrong key
# ---------------------------------------------------------------------------

def test_crypto_capabilities_report_matches_layout(tmp_path):
    caps = crypto_mod.capabilities()
    assert caps["cipher"] == "aes-256-gcm"
    assert caps["kdf"] in ("argon2id", "pbkdf2-hmac-sha256 (600k iters)")
    key = crypto_mod.generate_key()
    src = tmp_path / "plain.bin"
    src.write_bytes(b"layout probe " * 100)
    enc = tmp_path / "cipher.bin"
    nonce, tag = crypto_mod.encrypt_file(str(src), str(enc), key)
    raw = enc.read_bytes()
    if caps["aead_available"]:
        assert caps["file_layout"] == "tag-at-end (AEAD)"
        # salt(16) + nonce(12) + ct + tag(16): nonce at bytes 16..27,
        # tag must be the LAST 16 bytes.
        assert raw[16:28] == nonce
        assert raw[-16:] == tag
    else:
        assert caps["file_layout"] == "tag-after-nonce (legacy fallback)"
        assert raw[16:32] == tag
    out = tmp_path / "dec.bin"
    crypto_mod.decrypt_file(str(enc), str(out), key)
    assert out.read_bytes() == src.read_bytes()


@pytest.mark.parametrize("size", [0, 1024, 5 * 1024 * 1024])
def test_crypto_roundtrip_sizes(tmp_path, size):
    key = crypto_mod.generate_key()
    data = os.urandom(size)
    ct, nonce, tag = crypto_mod.encrypt(data, key)
    assert crypto_mod.decrypt(ct, key) == data
    assert nonce in ct and tag == ct[-16:]
    # File API roundtrip at the same size.
    src = tmp_path / f"s{size}.bin"
    src.write_bytes(data)
    enc, dec = tmp_path / f"s{size}.enc", tmp_path / f"s{size}.dec"
    crypto_mod.encrypt_file(str(src), str(enc), key)
    crypto_mod.decrypt_file(str(enc), str(dec), key)
    assert dec.read_bytes() == data


def test_wrong_key_fails_loudly(tmp_path, enc_env):
    key = crypto_mod.generate_key()
    other = crypto_mod.generate_key()
    ct, _, _ = crypto_mod.encrypt(b"top secret", key)
    with pytest.raises(Exception):
        crypto_mod.decrypt(ct, other)

    src = tmp_path / "a.bin"
    src.write_bytes(b"top secret file")
    enc = tmp_path / "a.enc"
    crypto_mod.encrypt_file(str(src), str(enc), key)
    with pytest.raises(Exception):
        crypto_mod.decrypt_file(str(enc), str(tmp_path / "a.dec"), other)

    # Through the downloader a wrong configured key must fail the restore,
    # never silently return wrong bytes. download_file loads the key from the
    # configured key_file, so corrupt the key file in place (same path,
    # different key) to simulate a wrong-key deployment.
    from backupme.backends import get_all_backends
    root = Path(enc_env)
    backends = get_all_backends(load_config())
    cfg0 = load_config()
    r = qa_backup(root, "enc/wrong_key.txt", b"wrong key victim " * 10,
                  backends, cfg0)
    key_path = Path(cfg0.encryption.key_file)
    key_path.write_bytes(crypto_mod.generate_key() + b"\n")
    dest = root / "restore_out" / "wrong_key"
    with pytest.raises(RuntimeError):
        download_file(r["file_id"], str(dest))
    assert not dest.exists()


# ---------------------------------------------------------------------------
# Compress: recompress_blobs counters + restore survival on a mixed corpus
# ---------------------------------------------------------------------------

def test_recompress_counters_and_bundle_member_survival(env, backends):
    root = Path(env)
    cfg = chunky_cfg(root)
    batched = qa_backup(root, "mix/small.txt", b"batched member " * 50,
                        backends, cfg)
    chunked = qa_backup(root, "mix/big.bin", b"chunk payload " * 14286,
                        backends, cfg)
    assert batched["status"] == "ok" and chunked["status"] == "ok"
    assert dedup_mod.reconcile_refcounts() == 0

    # Mixed corpus: 1 parent bundle row + 1 member row + 1 standalone chunk.
    # Bundles are SKIPPED (recompressing a parent invalidates its members'
    # @offset:len mappings — the qa defect that made batched files
    # unrestorable); only the standalone chunk is recompressed.
    result = compress_mod.recompress_blobs(algo="zstd", level=1)
    assert result["total_scanned"] == 3, result
    assert result["recompressed"] == 1, result   # standalone chunk only
    assert result["failed"] == 0, result
    assert result["skipped_encrypted"] == 2, result  # member row + parent

    # BOTH restores must survive the migration.
    assert qa_restore(chunked["file_id"], root) == b"chunk payload " * 14286
    assert qa_restore(batched["file_id"], root) == b"batched member " * 50
    assert dedup_mod.reconcile_refcounts() == 0


def test_recompress_bundle_member_restore_survives(env, backends):
    """Fixed qa defect: recompressing used to rename the parent bundle object
    without updating bundle-member storage rows (<bundle>@off:len), making
    every batched file unrestorable. Bundles are now skipped outright and
    their members restore untouched."""
    root = Path(env)
    cfg = chunky_cfg(root)
    batched = qa_backup(root, "mix/only.txt", b"member survives recompress " * 50,
                        backends, cfg)
    compress_mod.recompress_blobs(algo="zstd", level=1)
    assert qa_restore(batched["file_id"], root) == b"member survives recompress " * 50


def test_recompress_cold_backend_cache_still_recompresses(env, backends):
    """Fixed qa defect: recompress_blobs resolved backends via
    get_backend(name, {}) — with a cold instance cache (fresh CLI process)
    that always returned None and every blob was skipped without a failure
    count. It now resolves through the real config."""
    root = Path(env)
    cfg = chunky_cfg(root)
    qa_backup(root, "mix/f1.txt", b"cold cache payload " * 50, backends, cfg)
    qa_backup(root, "mix/f2.bin", b"cold cache chunk " * 30000, backends, cfg)
    invalidate_cache()  # simulate a fresh process (empty instance cache)
    result = compress_mod.recompress_blobs(algo="zstd", level=1)
    assert result["recompressed"] > 0, result


def test_recompress_skips_encrypted(enc_env):
    from backupme.backends import get_all_backends
    root = Path(enc_env)
    cfg = chunky_cfg(root)
    backends = get_all_backends(cfg)
    r = qa_backup(root, "enc/secret.txt", b"encrypted corpus " * 50,
                  backends, cfg)
    assert r["status"] == "ok"
    before = execute("SELECT COUNT(*) FROM blob_storage").fetchone()[0]
    result = compress_mod.recompress_blobs(algo="zstd", level=1)
    assert result["skipped_encrypted"] >= 1
    assert result["recompressed"] == 0
    after = execute("SELECT COUNT(*) FROM blob_storage").fetchone()[0]
    assert after == before
    assert qa_restore(r["file_id"], root) == b"encrypted corpus " * 50


# ---------------------------------------------------------------------------
# data_health: buckets consistent with seeded damage
# ---------------------------------------------------------------------------

def test_data_health_buckets_after_seeded_damage(env, backends):
    root = Path(env)
    cfg = load_config()
    healthy = qa_backup(root, "dh/healthy.txt", b"healthy " * 40, backends, cfg)
    victim = qa_backup(root, "dh/victim.txt", b"victim " * 40, backends, cfg)
    assert healthy["status"] == "ok" and victim["status"] == "ok"

    s0 = data_health.summary()
    assert s0["missing_blob_files"] == 0
    assert s0["purge_candidates"] == 0
    assert s0["live_files"] >= 2

    # Seed damage: drop the only storage row of the victim's blob.
    blob_id = execute("SELECT blob_id FROM file_chunks WHERE file_id = ?",
                      (victim["file_id"],)).fetchone()[0]
    execute("DELETE FROM blob_storage WHERE blob_id = ?", (blob_id,))
    commit()

    s1 = data_health.summary()
    assert s1["missing_blob_files"] >= 1
    assert s1["missing_blobs"] >= 1
    assert s1["purge_candidates"] >= 1

    drill = data_health.issues(data_health.REASON_MISSING_BLOB, limit=100)
    assert any(item["file_id"] == victim["file_id"] for item in drill["items"])
    assert drill["total"] == s1["missing_blob_files"]
    cand = data_health.purge_candidates(limit=100)
    assert any(item["file_id"] == victim["file_id"] for item in cand["items"])
    assert data_health.filter_purge_candidate_ids(
        [healthy["file_id"], victim["file_id"]]) == {victim["file_id"]}
    # Unknown reason is rejected (route maps to 400).
    with pytest.raises(ValueError):
        data_health.issues("nonsense")


# ---------------------------------------------------------------------------
# Rebalance / replicate / drain
# ---------------------------------------------------------------------------

def test_replicate_to_n_and_restore_from_either_backend(env, backends):
    root = Path(env)
    add_backend(root, "second_local", root.parent / "storage2")
    cfg = chunky_cfg(root)
    all_backends = get_all_backends(cfg)
    names = {b.name for b in all_backends}
    assert "second_local" in names

    from backupme.mirror import replicate_to_n
    batched = qa_backup(root, "repl/small.txt", b"replicate me " * 40,
                        all_backends[:1], cfg)
    chunked = qa_backup(root, "repl/big.bin", b"chunky replicate " * 30000,
                        all_backends[:1], cfg)

    # Replicate top-level blobs (bundle parent + chunk blob) to the target.
    copied = 0
    for blob_id in [row[0] for row in execute(
            "SELECT DISTINCT blob_id FROM blob_storage WHERE backend_name = ? "
            "AND blob_id NOT LIKE 'bundle/%/%/%/%'",
            (all_backends[0].name,)).fetchall()]:
        copied += replicate_to_n(blob_id, [all_backends[1]], cfg)
    assert copied >= 2
    assert execute("SELECT COUNT(*) FROM blob_storage WHERE backend_name = ?",
                   ("second_local",)).fetchone()[0] >= 2

    # Restore from either backend explicitly.
    assert qa_restore(batched["file_id"], root, all_backends[0].name) == b"replicate me " * 40
    assert qa_restore(chunked["file_id"], root, all_backends[0].name) == b"chunky replicate " * 30000
    assert qa_restore(chunked["file_id"], root, "second_local") == b"chunky replicate " * 30000
    assert qa_restore(batched["file_id"], root, "second_local") == b"replicate me " * 40
    assert dedup_mod.reconcile_refcounts() == 0


def test_rebalance_copies_top_level_only(env, backends):
    root = Path(env)
    add_backend(root, "rebal_target", root.parent / "storage3")
    cfg = chunky_cfg(root)
    all_backends = get_all_backends(cfg)
    target = next(b for b in all_backends if b.name == "rebal_target")
    first = all_backends[0]

    batched = qa_backup(root, "reb/small.txt", b"rebalance member " * 40,
                        [first], cfg)
    chunked = qa_backup(root, "reb/big.bin", b"rebalance chunk " * 30000,
                        [first], cfg)
    member_id, parent_id, parent_path, _, _ = _bundle_member_row(batched["file_id"])

    from backupme.core.rebalance import rebalance_to, plan_rebalance, drain_backend
    plan = plan_rebalance("rebal_target")
    assert plan["blobs"] >= 2  # parent bundle + chunk blob (members excluded)
    out = rebalance_to("rebal_target", all_backends, cfg)
    assert out["status"] == "ok", out
    assert out["copied"] >= 2
    assert out["remaining_blobs"] == 0

    # Member byte-range rows must NOT be copied as standalone objects.
    member_on_target = execute(
        "SELECT COUNT(*) FROM blob_storage WHERE backend_name = ? AND blob_id = ?",
        ("rebal_target", member_id)).fetchone()[0]
    assert member_on_target == 0
    parent_on_target = execute(
        "SELECT COUNT(*) FROM blob_storage WHERE backend_name = ? AND blob_id = ?",
        ("rebal_target", parent_id)).fetchone()[0]
    assert parent_on_target == 1

    assert qa_restore(batched["file_id"], root) == b"rebalance member " * 40
    assert qa_restore(chunked["file_id"], root) == b"rebalance chunk " * 30000
    assert dedup_mod.reconcile_refcounts() == 0

    # Drain dry-run reports, deletes nothing.
    files_before = sorted(f["path"] for f in first.list_files())
    dry = drain_backend(first.name, all_backends, cfg, dry_run=True)
    assert dry["status"] == "dry-run"
    assert first.list_files() and sorted(
        f["path"] for f in first.list_files()) == files_before

    # Real drain: copies away single-replica blobs, never deletes.
    drained = drain_backend(first.name, all_backends, cfg)
    assert drained["status"] == "ok", drained
    files_after = sorted(f["path"] for f in first.list_files())
    assert files_after == files_before  # nothing was deleted remotely
    assert drained["remaining_only_here_blobs"] == 0
    assert qa_restore(batched["file_id"], root) == b"rebalance member " * 40


# ---------------------------------------------------------------------------
# Scheduler: exception guard, single-flight locks, health regression warning
# ---------------------------------------------------------------------------

def _sched_cfg(root: Path) -> dict:
    from dataclasses import asdict
    cfg = asdict(load_config())
    cfg["scheduler"] = {"interval": 3600, "ingest_interval": 300,
                        "gc_interval_days": 7, "verify_interval_days": 1}
    cfg["cpanel_ingest"] = {"enabled": True, "drop_root": str(root / "drop")}
    return cfg


def test_periodic_loop_survives_failing_stage(env, monkeypatch):
    from backupme.scheduler import BackupScheduler
    root = Path(env)
    sched = BackupScheduler(_sched_cfg(root))

    calls = {"backups": 0, "retention": 0}

    def boom():
        calls["backups"] += 1
        sched._stop.set()  # end the loop after this failing pass
        raise RuntimeError("injected backup stage failure")

    def retention_probe():
        calls["retention"] += 1

    monkeypatch.setattr(sched, "_run_backups", boom)
    monkeypatch.setattr(sched, "_run_retention", retention_probe)
    # wait() returns immediately; the stop event set inside boom() ends the loop.
    monkeypatch.setattr(sched._stop, "wait", lambda timeout: None)

    sched._periodic_loop()  # must not raise
    assert calls["backups"] == 1


def test_single_flight_locks_are_nonblocking(env, monkeypatch):
    from backupme.scheduler import BackupScheduler
    root = Path(env)
    cfg = _sched_cfg(root)
    drop = root / "drop"
    drop.mkdir(parents=True, exist_ok=True)
    (drop / "acct").mkdir(exist_ok=True)
    (drop / "acct" / "cpmove-acct.tar.gz").write_bytes(
        _tar_bytes([("homedir/a.php", b"<?php a;")]))
    sched = BackupScheduler(cfg)

    ingest_calls = []
    from backupme.core import cpanel_ingest as ci_mod
    real_ingest_tree = ci_mod.ingest_tree
    monkeypatch.setattr(ci_mod, "ingest_tree",
                        lambda *a, **k: ingest_calls.append(1)
                        or real_ingest_tree(*a, **k))

    # Lock already held elsewhere -> the tick must return immediately
    # WITHOUT running another scan.
    assert sched._ingest_lock.acquire(blocking=False)
    try:
        t0 = time.monotonic()
        sched._run_ingest()
        assert time.monotonic() - t0 < 5
        assert ingest_calls == []
    finally:
        sched._ingest_lock.release()

    # Free lock -> the tick actually runs the scan.
    sched._run_ingest()
    assert len(ingest_calls) == 1
    # Same semantics for the gc lock.
    gc_calls = []
    from backupme.core import gc as gc_mod
    real_gc = gc_mod.run_gc
    monkeypatch.setattr(gc_mod, "run_gc",
                        lambda *a, **k: gc_calls.append(1) or real_gc(*a, **k))
    assert sched._gc_lock.acquire(blocking=False)
    try:
        sched._run_scheduled_gc()
        assert gc_calls == []
    finally:
        sched._gc_lock.release()
    sched._run_scheduled_gc()
    assert len(gc_calls) == 1


def test_health_tick_regression_warning(env, backends, monkeypatch, caplog):
    import logging
    from backupme.scheduler import BackupScheduler
    root = Path(env)
    cfg = _sched_cfg(root)
    r = qa_backup(root, "hl/file.txt", b"health probe " * 40, backends,
                  load_config())
    assert r["status"] == "ok"

    notified = []
    from backupme.core import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify_failure",
                        lambda title, detail, config=None:
                        notified.append((title, detail)))

    sched = BackupScheduler(cfg)
    with caplog.at_level(logging.WARNING, logger="scheduler"):
        sched._log_health()          # baseline snapshot
        assert "regression" not in caplog.text.lower()

        blob_id = execute("SELECT blob_id FROM file_chunks WHERE file_id = ?",
                          (r["file_id"],)).fetchone()[0]
        execute("DELETE FROM blob_storage WHERE blob_id = ?", (blob_id,))
        commit()

        caplog.clear()
        sched._log_health()          # regression must be warned about
        assert "data health regression" in caplog.text.lower()
        assert notified and "regression" in notified[0][0].lower()

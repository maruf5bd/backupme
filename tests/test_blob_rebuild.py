"""blob_rebuild (D14): rebuild physically-missing blobs from verified
restore artifacts.

Scenarios: end-to-end rebuild of destroyed objects through the real
pipeline, plaintext-sha gate verification, restore-after-rebuild,
dry-run, and byte-exact orphan copy from a legacy root.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from backupme.core import blob_rebuild
from backupme.core import compress as compress_mod
from backupme.core.database import commit, execute
from backupme.core.downloader import download_file
from backupme.core.uploader import upload_bundle
from tests.helpers import add_backend, load_cfg


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _wipe_objects(root: Path) -> int:
    removed = 0
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            os.remove(os.path.join(dp, f))
            removed += 1
    return removed


FILES = {
    "big1.bin": os.urandom(12_000_000),  # > 10 MB vault threshold -> standalone chunk blob
    "big2.bin": os.urandom(11_000_000),  # standalone too, incompressible (raw object)
}


def _backup_two_files(storage_root: Path, backends):
    src_dir = storage_root.parent / "src"
    src_dir.mkdir(exist_ok=True)
    entries = []
    for name, data in FILES.items():
        p = src_dir / name
        p.write_bytes(data)
        entries.append({"local_path": str(p), "source_root": str(src_dir), "relative_path": name})
    results = upload_bundle(entries, backends, load_cfg(), 1)
    commit()
    file_ids = [r["file_id"] for r in results]
    assert all(file_ids), results
    return file_ids


def _make_restore_tree(storage_root: Path, file_ids) -> Path:
    restored = storage_root.parent / "restored"
    restored.mkdir(exist_ok=True)
    ok = {}
    for fid, (name, data) in zip(file_ids, FILES.items()):
        (restored / name).write_bytes(data)
        ok[str(fid)] = {"path": str(restored / name), "sha256": _sha(data), "size": len(data)}
    state_path = storage_root.parent / "state.json"
    state_path.write_text(json.dumps({"ok": ok, "fail": {}}), encoding="utf-8")
    return state_path


def test_rebuild_end_to_end(env, backends):
    cfg = load_cfg()
    storage_root = Path(cfg.backends["local"][0].path)
    file_ids = _backup_two_files(storage_root, backends)

    n = execute(
        "SELECT count(*) AS n FROM blob_storage WHERE backend_name = 'test_local'"
    ).fetchone()["n"]
    assert n >= 2

    # destroy every object physically — the DB still references them (D14 shape)
    assert _wipe_objects(storage_root) >= 2
    ref = execute("SELECT count(*) AS n FROM blobs WHERE ref_count > 0").fetchone()["n"]
    assert ref >= 2

    state_path = _make_restore_tree(storage_root, file_ids)
    report = blob_rebuild.rebuild_blobs(
        cfg, str(state_path), extra_roots=[], target_backend_name="test_local"
    )
    assert report["status"] == "ok", report
    assert report["rebuilt"] >= 2, report
    assert report["failed"] == [], report
    assert report["unrecoverable"] == 0, report

    # every stored row must decompress to plaintext matching the blob tail
    rows = execute(
        "SELECT blob_id, storage_path, compression_algo FROM blob_storage "
        "WHERE backend_name = 'test_local'"
    ).fetchall()
    assert len(rows) >= 2
    for r in rows:
        obj = storage_root / r["storage_path"]
        assert obj.is_file(), r["storage_path"]
        stored = obj.read_bytes()
        plaintext = (
            stored if not r["compression_algo"]
            else compress_mod.decompress(stored, r["compression_algo"])
        )
        assert _sha(plaintext) == r["blob_id"].rsplit("/", 1)[-1]

    # end-to-end: the files restore through the real pipeline again
    for fid, (name, data) in zip(file_ids, FILES.items()):
        dest = storage_root.parent / "out" / name
        result = download_file(fid, str(dest))
        assert result.get("status") != "error", result
        assert dest.read_bytes() == data


def test_dry_run_then_orphan_copy(env, backends):
    cfg = load_cfg()
    storage_root = Path(cfg.backends["local"][0].path)
    orphan_root = storage_root.parent / "orphan_store"
    orphan_root.mkdir(exist_ok=True)
    add_backend(env, "orphan", orphan_root)

    file_ids = _backup_two_files(storage_root, backends)
    state_path = _make_restore_tree(storage_root, file_ids)

    rows = execute(
        "SELECT blob_id, storage_path, stored_sha256, compression_algo, compression_level "
        "FROM blob_storage WHERE backend_name = 'test_local' ORDER BY blob_id"
    ).fetchall()
    target_row = rows[0]

    # plant a byte-identical object in the orphan root (legacy store)
    fr = execute(
        "SELECT file_id, pt_offset, pt_size FROM file_chunks WHERE blob_id = ?",
        (target_row["blob_id"],),
    ).fetchone()
    info = json.loads(state_path.read_text(encoding="utf-8"))["ok"][str(fr["file_id"])]
    data = Path(info["path"]).read_bytes()[fr["pt_offset"]:fr["pt_offset"] + fr["pt_size"]]
    stored = (
        data if not target_row["compression_algo"]
        else compress_mod.compress(data, target_row["compression_algo"],
                                   target_row["compression_level"] or 3)
    )
    dest = orphan_root / target_row["storage_path"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(stored)
    assert _sha(stored) == target_row["stored_sha256"]

    # destroy the live objects, keep the orphan
    _wipe_objects(storage_root)

    # dry run: plans the copy, writes nothing
    report = blob_rebuild.rebuild_blobs(
        cfg, str(state_path), extra_roots=[str(orphan_root)],
        target_backend_name="test_local", dry_run=True,
    )
    assert report["planned_copies"] == 1, report
    assert not (storage_root / target_row["storage_path"]).exists()

    # real run: orphan object copied back byte-identical, others rebuilt
    report = blob_rebuild.rebuild_blobs(
        cfg, str(state_path), extra_roots=[str(orphan_root)],
        target_backend_name="test_local",
    )
    assert report["status"] == "ok", report
    assert report["orphan_copied"] == 1, report
    copied = storage_root / target_row["storage_path"]
    assert copied.is_file()
    assert _sha(copied.read_bytes()) == target_row["stored_sha256"]

    # end-to-end restore of the copied blob's file
    fr_file_id = fr["file_id"]
    dest2 = storage_root.parent / "out2" / "orphan_file"
    result = download_file(fr_file_id, str(dest2))
    assert result.get("status") != "error", result


def test_unrecoverable_accounting(env, backends):
    cfg = load_cfg()
    storage_root = Path(cfg.backends["local"][0].path)
    file_ids = _backup_two_files(storage_root, backends)

    # restore tree covers only the FIRST file — the other blob is lost
    restored = storage_root.parent / "restored3"
    restored.mkdir(exist_ok=True)
    first_name, first = next(iter(FILES.items()))
    (restored / first_name).write_bytes(first)
    ok = {str(file_ids[0]): {"path": str(restored / first_name),
                             "sha256": _sha(first), "size": len(first)}}
    state_path = storage_root.parent / "state3.json"
    state_path.write_text(json.dumps({"ok": ok, "fail": {}}), encoding="utf-8")

    _wipe_objects(storage_root)
    report = blob_rebuild.rebuild_blobs(
        cfg, str(state_path), extra_roots=[], target_backend_name="test_local"
    )
    assert report["status"] == "partial", report
    assert report["rebuilt"] >= 1, report
    assert report["unrecoverable"] >= 1, report
    assert report["failed"] == [], report  # no op failed; the blob is simply unrecoverable

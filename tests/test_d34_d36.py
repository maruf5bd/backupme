"""D34/D36 defect-fix tests.

Scheduler/retention edge cases live in test_scheduler_automation.py. This
file covers the integrity/verification fixes: INT-2 (re-upload after a
retention soft-delete must not hit the files UNIQUE constraint), INT-3
(auto-heal source re-back), the unsafe-name guards (NTFS ADS, reserved
device names, literal ".."), and the BackupDef.redundancy wiring.
"""
import pytest

import backupme.core.uploader as uploader_mod
from backupme.core import index
from backupme.core.database import execute, commit
from backupme.core.downloader import download_file
from backupme.core.uploader import upload_bundle
from tests.helpers import backup_bytes, restore_bytes, load_cfg


# ---------------------------------------------------------------------------
# INT-2: re-upload after a retention soft-delete
# ---------------------------------------------------------------------------

def test_reupload_after_soft_delete_allocates_next_seq(env, backends):
    res1 = backup_bytes(env, "docs/a.txt", b"hello", backends)
    index.delete_file(res1["file_id"])  # soft-delete the only version
    res2 = backup_bytes(env, "docs/a.txt", b"hello", backends)
    assert res2["status"] == "ok", res2
    assert res2["file_id"] != res1["file_id"]
    seqs = [r[0] for r in execute(
        "SELECT version_seq FROM files WHERE source_root=? AND relative_path='docs/a.txt' "
        "ORDER BY version_seq", (str(env),)).fetchall()]
    # The soft-deleted seq is never reused; the new live row got the next one.
    assert seqs == [1, 2]
    assert restore_bytes(res2["file_id"], env) == b"hello"


def test_reupload_after_soft_delete_batch_not_rolled_back(env, backends):
    r1 = backup_bytes(env, "docs/a.txt", b"AAA", backends)
    backup_bytes(env, "docs/b.txt", b"BBB", backends)
    index.delete_file(r1["file_id"])
    # One batch mixing a soft-deleted-latest path with a live one: before the
    # fix the UNIQUE violation rolled back the WHOLE bundle (both files).
    pa = env / "docs" / "a.txt"
    pa.write_bytes(b"A2A")
    pb = env / "docs" / "b.txt"
    pb.write_bytes(b"B2B")
    results = upload_bundle([
        {"local_path": str(pa), "source_root": str(env), "relative_path": "docs/a.txt"},
        {"local_path": str(pb), "source_root": str(env), "relative_path": "docs/b.txt"},
    ], backends, load_cfg(), 1)
    assert [r["status"] for r in results] == ["ok", "ok"], results
    a_seqs = sorted(r[0] for r in execute(
        "SELECT version_seq FROM files WHERE source_root=? AND relative_path='docs/a.txt'",
        (str(env),)).fetchall())
    assert a_seqs == [1, 2]


def test_index_file_allocates_next_seq_when_key_soft_deleted(env):
    """Direct index_file writers (e.g. archive registration) get the same
    protection as the upload pipeline."""
    fid = index.index_file("arch/abc", "member.bin", "s" * 64, 10, version_seq=1)
    index.delete_file(fid)
    fid2 = index.index_file("arch/abc", "member.bin", "t" * 64, 12, version_seq=1)
    row = execute("SELECT version_seq FROM files WHERE id=?", (fid2,)).fetchone()
    assert row["version_seq"] == 2


# ---------------------------------------------------------------------------
# INT-3: auto-heal source re-back
# ---------------------------------------------------------------------------

def _wipe_blob_storage(file_id: int) -> str:
    blob_id = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id=?", (file_id,)).fetchone()[0]
    execute("DELETE FROM blob_storage WHERE blob_id=?", (blob_id,))
    commit()
    return blob_id


def test_heal_from_source_rebacks_blob(env, backends):
    res = backup_bytes(env, "src/doc.txt", b"heal me " * 40, backends)
    fid = res["file_id"]
    _wipe_blob_storage(fid)
    from backupme.core.verify import heal_damaged_file
    out = heal_damaged_file(fid)
    assert out["status"] == "healed", out
    # Every chunk maps to a blob that now has a stored copy again.
    chunks = execute("SELECT blob_id FROM file_chunks WHERE file_id=?", (fid,)).fetchall()
    assert chunks and all(
        execute("SELECT 1 FROM blob_storage WHERE blob_id=?", (c["blob_id"],)).fetchone()
        for c in chunks)
    assert restore_bytes(fid, env) == b"heal me " * 40


def test_integrity_check_auto_heals_from_source(env, backends):
    res = backup_bytes(env, "src/doc2.txt", b"auto heal " * 40, backends)
    _wipe_blob_storage(res["file_id"])
    from backupme.core.verify import run_integrity_check
    out = run_integrity_check(check_type="random", count=5, auto_heal=True)
    assert out["status"] == "completed", out
    assert out["failed"] == 0
    assert out["checked"] >= 1
    assert restore_bytes(res["file_id"], env) == b"auto heal " * 40


# ---------------------------------------------------------------------------
# D36 / SEC T7: unsafe name guards
# ---------------------------------------------------------------------------

def _evil_relative_paths():
    return [
        "evil.txt:stream",   # NTFS alternate data stream
        "CON",               # reserved device name
        "logs/CON.txt",      # reserved base name with an extension
        "sub/NUL",           # reserved name in a middle segment
        "a/../b.txt",        # literal .. segment
        "..\\evil.txt",      # traversal written with backslashes
    ]


def test_uploads_reject_unsafe_names_per_file(env, backends):
    good = env / "ok.txt"
    good.write_bytes(b"fine " * 30)
    infos = [{"local_path": str(good), "source_root": str(env),
              "relative_path": "ok.txt"}]
    # The DISK names stay harmless; only the indexed relative_path is unsafe.
    for i, rel in enumerate(_evil_relative_paths()):
        p = env / f"evil_{i}.bin"
        p.write_bytes(b"bad " * 10)
        infos.append({"local_path": str(p), "source_root": str(env),
                      "relative_path": rel})
    results = upload_bundle(infos, backends, load_cfg(), 1)
    assert len(results) == len(infos)
    # Bundled files report last; find the safe sibling by its local_path.
    good_result = next(r for r in results if r.get("local_path") == str(good))
    assert good_result["status"] == "ok", "the safe sibling must upload normally"
    for r in results:
        if r is good_result:
            continue
        assert r["status"] == "error", r
        assert r["error"], "each rejection carries a clear reason"
    # Nothing unsafe was indexed.
    for rel in _evil_relative_paths():
        n = execute("SELECT COUNT(*) FROM files WHERE relative_path=?",
                    (rel,)).fetchone()[0]
        assert n == 0, rel


def test_restore_dest_rejects_unsafe_names(env, backends):
    res = backup_bytes(env, "doc.txt", b"payload " * 30, backends)
    for bad in ("evil.txt:stream", "CON", ".."):
        dest = env / "restore_out" / bad
        with pytest.raises(ValueError):
            download_file(res["file_id"], str(dest))
    # The guard fires before anything is created (no restore_out dir at all).
    assert not (env / "restore_out").exists()


def test_path_guard_reasons():
    from backupme.core import path_guard
    assert path_guard.unsafe_relative_path_reason("docs/report.txt") is None
    assert path_guard.unsafe_relative_path_reason("deep/nested/file.bin") is None
    assert "alternate-stream" in path_guard.unsafe_relative_path_reason("a.txt:ads")
    assert "reserved" in path_guard.unsafe_relative_path_reason("con.txt")
    assert "reserved" in path_guard.unsafe_relative_path_reason("x/COM3/y")
    assert "'..'" in path_guard.unsafe_relative_path_reason("a/../b")
    assert path_guard.unsafe_dest_filename_reason("out.bin") is None
    assert path_guard.unsafe_dest_filename_reason("..") is not None


# ---------------------------------------------------------------------------
# D34: BackupDef.redundancy reaches the daemon upload calls
# ---------------------------------------------------------------------------

def test_backup_def_redundancy_parsed_from_yaml(env):
    from backupme.config import load_config
    p = env / "red.yaml"
    p.write_text(
        "backups:\n"
        f"- source: {(env / 'srcdata').as_posix()}\n"
        "  redundancy: 3\n"
    )
    cfg = load_config(str(p))
    assert cfg.backups[0].redundancy == 3


def test_daemon_pass_resolves_redundancy_from_entry(env, monkeypatch):
    srcdir = env / "srcdata"
    srcdir.mkdir()
    (srcdir / "f.txt").write_bytes(b"redd " * 20)

    import dataclasses
    from backupme.config import load_config
    from backupme.scheduler import BackupScheduler

    captured = {}

    def fake_upload(files, bks, config, redundancy=1):
        captured["redundancy"] = redundancy
        return []

    monkeypatch.setattr(uploader_mod, "upload_bundle", fake_upload)

    base = dataclasses.asdict(load_config())
    cfg = dict(base)
    cfg["backups"] = [{"source": str(srcdir), "redundancy": 2}]
    BackupScheduler(cfg)._backups_pass()
    assert captured == {"redundancy": 2}

    # Garbage redundancy falls back to 1 instead of crashing the pass.
    captured.clear()
    cfg2 = dict(base)
    cfg2["backups"] = [{"source": str(srcdir), "redundancy": "x"}]
    BackupScheduler(cfg2)._backups_pass()
    assert captured == {"redundancy": 1}

"""backend rescan --register: verify-and-register objects the DB lost rows for.

Every test runs against the isolated `env` fixture (scratch config + DB +
local backend) and backs files up through the REAL pipeline before deleting
the backend's blob_storage rows in SQL — mirroring the recovery scenario the
command exists for (database loss/rewrite, backend still holds the objects).
"""
import hashlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli
from backupme.core.backend_register import register_backend_objects
from backupme.core.database import commit, execute
from tests.helpers import backup_bytes, restore_bytes


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def chunked_cfg(env):
    """Force the chunked path so top-level objects/<t2>/<t4>/<sha> blobs exist
    alongside the bundle path small files take by default."""
    cfg_path = env / "config.yaml"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8")
        + "chunking:\n  min_size: 1024\n  chunk_size: 8192\n",
        encoding="utf-8",
    )


def invoke(runner, env, *args):
    """Invoke the CLI pinned to the isolated env's config and DB."""
    return runner.invoke(
        cli,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"), *args],
    )


def _load_cfg():
    from backupme.config import load_config
    return load_config()


def _top_level_rows(backend_name):
    """Non-member storage rows on one backend (bundle members ride parents)."""
    return execute(
        "SELECT blob_id, storage_path FROM blob_storage "
        "WHERE backend_name = ? AND storage_path NOT LIKE '%@%'",
        (backend_name,),
    ).fetchall()


def _delete_top_level_rows(backend_name):
    execute(
        "DELETE FROM blob_storage WHERE backend_name = ? AND storage_path NOT LIKE '%@%'",
        (backend_name,),
    )
    commit()


def _blob_object(bk, storage_path: str) -> Path:
    return Path(bk.base_path) / storage_path


# ---------------------------------------------------------------------------
# (a) deleted rows are recreated and restore works end-to-end again
# ---------------------------------------------------------------------------

def test_register_recreates_deleted_rows_and_restore_works(env, backends, chunked_cfg):
    bk = backends[0]
    big_data = os.urandom(20_000)          # chunked: several objects/ blobs
    small_data = b"small-bundled-payload"  # bundled: one bundle/ parent
    res_big = backup_bytes(env, "docs/big.bin", big_data, backends)
    res_small = backup_bytes(env, "docs/small.bin", small_data, backends)
    assert res_big["status"] == "ok", res_big
    assert res_small["status"] == "ok", res_small

    lost = _top_level_rows(bk.name)
    assert len(lost) >= 2, "expected chunk blob(s) plus a bundle parent"
    _delete_top_level_rows(bk.name)
    assert _top_level_rows(bk.name) == []

    report = register_backend_objects(bk, _load_cfg())
    assert report["status"] == "ok", report
    assert report["planned"] == len(lost)
    assert report["registered"] == len(lost)
    assert report["failed"] == []
    assert report["not_found"] == []
    # rows are back, pointing at the same objects
    restored_rows = {r["storage_path"] for r in _top_level_rows(bk.name)}
    assert restored_rows == {r["storage_path"] for r in lost}

    # end-to-end restore through the real downloader
    assert restore_bytes(res_big["file_id"], env) == big_data
    assert restore_bytes(res_small["file_id"], env) == small_data


# ---------------------------------------------------------------------------
# (b) a corrupt staged object is rejected with a reason, never registered
# ---------------------------------------------------------------------------

def test_register_rejects_corrupt_object_with_reason(env, backends, chunked_cfg):
    bk = backends[0]
    data = os.urandom(20_000)
    res = backup_bytes(env, "docs/corruptme.bin", data, backends)
    assert res["status"] == "ok", res

    rows = _top_level_rows(bk.name)
    assert rows, "expected at least one top-level object"
    _delete_top_level_rows(bk.name)

    # corrupt EVERY staged object (truncate the stored bytes)
    for r in rows:
        p = _blob_object(bk, r["storage_path"])
        raw = p.read_bytes()
        assert len(raw) > 2
        p.write_bytes(raw[: len(raw) // 2])

    report = register_backend_objects(bk, _load_cfg())
    assert report["registered"] == 0
    assert report["status"] == "partial"
    failed_ids = {f["blob_id"] for f in report["failed"]}
    assert failed_ids == {r["blob_id"] for r in rows}
    reasons = " ".join(f["reason"] for f in report["failed"])
    assert reasons, "failure reason missing"
    assert "decompress failed" in reasons or "sha mismatch" in reasons
    # nothing was registered
    assert _top_level_rows(bk.name) == []


# ---------------------------------------------------------------------------
# (c) dry-run verifies but writes nothing
# ---------------------------------------------------------------------------

def test_register_dry_run_writes_no_rows(env, backends, chunked_cfg):
    bk = backends[0]
    res = backup_bytes(env, "docs/dry.bin", os.urandom(20_000), backends)
    assert res["status"] == "ok", res
    _delete_top_level_rows(bk.name)

    before = execute("SELECT COUNT(*) AS c FROM blob_storage WHERE backend_name = ?",
                     (bk.name,)).fetchone()["c"]
    report = register_backend_objects(bk, _load_cfg(), dry_run=True)
    assert report["dry_run"] is True
    assert report["registered"] == 0
    assert report["would_register"] >= 1
    assert report["failed"] == []
    assert report["not_found"] == []
    assert not (env / "register_checkpoint_test_local.json").exists()

    after = execute("SELECT COUNT(*) AS c FROM blob_storage WHERE backend_name = ?",
                    (bk.name,)).fetchone()["c"]
    assert after == before
    assert _top_level_rows(bk.name) == []


# ---------------------------------------------------------------------------
# (d) checkpoint resume: limited run then a completing rerun
# ---------------------------------------------------------------------------

def test_register_checkpoint_resume(env, backends, chunked_cfg):
    bk = backends[0]
    for i in range(3):
        res = backup_bytes(env, f"docs/resume{i}.bin", os.urandom(20_000), backends)
        assert res["status"] == "ok", res
    lost = _top_level_rows(bk.name)
    total = len(lost)
    assert total >= 3
    _delete_top_level_rows(bk.name)

    ck_path = env / "register_checkpoint_test_local.json"
    first = register_backend_objects(bk, _load_cfg(), limit=1)
    assert first["status"] == "ok"
    assert first["planned"] == 1
    assert first["registered"] == 1
    assert ck_path.exists(), "checkpoint file missing"
    done1 = json.loads(ck_path.read_text(encoding="utf-8"))["done"]
    assert len(done1) == 1
    assert len(_top_level_rows(bk.name)) == 1

    # rerun without a limit completes the plan (checkpoint + anti-join skip
    # the blob already registered)
    second = register_backend_objects(bk, _load_cfg())
    assert second["status"] == "ok"
    assert second["planned"] == total - 1
    assert second["registered"] == total - 1
    assert second["failed"] == []
    assert len(_top_level_rows(bk.name)) == total
    done2 = json.loads(ck_path.read_text(encoding="utf-8"))["done"]
    assert len(done2) == total


# ---------------------------------------------------------------------------
# (e) unreferenced objects are ignored (plan is DB-driven)
# ---------------------------------------------------------------------------

def test_register_ignores_unreferenced_objects(env, backends, chunked_cfg):
    bk = backends[0]
    res = backup_bytes(env, "docs/known.bin", os.urandom(20_000), backends)
    assert res["status"] == "ok", res
    assert _top_level_rows(bk.name), "sanity: referenced blobs have rows"

    fake_tail = hashlib.sha256(b"stray-object").hexdigest()
    stray = _blob_object(bk, f"objects/{fake_tail[:2]}/{fake_tail[2:4]}/{fake_tail}")
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"stray-bytes")
    legacy = _blob_object(bk, f"{fake_tail}.zstd")
    legacy.write_bytes(b"legacy-stray")
    # even a DB-known but UNREFERENCED blob must never be planned
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, 11, 0)",
            (fake_tail,))
    commit()

    report = register_backend_objects(bk, _load_cfg())
    assert report["planned"] == 0
    assert report["registered"] == 0
    assert report["failed"] == []
    assert report["not_found"] == []
    rows = execute("SELECT COUNT(*) AS c FROM blob_storage WHERE blob_id = ?",
                   (fake_tail,)).fetchone()["c"]
    assert rows == 0


# ---------------------------------------------------------------------------
# (f) CLI: rescan NAME --register --json merges the report, exit 0
# ---------------------------------------------------------------------------

def test_cli_rescan_register_json_exit_0_and_fields(env, backends, chunked_cfg, runner):
    bk = backends[0]
    data = os.urandom(20_000)
    res = backup_bytes(env, "docs/clireg.bin", data, backends)
    assert res["status"] == "ok", res
    _delete_top_level_rows(bk.name)

    r = invoke(runner, env, "backend", "rescan", bk.name, "--register", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    reg = payload["register"]
    assert reg["status"] == "ok"
    assert reg["registered"] >= 1
    for key in ("backend", "planned", "failed", "not_found", "elapsed"):
        assert key in reg, f"register report missing {key}"
    # the walk section of the plain rescan is still present
    assert "extra_on_box" in payload
    assert _top_level_rows(bk.name), "expected recreated rows"

    # a follow-up dry-run has nothing left to do and stays exit 0
    r2 = invoke(runner, env, "backend", "rescan", bk.name, "--register", "--dry-run", "--json")
    assert r2.exit_code == 0, r2.output
    reg2 = json.loads(r2.stdout)["register"]
    assert reg2["dry_run"] is True
    assert reg2["planned"] == 0
    assert reg2["registered"] == 0


def test_cli_rescan_register_unknown_backend_exit_1(runner, env):
    r = invoke(runner, env, "backend", "rescan", "ghost_backend", "--register", "--json")
    assert r.exit_code == 1, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert "no backend named" in payload["error"]


def test_cli_rescan_without_flags_unchanged_exit_code(env, backends, runner):
    """Plain rescan keeps its legacy behavior: extra objects on box alone do
    NOT fail the walk-vs-DB diff (exit 0)."""
    bk = backends[0]
    res = backup_bytes(env, "docs/plain.bin", os.urandom(20_000), backends)
    assert res["status"] == "ok", res
    r = invoke(runner, env, "backend", "rescan", bk.name, "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert "register" not in payload
    assert payload["missing_on_box"] == []

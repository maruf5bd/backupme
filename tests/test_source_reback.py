"""Original-source re-back (source_reback): heal DB-loss residue (live files
whose chunks reference blobs with NO blob_storage rows anywhere) by re-backing
the original source file through the REAL pipeline, in place.

All scenarios run in the isolated `env` scratch (own config, DB, local
backend); blob_storage rows are wiped via SQL to simulate the DB-loss
residue. CliRunner invocations are pinned to the scratch config/DB like
test_cli_p2.py — the live config.yaml / backupme.db are never touched.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli
from tests.helpers import backup_bytes, restore_bytes


@pytest.fixture()
def runner():
    return CliRunner()


def invoke(runner, env, *args, **kw):
    return runner.invoke(
        cli,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"), *args],
        **kw,
    )


def parse_json(result):
    return json.loads(result.stdout)


def wipe_blob_storage():
    """Simulate DB-loss residue: no blob knows where anything is stored."""
    from backupme.core.database import execute, commit
    execute("DELETE FROM blob_storage")
    commit()


def blob_storage_count():
    from backupme.core.database import execute
    return execute("SELECT COUNT(*) FROM blob_storage").fetchone()[0]


def uncovered_chunks(file_id):
    from backupme.core.database import execute
    return execute(
        "SELECT COUNT(*) FROM file_chunks fc WHERE fc.file_id = ?"
        " AND NOT EXISTS (SELECT 1 FROM blob_storage bs WHERE bs.blob_id = fc.blob_id)",
        (file_id,),
    ).fetchone()[0]


def file_sha(file_id):
    from backupme.core.database import execute
    return execute("SELECT sha256 FROM files WHERE id = ?", (file_id,)).fetchone()[0]


def insert_fake_rowless(source_root, relative_path):
    """A DB-only candidate: files + blobs + file_chunks rows, no storage."""
    from backupme.core.database import execute, commit
    sha = hashlib.sha256(relative_path.encode()).hexdigest()
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, mtime,"
        " chunked, redundancy, version_group, version_seq)"
        " VALUES (?, ?, ?, 10, 0, 1, 1, ?, 1)",
        (source_root, relative_path, sha, sha[:16]),
    )
    file_id = execute("SELECT last_insert_rowid()").fetchone()[0]
    blob_id = f"dead/{sha[:8]}/{sha}"
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, 10, 1)", (blob_id,))
    execute(
        "INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size)"
        " VALUES (?, 0, ?, 0, 10)",
        (file_id, blob_id),
    )
    commit()
    return file_id


def seed_two(env, backends):
    d1 = b"alpha-payload-" + os.urandom(8)
    d2 = b"beta-payload-" + os.urandom(8)
    fid1 = backup_bytes(env, "docs/a.txt", d1, backends)["file_id"]
    fid2 = backup_bytes(env, "docs/b.txt", d2, backends)["file_id"]
    return fid1, d1, fid2, d2


# ---------------------------------------------------------------------------
# (a) find -> dry run (zero writes) -> apply heals in place
# ---------------------------------------------------------------------------

def test_find_dry_run_and_apply_heals_in_place(env, backends):
    from backupme.core.dedup import reconcile_refcounts
    from backupme.core.source_reback import find_rowless_live_files, reback_sources

    fid1, d1, fid2, d2 = seed_two(env, backends)
    shas_before = {fid1: file_sha(fid1), fid2: file_sha(fid2)}
    wipe_blob_storage()

    found = find_rowless_live_files()
    assert found["total"] == 2, found
    assert {s["file_id"] for s in found["sample"]} == {fid1, fid2}
    row1 = next(s for s in found["sample"] if s["file_id"] == fid1)
    assert row1["relative_path"] == "docs/a.txt"
    assert row1["sha256"] == shas_before[fid1]
    assert row1["size"] == len(d1)
    assert row1["chunk_count"] >= 1
    assert row1["virtual_root"] is False

    # Dry run: reports would_heal, ZERO uploads (no storage rows appear),
    # no checkpoint, no op_log entry.
    dry = reback_sources(dry_run=True)
    assert dry["status"] == "dry-run"
    assert dry["would_heal"] == 2
    assert dry["would_skip"] == 0
    assert dry["failures"] == []
    assert blob_storage_count() == 0
    assert not (env / "reback_checkpoint.json").exists()
    from backupme.core.database import execute
    assert execute(
        "SELECT COUNT(*) FROM op_log WHERE op_type = 'reback_sources'"
    ).fetchone()[0] == 0

    res = reback_sources(dry_run=False)
    assert res["status"] == "applied"
    assert res["healed"] == 2, res
    assert res["skipped"] == 0
    assert res["failures"] == []
    # The upload really happened: storage rows were (re)created — the donor
    # guard did NOT let the rowless file dedup onto itself.
    assert blob_storage_count() > 0

    for fid, data in ((fid1, d1), (fid2, d2)):
        assert uncovered_chunks(fid) == 0, f"file {fid} still rowless"
        assert file_sha(fid) == shas_before[fid], "sha256 must not change"
        assert restore_bytes(fid, env) == data, "restore must be byte-identical"

    # One committed op_log entry; refcounts fully consistent afterwards
    # (the run's own reconcile pass already fixed the recreated-member drift).
    op_rows = execute(
        "SELECT status FROM op_log WHERE op_type = 'reback_sources'"
    ).fetchall()
    assert op_rows and all(r["status"] == "committed" for r in op_rows)
    assert reconcile_refcounts() == 0

    ck = json.loads((env / "reback_checkpoint.json").read_text())
    assert set(ck["healed"]) == {str(fid1), str(fid2)}

    # Rerun on a healthy vault: healed files are no longer rowless, so there
    # is nothing left to process at all.
    rerun = reback_sources(dry_run=False)
    assert rerun["healed"] == 0
    assert rerun["candidates"] == 0

    # If the residue ever comes back (storage rows lost AGAIN), the healed
    # ids are checkpointed and skipped until the checkpoint is deleted.
    wipe_blob_storage()
    again = reback_sources(dry_run=False)
    assert again["healed"] == 0
    assert again["candidates"] == 2
    assert again["already_processed"] == 2
    assert blob_storage_count() == 0


def test_checkpoint_resume_with_limit(env, backends):
    from backupme.core.source_reback import reback_sources

    fid1, _, fid2, _ = seed_two(env, backends)
    wipe_blob_storage()

    first = reback_sources(dry_run=False, limit=1)
    assert first["healed"] == 1, first
    assert first["processed"] == 1

    # The remaining candidate is still rowless and gets healed; the first
    # file is no longer a candidate at all (it has storage now).
    second = reback_sources(dry_run=False, limit=1)
    assert second["healed"] == 1, second
    assert second["already_processed"] == 0

    third = reback_sources(dry_run=False)
    assert third["healed"] == 0
    assert third["candidates"] == 0
    assert uncovered_chunks(fid1) == 0
    assert uncovered_chunks(fid2) == 0


# ---------------------------------------------------------------------------
# (b) source gone -> SOURCE_GONE, the other file still heals
# ---------------------------------------------------------------------------

def test_source_gone_reports_and_other_still_heals(env, backends):
    from backupme.core.source_reback import reback_sources

    d1 = b"will-vanish-payload"
    fid1 = backup_bytes(env, "gone/a.txt", d1, backends)["file_id"]
    d2 = b"stays-here-payload"
    fid2 = backup_bytes(env, "kept/b.txt", d2, backends)["file_id"]
    (env / "gone" / "a.txt").unlink()
    wipe_blob_storage()

    res = reback_sources(dry_run=False)
    assert res["healed"] == 1, res
    assert res["skip_reasons"] == {"SOURCE_GONE": 1}
    assert res["failures"] == []

    assert uncovered_chunks(fid2) == 0
    assert restore_bytes(fid2, env) == d2
    assert uncovered_chunks(fid1) > 0, "SOURCE_GONE file must stay untouched"

    ck = json.loads((env / "reback_checkpoint.json").read_text())
    assert ck["skipped"][str(fid1)]["reason"] == "SOURCE_GONE"

    # Rerun: the SOURCE_GONE file is still rowless and stays checkpoint-skipped.
    rerun = reback_sources(dry_run=False)
    assert rerun["healed"] == 0
    assert rerun["already_processed"] == 1
    assert rerun["processed"] == 0


# ---------------------------------------------------------------------------
# (c) same size, different bytes -> SOURCE_CHANGED
# ---------------------------------------------------------------------------

def test_source_changed_same_size_skipped(env, backends):
    from backupme.core.source_reback import reback_sources

    d1 = b"original-bytes!!"
    fid1 = backup_bytes(env, "chg/a.txt", d1, backends)["file_id"]
    d2 = b"untouched-data!!"
    fid2 = backup_bytes(env, "ok/b.txt", d2, backends)["file_id"]
    (env / "chg" / "a.txt").write_bytes(b"REPLACED-CONTENT")  # same size, new bytes
    wipe_blob_storage()

    dry = reback_sources(dry_run=True)
    assert dry["would_heal"] == 1
    assert dry["would_skip"] == 1
    assert dry["skip_reasons"] == {"SOURCE_CHANGED": 1}

    res = reback_sources(dry_run=False)
    assert res["healed"] == 1
    assert res["skip_reasons"] == {"SOURCE_CHANGED": 1}
    assert uncovered_chunks(fid2) == 0
    assert restore_bytes(fid2, env) == d2
    assert uncovered_chunks(fid1) > 0, "changed source must not be re-backed"


def test_source_size_changed_skipped(env, backends):
    from backupme.core.source_reback import reback_sources

    fid1 = backup_bytes(env, "sz/a.txt", b"12345678", backends)["file_id"]
    fid2 = backup_bytes(env, "sz/b.txt", b"87654321", backends)["file_id"]
    (env / "sz" / "a.txt").write_bytes(b"1234567890")  # different size
    wipe_blob_storage()

    res = reback_sources(dry_run=False)
    assert res["healed"] == 1
    assert res["skip_reasons"] == {"SOURCE_SIZE_CHANGED": 1}
    assert uncovered_chunks(fid1) > 0


# ---------------------------------------------------------------------------
# (d) virtual roots ('archive/<fp>', 'noroot') -> VIRTUAL_ROOT
# ---------------------------------------------------------------------------

def test_virtual_root_candidates_skipped(env, backends):
    from backupme.core.source_reback import find_rowless_live_files, reback_sources

    fid1, _, fid2, _ = seed_two(env, backends)
    fake1 = insert_fake_rowless("archive/9f2cfingerprint", "vault/gone.bin")
    fake2 = insert_fake_rowless("noroot", "legacy/old.bin")
    wipe_blob_storage()

    found = find_rowless_live_files()
    assert found["total"] == 4, found
    by_id = {s["file_id"]: s for s in found["sample"]}
    assert by_id[fake1]["virtual_root"] is True
    assert by_id[fake2]["virtual_root"] is True
    assert by_id[fid1]["virtual_root"] is False

    res = reback_sources(dry_run=False)
    assert res["healed"] == 2, res
    assert res["skip_reasons"] == {"VIRTUAL_ROOT": 2}
    assert uncovered_chunks(fake1) > 0
    assert uncovered_chunks(fake2) > 0


# ---------------------------------------------------------------------------
# (e) CLI: dry-run default (REPORT ONLY), --apply exits 0
# ---------------------------------------------------------------------------

def test_cli_dry_run_and_apply(runner, env, backends):
    fid1, _, fid2, _ = seed_two(env, backends)
    wipe_blob_storage()

    r = invoke(runner, env, "db", "reback-sources", "--json")
    assert r.exit_code == 0, r.output
    data = parse_json(r)
    assert data["status"] == "dry-run"
    assert data["would_heal"] == 2
    assert data["candidates"] == 2

    rh = invoke(runner, env, "db", "reback-sources")
    assert rh.exit_code == 0, rh.output
    assert "REPORT ONLY" in rh.output
    assert "would heal: 2" in rh.output

    ra = invoke(runner, env, "db", "reback-sources", "--apply", "--json")
    assert ra.exit_code == 0, ra.output
    applied = parse_json(ra)
    assert applied["status"] == "applied"
    assert applied["healed"] == 2
    assert uncovered_chunks(fid1) == 0
    assert uncovered_chunks(fid2) == 0

    rah = invoke(runner, env, "db", "reback-sources", "--apply")
    assert rah.exit_code == 0, rah.output
    assert "APPLIED" in rah.output
    assert "already processed" in rah.output


# ---------------------------------------------------------------------------
# (f) --state-file fallback: source gone but a verified restored copy heals
# ---------------------------------------------------------------------------

def test_state_file_fallback_heals_when_source_gone(env, backends):
    from backupme.core.source_reback import reback_sources

    data = b"state-copy-payload-123"
    fid = backup_bytes(env, "lost/orig.bin", data, backends)["file_id"]
    sha = hashlib.sha256(data).hexdigest()
    copy_path = env.parent / "state_copy.bin"
    copy_path.write_bytes(data)
    (env / "lost" / "orig.bin").unlink()
    wipe_blob_storage()

    state_path = env.parent / "campaign_state.json"
    state_path.write_text(json.dumps(
        {"ok": {str(fid): {"path": str(copy_path), "sha256": sha,
                           "size": len(data)}}}))

    dry = reback_sources(dry_run=True, state_file=str(state_path))
    assert dry["would_heal"] == 1, dry
    assert dry["sample"][0]["source_kind"] == "state_file"

    res = reback_sources(dry_run=False, state_file=str(state_path))
    assert res["healed"] == 1, res
    assert res["failures"] == []

    from backupme.core.database import execute
    row = execute(
        "SELECT source_root, relative_path FROM files WHERE id = ?", (fid,)
    ).fetchone()
    # The heal is IN PLACE: the DB row keeps its original coordinates.
    assert row["source_root"] == str(env)
    assert row["relative_path"] == "lost/orig.bin"
    assert uncovered_chunks(fid) == 0
    assert restore_bytes(fid, env) == data


def test_state_file_mismatch_skipped(env, backends):
    from backupme.core.source_reback import reback_sources

    data = b"real-payload-000000"
    fid = backup_bytes(env, "gone2/orig.bin", data, backends)["file_id"]
    bad_copy = env.parent / "bad_copy.bin"
    bad_copy.write_bytes(b"fake-payload-000")  # same size, different bytes
    (env / "gone2" / "orig.bin").unlink()
    wipe_blob_storage()

    state_path = env.parent / "bad_state.json"
    state_path.write_text(json.dumps(
        {"ok": {str(fid): {"path": str(bad_copy),
                           "sha256": hashlib.sha256(data).hexdigest(),
                           "size": len(data)}}}))

    res = reback_sources(dry_run=False, state_file=str(state_path))
    assert res["healed"] == 0, res
    assert res["skip_reasons"] == {"STATE_MISMATCH": 1}
    assert uncovered_chunks(fid) > 0


# ---------------------------------------------------------------------------
# (g) healthy same-sha donor heals by mapping copy (uploader donor guard)
# ---------------------------------------------------------------------------

def test_healthy_donor_dedup_heal(env, backends):
    """A rowless file whose content still exists on a HEALTHY file must heal
    by copying the donor's chunk mapping — the donor guard must never pick
    the rowless file itself as its own donor.

    The victim's rowless state is simulated with a DB-only member blob id
    different from the donor's (same-content pipeline uploads dedup onto one
    shared member, which the recreate-same-id path already covers in the
    end-to-end test above).
    """
    from backupme.core.source_reback import reback_sources
    from backupme.core.database import execute, commit

    data = b"shared-content-donor"
    sha = hashlib.sha256(data).hexdigest()
    healthy = backup_bytes(env, "donor/first.txt", data, backends)["file_id"]

    # A real source file on disk, and a DB row for it whose chunk points at a
    # dead member blob (no blobs row even — as if the residue lost more).
    victim_src = env / "victim" / "second.txt"
    victim_src.parent.mkdir(parents=True, exist_ok=True)
    victim_src.write_bytes(data)
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, mtime,"
        " chunked, redundancy, version_group, version_seq)"
        " VALUES (?, 'victim/second.txt', ?, ?, 0, 1, 1, ?, 1)",
        (str(env), sha, len(data), sha[:16]),
    )
    broken = execute("SELECT last_insert_rowid()").fetchone()[0]
    dead_blob = f"dead/{sha[:8]}/{sha}"
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
            (dead_blob, len(data)))
    execute(
        "INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size)"
        " VALUES (?, 0, ?, 0, ?)",
        (broken, dead_blob, len(data)),
    )
    commit()

    assert uncovered_chunks(broken) > 0
    assert uncovered_chunks(healthy) == 0

    res = reback_sources(dry_run=False)
    assert res["healed"] == 1, res
    assert res["failures"] == []

    victim_blobs_after = [r["blob_id"] for r in execute(
        "SELECT DISTINCT blob_id FROM file_chunks WHERE file_id = ?", (broken,)
    ).fetchall()]
    donor_blobs = [r["blob_id"] for r in execute(
        "SELECT DISTINCT blob_id FROM file_chunks WHERE file_id = ?", (healthy,)
    ).fetchall()]
    assert set(victim_blobs_after) == set(donor_blobs), \
        "heal must copy the healthy donor's chunk mapping"
    assert uncovered_chunks(broken) == 0
    assert restore_bytes(broken, env) == data

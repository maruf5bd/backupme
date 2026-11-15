"""backend prune-phantom-rows: delete DB rows that claim objects which do
not physically exist — never files, never member rows, never last copies.

Every test runs against the isolated `env` fixture (scratch config + DB +
local backend) and backs files up through the REAL pipeline before seeding
bogus rows in SQL — mirroring the recovery scenario the command exists for
(an external DB rewrite leaving rows that point nowhere).
"""
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli
from backupme.core.database import commit, execute
from backupme.core.phantom_prune import apply_prune, find_phantom_rows
from tests.helpers import add_backend, backup_bytes


@pytest.fixture()
def runner():
    return CliRunner()


def invoke(runner, env, *args):
    """Invoke the CLI pinned to the isolated env's config and DB."""
    return runner.invoke(
        cli,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"), *args],
    )


def _load_cfg():
    from backupme.config import load_config
    return load_config()


def _insert_bogus_row(blob_id, backend_name, storage_path, stored_size=5):
    """Seed a blob + blob_storage row the way an external rewrite would have:
    the DB claims an object that was never uploaded."""
    execute(
        "INSERT OR IGNORE INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
        (blob_id, stored_size),
    )
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, "
        "stored_sha256, compression_algo, compression_level, encrypted) "
        "VALUES (?, ?, ?, ?, ?, 'zstd', 3, 0)",
        (blob_id, backend_name, storage_path, stored_size, blob_id.rsplit("/", 1)[-1]),
    )
    commit()


def _row_exists(backend_name, blob_id, storage_path) -> int:
    return execute(
        "SELECT COUNT(*) AS c FROM blob_storage "
        "WHERE backend_name = ? AND blob_id = ? AND storage_path = ?",
        (backend_name, blob_id, storage_path),
    ).fetchone()["c"]


def _member_row_count(backend_name) -> int:
    return execute(
        "SELECT COUNT(*) AS c FROM blob_storage "
        "WHERE backend_name = ? AND storage_path LIKE '%@%'",
        (backend_name,),
    ).fetchone()["c"]


def _oplog_prune_count() -> int:
    return execute(
        "SELECT COUNT(*) AS c FROM op_log WHERE op_type = 'prune_phantom_rows'"
    ).fetchone()["c"]


def _tree_snapshot(bk) -> dict:
    """Every physical file on the backend: rel path -> (size, sha256)."""
    root = Path(bk.base_path)
    snap = {}
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            data = p.read_bytes()
            snap[rel] = (len(data), hashlib.sha256(data).hexdigest())
    return snap


def _seed(env, backends) -> tuple:
    """Real backup + 3 bogus phantom rows on the scratch local backend:
    FA covered by a row on a second backend, FB covered by another (real)
    row on the same backend, FC the last (only) row for its blob."""
    res = backup_bytes(env, "docs/real.txt", b"phantom-prune-real-payload", backends)
    assert res["status"] == "ok", res
    add_backend(env, "second_local", env.parent / "storage_second")
    from backupme.backends import get_all_backends
    bk = next(b for b in get_all_backends(_load_cfg()) if b.name == "test_local")

    fa = hashlib.sha256(b"phantom-a").hexdigest()
    fb = hashlib.sha256(b"phantom-b").hexdigest()
    fc = hashlib.sha256(b"phantom-c").hexdigest()
    fa_path = f"objects/{fa[:2]}/{fa[2:4]}/{fa}.zstd"
    fb_path = f"objects/{fb[:2]}/{fb[2:4]}/{fb}.zstd"
    fc_path = f"objects/{fc[:2]}/{fc[2:4]}/{fc}.zstd"
    fb_real_path = f"objects/{fb[:2]}/{fb[2:4]}/{fb}"
    mem_id = f"bundle/{fc[:2]}/{fc[2:4]}/tar/mem"
    mem_path = f"bundle/{fc[:2]}/{fc[2:4]}/mem.tar@100:200"

    # FA: phantom on test_local, but the second backend has a row for it.
    _insert_bogus_row(fa, "test_local", fa_path)
    _insert_bogus_row(fa, "second_local", fa_path)
    # FB: one phantom row plus one REAL row (object exists) on the same
    # backend — deleting the phantom loses nothing.
    _insert_bogus_row(fb, "test_local", fb_path)
    _insert_bogus_row(fb, "test_local", fb_real_path)
    real_obj = Path(bk.base_path) / fb_real_path
    real_obj.parent.mkdir(parents=True, exist_ok=True)
    real_obj.write_bytes(b"fb-real-object")
    # FC: phantom and the ONLY row for the blob anywhere — data-loss signal.
    _insert_bogus_row(fc, "test_local", fc_path)
    # A bogus MEMBER row ('@' storage) must never even be a candidate.
    _insert_bogus_row(mem_id, "test_local", mem_path)

    return bk, {"fa": fa, "fb": fb, "fc": fc, "mem_id": mem_id,
                "fa_path": fa_path, "fb_path": fb_path, "fc_path": fc_path,
                "fb_real_path": fb_real_path, "mem_path": mem_path}


# ---------------------------------------------------------------------------
# (a) after a real backup the report finds zero phantoms
# ---------------------------------------------------------------------------

def test_report_finds_zero_phantom_after_real_backup(env, backends):
    bk = backends[0]
    for name in ("docs/one.txt", "docs/two.txt"):
        res = backup_bytes(env, name, b"phantom-scan-" + name.encode() + b"-payload",
                           backends)
        assert res["status"] == "ok", res
    report = find_phantom_rows(bk, _load_cfg())
    assert report["status"] == "ok", report
    assert report["backend"] == bk.name
    assert report["scanned"] >= 2, report
    assert report["phantom"] == 0
    assert report["safe_candidates"] == 0
    assert report["last_copy_missing"] == 0
    assert report["phantom_sample"] == []
    assert report["member_rows_skipped"] >= 2  # the bundled member rows
    assert report["errors"] == []
    assert _oplog_prune_count() == 0


# ---------------------------------------------------------------------------
# (b) 3 bogus rows: exactly 2 safe candidates + 1 last_copy_missing
# ---------------------------------------------------------------------------

def test_report_flags_safe_and_last_copy_bogus_rows(env, backends):
    bk, s = _seed(env, backends)
    report = find_phantom_rows(bk, _load_cfg())
    assert report["status"] == "ok", report
    assert report["phantom"] == 3
    assert report["safe_candidates"] == 2
    assert report["last_copy_missing"] == 1
    assert report["errors"] == []
    by_blob = {e["blob_id"]: e for e in report["phantom_sample"]}
    assert set(by_blob) == {s["fa"], s["fb"], s["fc"]}
    assert by_blob[s["fa"]]["covered_elsewhere"] is True
    assert by_blob[s["fa"]]["last_copy_missing"] is False
    assert by_blob[s["fa"]]["path"] == s["fa_path"]
    assert by_blob[s["fb"]]["covered_elsewhere"] is True
    assert by_blob[s["fb"]]["last_copy_missing"] is False
    # FC is the only row for its blob anywhere -> data-loss signal, kept
    assert by_blob[s["fc"]]["covered_elsewhere"] is False
    assert by_blob[s["fc"]]["last_copy_missing"] is True
    # the member row is reported as skipped, never a candidate
    assert report["member_rows_skipped"] >= 2  # real members + bogus member
    assert s["mem_id"] not in by_blob


# ---------------------------------------------------------------------------
# (c) report-only (no --apply) changes nothing
# ---------------------------------------------------------------------------

def test_report_only_changes_nothing(env, backends):
    bk, s = _seed(env, backends)
    before_rows = execute(
        "SELECT COUNT(*) AS c FROM blob_storage WHERE backend_name = ?",
        (bk.name,),
    ).fetchone()["c"]
    before_tree = _tree_snapshot(bk)
    report = find_phantom_rows(bk, _load_cfg())
    assert report["phantom"] == 3
    after_rows = execute(
        "SELECT COUNT(*) AS c FROM blob_storage WHERE backend_name = ?",
        (bk.name,),
    ).fetchone()["c"]
    assert after_rows == before_rows
    assert _tree_snapshot(bk) == before_tree
    assert _oplog_prune_count() == 0


# ---------------------------------------------------------------------------
# (d) --apply deletes exactly the 2 safe rows; last copy + files survive
# ---------------------------------------------------------------------------

def test_apply_deletes_only_safe_rows(env, backends):
    bk, s = _seed(env, backends)
    before_tree = _tree_snapshot(bk)
    members_before = _member_row_count(bk.name)

    out = apply_prune(bk, _load_cfg())
    assert out["status"] == "ok", out
    assert out["planned"] == 2
    assert out["deleted"] == 2
    assert out["batches"] == 1
    assert out["kept_last_copy"] == 1
    assert out["reappeared"] == 0
    assert out["uncovered_now"] == 0
    assert out["op_log_id"] is not None

    # exactly the safe rows are gone from the DB
    assert _row_exists(bk.name, s["fa"], s["fa_path"]) == 0
    assert _row_exists(bk.name, s["fb"], s["fb_path"]) == 0
    # the blob's other rows survive untouched
    assert _row_exists("second_local", s["fa"], s["fa_path"]) == 1
    assert _row_exists(bk.name, s["fb"], s["fb_real_path"]) == 1
    # the last-copy row survives
    assert _row_exists(bk.name, s["fc"], s["fc_path"]) == 1
    # the bogus member row and all real member rows survive
    assert _row_exists(bk.name, s["mem_id"], s["mem_path"]) == 1
    assert _member_row_count(bk.name) == members_before

    # no file on the backend was touched
    assert _tree_snapshot(bk) == before_tree

    # one committed op_log entry for the prune
    ops = execute(
        "SELECT * FROM op_log WHERE op_type = 'prune_phantom_rows'"
    ).fetchall()
    assert len(ops) == 1
    assert ops[0]["status"] == "committed"
    assert ops[0]["id"] == out["op_log_id"]
    assert bk.name in ops[0]["target_json"]

    # a second apply re-scans, finds only the last-copy row, deletes nothing
    # and appends NO new op_log entry (idempotency key + no-op short circuit)
    out2 = apply_prune(bk, _load_cfg())
    assert out2["deleted"] == 0
    assert out2["planned"] == 0
    assert _oplog_prune_count() == 1
    assert _tree_snapshot(bk) == before_tree


def test_apply_op_log_key_format_and_failed_transition(env, backends, monkeypatch):
    """P1-4 parity: the idempotency key encodes backend + row-set digest, and
    a batch loop that fails unexpectedly must move the op_log row to 'failed'
    (never leave a dangling 'pending')."""
    import backupme.core.phantom_prune as pp

    bk, _s = _seed(env, backends)
    out = apply_prune(bk, _load_cfg())
    assert out["deleted"] == 2
    ops = execute(
        "SELECT * FROM op_log WHERE op_type = 'prune_phantom_rows' ORDER BY id"
    ).fetchall()
    assert len(ops) == 1
    assert ops[0]["idempotency_key"].startswith(f"prune_phantom_rows:{bk.name}:")

    # Seed two fresh coverable phantoms (sibling rows on second_local), then
    # make the batch commit explode: the error must propagate AND the op_log
    # row must end 'failed' with the error recorded.
    fx = hashlib.sha256(b"phantom-fail-x").hexdigest()
    fy = hashlib.sha256(b"phantom-fail-y").hexdigest()
    for tail in (fx, fy):
        _insert_bogus_row(tail, "test_local",
                          f"objects/{tail[:2]}/{tail[2:4]}/{tail}.zstd")
        _insert_bogus_row(tail, "second_local",
                          f"objects/{tail[:2]}/{tail[2:4]}/{tail}.zstd")

    def exploding_commit():
        raise RuntimeError("commit exploded")

    monkeypatch.setattr(pp, "commit", exploding_commit)
    with pytest.raises(RuntimeError, match="commit exploded"):
        apply_prune(bk, _load_cfg())

    rows = execute(
        "SELECT status, last_error FROM op_log WHERE op_type = 'prune_phantom_rows' "
        "ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[1]["status"] == "failed"
    assert "commit exploded" in rows[1]["last_error"]


# ---------------------------------------------------------------------------
# (e) '@' member rows are never candidates and never pruned
# ---------------------------------------------------------------------------

def test_member_rows_never_candidates_and_survive_apply(env, backends):
    bk, s = _seed(env, backends)
    report = find_phantom_rows(bk, _load_cfg())
    assert all("@" not in e["path"] for e in report["phantom_sample"])
    out = apply_prune(bk, _load_cfg())
    assert out["deleted"] == 2
    # the bogus member row (whose parent object does not exist either) and
    # every real member row are all still there
    assert _row_exists(bk.name, s["mem_id"], s["mem_path"]) == 1
    members = execute(
        "SELECT storage_path FROM blob_storage "
        "WHERE backend_name = ? AND storage_path LIKE '%@%'",
        (bk.name,),
    ).fetchall()
    assert members, "expected member rows to survive"
    # an explicitly supplied member-row candidate is refused by the guard
    mem_cand = {"backend": bk.name, "path": s["mem_path"], "blob_id": s["mem_id"],
                "stored_size": 5, "covered_elsewhere": True,
                "last_copy_missing": False}
    out2 = apply_prune(bk, _load_cfg(), candidates=[mem_cand])
    assert out2["skipped_member"] == 1
    assert out2["deleted"] == 0
    assert _row_exists(bk.name, s["mem_id"], s["mem_path"]) == 1


# ---------------------------------------------------------------------------
# gating extras: reappeared objects, rows already gone, stale coverage
# ---------------------------------------------------------------------------

def test_apply_keeps_row_when_object_reappeared(env, backends):
    bk = backends[0]
    tail = hashlib.sha256(b"reappeared-blob").hexdigest()
    path = f"objects/{tail[:2]}/{tail[2:4]}/{tail}.zstd"
    _insert_bogus_row(tail, "test_local", path)
    (Path(bk.base_path) / path).parent.mkdir(parents=True, exist_ok=True)
    (Path(bk.base_path) / path).write_bytes(b"the object came back")
    cand = {"backend": bk.name, "path": path, "blob_id": tail,
            "stored_size": 5, "covered_elsewhere": True,
            "last_copy_missing": False}
    out = apply_prune(bk, _load_cfg(), candidates=[cand])
    assert out["reappeared"] == 1
    assert out["deleted"] == 0
    assert out["status"] == "ok"
    assert _row_exists(bk.name, tail, path) == 1


def test_apply_counts_row_already_gone_from_db(env, backends):
    bk = backends[0]
    tail = hashlib.sha256(b"already-gone").hexdigest()
    path = f"objects/{tail[:2]}/{tail[2:4]}/{tail}.zstd"
    _insert_bogus_row(tail, "test_local", path)
    execute("DELETE FROM blob_storage WHERE blob_id = ?", (tail,))
    commit()
    cand = {"backend": bk.name, "path": path, "blob_id": tail,
            "stored_size": 5, "covered_elsewhere": True,
            "last_copy_missing": False}
    out = apply_prune(bk, _load_cfg(), candidates=[cand])
    assert out["gone_from_db"] == 1
    assert out["deleted"] == 0


def test_apply_keeps_row_when_coverage_vanished(env, backends):
    bk, s = _seed(env, backends)
    # hand apply a STALE candidate list, then delete the second backend's FA
    # coverage row before the apply runs: the per-row re-check must notice
    # the vanished coverage and keep the test_local row too
    report = find_phantom_rows(bk, _load_cfg())
    assert report["safe_candidates"] == 2
    stale = list(report["phantom_sample"])  # <= 20 phantoms: full list here
    execute(
        "DELETE FROM blob_storage WHERE backend_name = 'second_local' AND blob_id = ?",
        (s["fa"],),
    )
    commit()
    out = apply_prune(bk, _load_cfg(), candidates=stale)
    assert out["uncovered_now"] == 1
    assert out["kept_last_copy"] == 1  # FC (last copy) skipped from the list
    assert out["deleted"] == 1  # only FB's phantom row is still safe
    assert _row_exists(bk.name, s["fa"], s["fa_path"]) == 1
    assert _row_exists(bk.name, s["fb"], s["fb_path"]) == 0


# ---------------------------------------------------------------------------
# stale-root guard: 100% phantom on a sizable backend refuses the apply
# ---------------------------------------------------------------------------

def test_all_phantom_refusal(env, backends, runner):
    bk = backends[0]
    for i in range(150):
        tail = hashlib.sha256(f"stale-root-{i}".encode()).hexdigest()
        _insert_bogus_row(tail, "test_local",
                          f"objects/{tail[:2]}/{tail[2:4]}/{tail}.zstd")
    report = find_phantom_rows(bk, _load_cfg())
    assert report["scanned"] == 150
    assert report["phantom"] == 150
    assert report["all_phantom_suspicious"] is True

    out = apply_prune(bk, _load_cfg())
    assert out["status"] == "refused_all_phantom"
    assert out["deleted"] == 0
    assert _oplog_prune_count() == 0
    assert _row_exists(bk.name, report["phantom_sample"][0]["blob_id"],
                       report["phantom_sample"][0]["path"]) == 1

    r = invoke(runner, env, "backend", "prune-phantom-rows",
               bk.name, "--apply", "--json")
    assert r.exit_code == 1, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "refused_all_phantom"
    assert _oplog_prune_count() == 0


# ---------------------------------------------------------------------------
# (f) CLI: report-only exit 0 with report fields; --apply exit 0 + count
# ---------------------------------------------------------------------------

def test_cli_report_only_json_exit_0(env, backends, runner):
    bk, s = _seed(env, backends)
    r = invoke(runner, env, "backend", "prune-phantom-rows", bk.name, "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"
    assert payload["backend"] == bk.name
    for key in ("scanned", "phantom", "safe_candidates", "last_copy_missing",
                "member_rows_skipped", "phantom_sample", "errors"):
        assert key in payload, f"report missing {key}"
    assert payload["phantom"] == 3
    assert payload["safe_candidates"] == 2
    assert payload["last_copy_missing"] == 1
    assert payload.get("applied") is None
    assert _oplog_prune_count() == 0
    assert _row_exists(bk.name, s["fa"], s["fa_path"]) == 1

    # human mode states clearly that nothing was deleted
    rh = invoke(runner, env, "backend", "prune-phantom-rows", bk.name)
    assert rh.exit_code == 0, rh.output
    assert "REPORT ONLY" in rh.output
    assert "nothing was deleted" in rh.output


def test_cli_apply_json_exit_0_with_deleted_count(env, backends, runner):
    bk, s = _seed(env, backends)
    before_tree = _tree_snapshot(bk)
    r = invoke(runner, env, "backend", "prune-phantom-rows",
               bk.name, "--apply", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["applied"] is True
    assert payload["planned"] == 2
    assert payload["deleted"] == 2
    assert payload["kept_last_copy"] == 1
    assert isinstance(payload["op_log_id"], int)
    # rows actually gone, last copy + member rows + files intact
    assert _row_exists(bk.name, s["fa"], s["fa_path"]) == 0
    assert _row_exists(bk.name, s["fb"], s["fb_path"]) == 0
    assert _row_exists(bk.name, s["fc"], s["fc_path"]) == 1
    assert _row_exists(bk.name, s["mem_id"], s["mem_path"]) == 1
    assert _tree_snapshot(bk) == before_tree
    ops = execute(
        "SELECT * FROM op_log WHERE op_type = 'prune_phantom_rows'"
    ).fetchall()
    assert len(ops) == 1 and ops[0]["status"] == "committed"

    # human apply output names the deletion count
    rh = invoke(runner, env, "backend", "prune-phantom-rows", bk.name, "--apply")
    assert rh.exit_code == 0, rh.output
    assert "APPLIED" in rh.output
    assert "deleted 0 row(s)" in rh.output


def test_cli_unknown_backend_exit_1(env, runner):
    r = invoke(runner, env, "backend", "prune-phantom-rows", "ghost_box", "--json")
    assert r.exit_code == 1, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert "no backend named" in payload["error"]


def test_cli_unconnectable_backend_exit_1(env, backends, runner):
    """A configured backend whose path is an existing FILE can never connect;
    the command must fail closed (exit 1), never prune."""
    blocker = env / "not_a_dir"
    blocker.write_text("this is a file, not a directory\n")
    cfg_path = env / "config.yaml"
    lines = cfg_path.read_text().splitlines(keepends=True)
    idx = next(i for i, l in enumerate(lines) if l.startswith("encryption:"))
    entry = ["  - name: broken_local\n",
             f"    path: {blocker.as_posix()}\n",
             "    priority: 10\n"]
    lines[idx:idx] = entry
    cfg_path.write_text("".join(lines))
    from backupme.backends import invalidate_cache
    invalidate_cache()
    r = invoke(runner, env, "backend", "prune-phantom-rows", "broken_local", "--json")
    assert r.exit_code == 1, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert "broken_local" in payload["error"]

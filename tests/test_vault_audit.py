"""Tests for the vault-truth commands: `db audit` (vault_audit.audit_vault)
and `db reconcile-refs` (db_reconcile.reconcile_refs).

Built after an external process rewrote the DB and left phantom refs, missing
storage paths, and rowless chunks behind. All invocations run against the
isolated `env` fixture (scratch config + scratch DB + scratch local backend)
through the real backup pipeline — never the live config.yaml / backupme.db.
"""
import hashlib
import json
import os
import uuid

import pytest
from click.testing import CliRunner

from backupme.cli import cli
from tests.helpers import backup_bytes, add_backend


@pytest.fixture()
def runner():
    return CliRunner()


def invoke(runner, env, *args, **kw):
    """Invoke the CLI pinned to the isolated env's config and DB."""
    return runner.invoke(
        cli,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"), *args],
        **kw,
    )


def parse_json(result):
    """Strict: stdout must be parseable JSON on its own."""
    return json.loads(result.stdout)


def seed_two(env):
    """Back up two different-content files through the real pipeline."""
    r1 = backup_bytes(env, "docs/one.txt", b"vault-audit-payload-one-" + os.urandom(8),
                      _backends(env))
    r2 = backup_bytes(env, "docs/two.txt", b"vault-audit-payload-two-" + os.urandom(8),
                      _backends(env))
    assert r1["status"] == "ok" and r2["status"] == "ok", (r1, r2)
    return r1, r2


def _backends(env):
    from backupme.config import load_config
    from backupme.backends import get_all_backends
    return get_all_backends(load_config())


def ref_counts():
    from backupme.core.database import execute
    return {r[0]: r[1] for r in execute("SELECT blob_id, ref_count FROM blobs").fetchall()}


# ---------------------------------------------------------------------------
# (a) clean vault: real pipeline backup -> audit shows zero everywhere
# ---------------------------------------------------------------------------

def test_audit_clean_after_real_backup(runner, env):
    seed_two(env)
    r = invoke(runner, env, "db", "audit", "--json")
    assert r.exit_code == 0, r.output
    d = parse_json(r)
    assert d["status"] == "ok"
    assert d["refcount_drift"] == 0
    assert d["phantom_refs"] == 0
    assert d["rowless_chunks_live_files"] == 0
    assert d["fully_rowless"] == 0
    assert d["archive_broken"] == 0
    assert d["members_rowless_parents"] == 0
    entry = d["rows_missing_paths"].get("test_local")
    assert entry is not None and entry["checked"] >= 1, d["rows_missing_paths"]
    assert entry["missing"] == 0
    assert d["elapsed"] >= 0


def test_audit_human_clean(runner, env):
    seed_two(env)
    r = invoke(runner, env, "db", "audit")
    assert r.exit_code == 0, r.output
    assert "vault audit: ok" in r.output
    assert "refcount drift: 0" in r.output
    assert "missing paths on test_local: 0" in r.output


# ---------------------------------------------------------------------------
# (b) refcount corruption -> audit drift, dry-run no-write, apply fixes
# ---------------------------------------------------------------------------

def test_reconcile_dry_run_then_apply_fixes_refcounts(runner, env):
    seed_two(env)
    from backupme.core.database import execute, commit
    execute("UPDATE blobs SET ref_count = 7")
    commit()
    truth = {
        r[0]: r[1]
        for r in execute(
            "SELECT blob_id, COUNT(*) FROM file_chunks GROUP BY blob_id"
        ).fetchall()
    }
    # member rows contribute one parent reference each
    for (bid,) in execute(
        "SELECT blob_id FROM blobs WHERE blob_id LIKE 'bundle/%/%/%/%'"
    ).fetchall():
        parent = bid.rsplit("/", 1)[0]
        truth[parent] = truth.get(parent, 0) + 1

    r = invoke(runner, env, "db", "audit", "--json")
    d = parse_json(r)
    assert d["refcount_drift"] > 0
    assert d["phantom_refs"] == 0  # expected > 0 for every real blob: not phantom
    assert d["status"] == "issues"

    # dry-run: reports would_fix, writes NOTHING
    r = invoke(runner, env, "db", "reconcile-refs", "--json")
    assert r.exit_code == 0, r.output
    d = parse_json(r)
    assert d["apply"] is False
    assert d["drift_total"] > 0
    assert d["would_fix"] == d["drift_total"]
    assert d["fixed"] == 0
    after_dry = ref_counts()
    assert all(v == 7 for v in after_dry.values()), after_dry

    # apply: ref_counts land exactly on the truth formula
    r = invoke(runner, env, "db", "reconcile-refs", "--apply", "--json")
    assert r.exit_code == 0, r.output
    d = parse_json(r)
    assert d["apply"] is True
    assert d["fixed"] == d["drift_total"]
    assert d["op_log_id"] > 0
    after_apply = ref_counts()
    for bid, rc in after_apply.items():
        assert rc == truth[bid], (bid, rc, truth[bid])


def test_reconcile_limit_caps_apply(runner, env):
    seed_two(env)
    from backupme.core.database import execute, commit
    execute("UPDATE blobs SET ref_count = 7")
    commit()
    r = invoke(runner, env, "db", "reconcile-refs", "--apply", "--limit", "1", "--json")
    d = parse_json(r)
    assert d["drift_total"] > 1
    assert d["fixed"] == 1
    values = set(ref_counts().values())
    assert 0 in values or 7 in values  # exactly one blob was corrected


# ---------------------------------------------------------------------------
# (c) phantom ref: blob row with refs but zero chunks
# ---------------------------------------------------------------------------

def test_phantom_ref_detected_and_zeroed_on_apply(runner, env):
    seed_two(env)
    from backupme.core.database import execute, commit
    sha = hashlib.sha256(b"phantom-content").hexdigest()
    blob_id = f"objects/{sha[:2]}/{sha[2:4]}/{sha}"
    execute(
        "INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 3)",
        (blob_id, 123),
    )
    commit()

    r = invoke(runner, env, "db", "audit", "--json")
    d = parse_json(r)
    assert d["phantom_refs"] == 1, d["phantom_refs_sample"]
    assert blob_id in d["phantom_refs_sample"]
    assert d["refcount_drift"] >= 1

    r = invoke(runner, env, "db", "reconcile-refs", "--apply", "--json")
    d = parse_json(r)
    assert d["fixed"] >= 1
    row = execute("SELECT ref_count FROM blobs WHERE blob_id = ?", (blob_id,)).fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# (d) rows_missing_paths: blob_storage row pointing at a nonexistent file
# ---------------------------------------------------------------------------

def test_rows_missing_paths_bogus_row(runner, env):
    from backupme.core.database import execute, commit
    sha = hashlib.sha256(b"bogus-path-content").hexdigest()
    blob_id = f"objects/{sha[:2]}/{sha[2:4]}/{sha}"
    execute(
        "INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)",
        (blob_id, 10),
    )
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256)"
        " VALUES (?, 'test_local', ?, 10, ?)",
        (blob_id, f"{blob_id}.zstd", sha),
    )
    commit()

    r = invoke(runner, env, "db", "audit", "--json")
    assert r.exit_code == 0, r.output
    d = parse_json(r)
    entry = d["rows_missing_paths"]["test_local"]
    assert entry["missing"] == 1, entry
    assert any(
        s["backend"] == "test_local" and s["path"] == f"{blob_id}.zstd"
        for s in d["rows_missing_paths_sample"]
    )
    assert d["status"] == "issues"


def test_rows_missing_paths_skips_range_rows_and_other_backends(runner, env):
    """Member rows (path@offset:len) are byte ranges, not files — and rows on
    non-local backends are skipped, not falsely reported missing."""
    from backupme.core.database import execute, commit
    sha = hashlib.sha256(b"range-row-content").hexdigest()
    blob_id = f"bundle/{sha[:2]}/{sha[2:4]}/{sha}"
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)", (blob_id, 10))
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256)"
        " VALUES (?, 'test_local', ?, 10, ?)",
        (blob_id, f"{blob_id}.zstd@128:10", sha),
    )
    execute(
        "INSERT INTO blob_storage (blob_id, backend_name, storage_path, stored_size, stored_sha256)"
        " VALUES (?, 'some_sftp', ?, 10, ?)",
        (blob_id, f"{blob_id}.zstd", sha),
    )
    commit()

    r = invoke(runner, env, "db", "audit", "--json")
    d = parse_json(r)
    assert d["rows_missing_paths"] == {}, d["rows_missing_paths"]
    assert "some_sftp" in d["rows_missing_paths_skipped_backends"]
    # the @-range row is excluded by the query itself; only the non-local
    # row reaches the skip counter
    assert d["rows_missing_paths_skipped_rows"] == 1


# ---------------------------------------------------------------------------
# member -> parent coverage
# ---------------------------------------------------------------------------

def test_members_rowless_parents(runner, env):
    seed_two(env)  # real bundle exists on disk; now add a member whose parent does not
    from backupme.core.database import execute, commit
    orphan_tar = uuid.uuid4().hex
    member_id = f"bundle/{orphan_tar[:2]}/{orphan_tar[2:4]}/{orphan_tar}/{uuid.uuid4().hex}"
    execute("INSERT INTO blobs (blob_id, pt_size, ref_count) VALUES (?, ?, 1)", (member_id, 10))
    cur = execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, mtime, chunked,"
        " version_group, version_seq) VALUES ('t', 'orphan.txt', ?, 10, 0, 0, 'orph', 1)",
        (uuid.uuid4().hex,),
    )
    file_id = cur.lastrowid
    execute(
        "INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size)"
        " VALUES (?, 0, ?, 0, 10)",
        (file_id, member_id),
    )
    commit()

    r = invoke(runner, env, "db", "audit", "--json")
    d = parse_json(r)
    assert d["members_rowless_parents"] == 1, d["members_rowless_parents_sample"]
    sample = d["members_rowless_parents_sample"][0]
    assert sample["member"] == member_id
    assert sample["parent"] == member_id.rsplit("/", 1)[0]
    # the synthetic file is also live-with-rowless-chunks
    assert d["rowless_chunks_live_files"] >= 1


# ---------------------------------------------------------------------------
# box coverage
# ---------------------------------------------------------------------------

def test_box_coverage_counts_missing_on_storagebox(runner, env):
    add_backend(env, "storagebox", env.parent / "storagebox_storage")
    seed_two(env)  # uploads go to test_local only (redundancy 1)

    r = invoke(runner, env, "db", "audit", "--json")
    d = parse_json(r)
    assert d["box_coverage_status"] == "ok"
    # top-level blobs (parent bundle + objects) all lack a storagebox row;
    # bundle members are excluded from the check
    assert d["box_coverage"] >= 1
    assert all(not s.startswith("bundle/") or s.count("/") < 4
               for s in d["box_coverage_sample"])


def test_box_coverage_skipped_without_backend(runner, env):
    seed_two(env)
    r = invoke(runner, env, "db", "audit", "--json")
    d = parse_json(r)
    assert d["box_coverage_status"] == "skipped"
    assert d["box_coverage"] == 0


# ---------------------------------------------------------------------------
# (e) CLI surface
# ---------------------------------------------------------------------------

def test_cli_audit_json_fields(runner, env):
    seed_two(env)
    r = invoke(runner, env, "db", "audit", "--json")
    assert r.exit_code == 0, r.output
    d = parse_json(r)
    for key in ("status", "refcount_drift", "phantom_refs",
                "rowless_chunks_live_files", "fully_rowless", "archive_broken",
                "rows_missing_paths", "members_rowless_parents", "box_coverage",
                "elapsed"):
        assert key in d, key


def test_cli_reconcile_refs_dry_run_leaves_refcounts(runner, env):
    seed_two(env)
    from backupme.core.database import execute, commit
    execute("UPDATE blobs SET ref_count = 42")
    commit()
    before = ref_counts()
    r = invoke(runner, env, "db", "reconcile-refs", "--json")
    assert r.exit_code == 0, r.output
    d = parse_json(r)
    assert d["apply"] is False
    assert d["drift_total"] >= 1
    assert d["would_fix"] >= 1
    assert d["fixed"] == 0
    assert ref_counts() == before


def test_cli_reconcile_refs_human_report_only(runner, env):
    seed_two(env)
    from backupme.core.database import execute, commit
    execute("UPDATE blobs SET ref_count = 42")
    commit()
    r = invoke(runner, env, "db", "reconcile-refs")
    assert r.exit_code == 0, r.output
    assert "REPORT ONLY" in r.output
    assert "would fix" in r.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(r.stdout)
    # nothing written
    assert all(v == 42 for v in ref_counts().values())


def test_cli_reconcile_refs_apply_human(runner, env):
    seed_two(env)
    from backupme.core.database import execute, commit
    execute("UPDATE blobs SET ref_count = 42")
    commit()
    r = invoke(runner, env, "db", "reconcile-refs", "--apply")
    assert r.exit_code == 0, r.output
    assert "APPLIED" in r.output
    truth_after = ref_counts()
    assert any(v != 42 for v in truth_after.values())


def test_cli_help_documents_flags(runner, env):
    r1 = invoke(runner, env, "db", "audit", "--help")
    assert r1.exit_code == 0, r1.output
    assert "--json" in r1.output
    r2 = invoke(runner, env, "db", "reconcile-refs", "--help")
    assert r2.exit_code == 0, r2.output
    assert "--apply" in r2.output
    assert "--limit" in r2.output
    assert "--json" in r2.output

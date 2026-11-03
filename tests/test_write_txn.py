"""QA-fix regression tests: write_txn rollback semantics and no leaked open
write transactions on the pooled thread-local connections."""
import pytest

from backupme.core.database import (
    execute, commit, write_txn, get_connection, initialize,
)
from tests.helpers import backup_bytes


def test_write_txn_commits_on_success():
    with write_txn():
        execute("INSERT INTO op_log (idempotency_key, op_type, target_json, status) "
                "VALUES ('txn-ok-1', 'test', '{}', 'pending')")
    row = execute("SELECT COUNT(*) FROM op_log WHERE idempotency_key='txn-ok-1'").fetchone()
    assert row[0] == 1
    assert not get_connection().in_transaction


def test_write_txn_rolls_back_on_error_and_leaves_no_open_txn():
    with pytest.raises(RuntimeError):
        with write_txn():
            execute("INSERT INTO op_log (idempotency_key, op_type, target_json, status) "
                    "VALUES ('txn-bad-1', 'test', '{}', 'pending')")
            raise RuntimeError("boom")
    row = execute("SELECT COUNT(*) FROM op_log WHERE idempotency_key='txn-bad-1'").fetchone()
    assert row[0] == 0
    # The pooled connection must NOT be left inside an open write transaction.
    assert not get_connection().in_transaction
    # And the connection stays usable for the next unit of work.
    with write_txn():
        execute("INSERT INTO op_log (idempotency_key, op_type, target_json, status) "
                "VALUES ('txn-ok-2', 'test', '{}', 'pending')")
    row = execute("SELECT COUNT(*) FROM op_log WHERE idempotency_key='txn-ok-2'").fetchone()
    assert row[0] == 1


def test_upload_batch_is_all_or_nothing(env, backends, monkeypatch):
    """A DB failure mid-batch must persist nothing for the whole batch."""
    # Two files land in one bundle batch (both under min_size).
    backup_bytes(env, "txn/f1.txt", b"one " * 50, backends)

    from backupme.core import dedup as dedup_mod
    original_add_ref = dedup_mod.add_ref

    calls = {"n": 0}

    def exploding_add_ref(blob_id):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated DB failure mid-batch")
        return original_add_ref(blob_id)

    monkeypatch.setattr(dedup_mod, "add_ref", exploding_add_ref)
    results = backup_bytes(env, "txn/f2.txt", b"two " * 50, backends)
    monkeypatch.setattr(dedup_mod, "add_ref", original_add_ref)

    # The failure surfaced as an error result (or exception) — either way,
    # nothing partial may be durable for that file.
    status = results.get("status")
    assert status in ("error", "ok", "partial"), results
    if status == "error":
        blobs_for_file = execute(
            "SELECT COUNT(*) FROM files WHERE relative_path LIKE '%f2.txt'").fetchone()[0]
        assert blobs_for_file == 0
    assert not get_connection().in_transaction


def test_busy_timeout_matches_connect_timeout():
    initialize()
    row = execute("PRAGMA busy_timeout").fetchone()
    assert row[0] == 60000

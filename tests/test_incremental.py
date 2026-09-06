import os
import time

import pytest

from backupme.scheduler import collect_changes
from backupme.core.database import execute, commit
from tests.helpers import backup_bytes


def test_second_run_skips_unchanged(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    candidates, skipped = collect_changes(str(env), [])
    names = {c.name for c in candidates}
    assert "a.txt" not in names and skipped == 1


def test_touched_mtime_reuploads(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    p = env / "a.txt"
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    candidates, skipped = collect_changes(str(env), [])
    assert "a.txt" in {c.name for c in candidates} and skipped == 0


def test_size_change_reuploads(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    (env / "a.txt").write_bytes(b"changed!")
    candidates, _ = collect_changes(str(env), [])
    assert "a.txt" in {c.name for c in candidates}


def test_new_file_is_candidate(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    (env / "new.txt").write_bytes(b"fresh")
    candidates, _ = collect_changes(str(env), [])
    assert "new.txt" in {c.name for c in candidates}


def test_legacy_row_without_chunks_not_skipped(env, backends):
    # Indexed row matching size/mtime but with no chunk rows (legacy phantom) — never skip.
    p = env / "phantom.txt"
    data = b"phantom " * 50
    p.write_bytes(data)
    st = p.stat()
    execute(
        """INSERT INTO files (source_root, relative_path, sha256, size, mtime, version_group)
           VALUES (?, 'phantom.txt', ?, ?, ?, 'g')""",
        (str(env), "a" * 64, st.st_size, st.st_mtime),
    )
    commit()
    candidates, skipped = collect_changes(str(env), [])
    assert "phantom.txt" in {c.name for c in candidates}


def test_force_returns_everything(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    candidates, skipped = collect_changes(str(env), [], force=True)
    assert "a.txt" in {c.name for c in candidates} and skipped == 0


def test_skip_rules_still_apply(env):
    (env / "junk.log").write_bytes(b"x" * 10)
    candidates, _ = collect_changes(str(env), [])
    assert all(c.name != "junk.log" for c in candidates)

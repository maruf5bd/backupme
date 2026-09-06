import pytest

from backupme.core.database import execute, commit
from backupme.core import migration
from tests.helpers import backup_bytes, restore_bytes


def _seed_legacy_inline(env, rel: str, content_text: str, sha256: str, size: int) -> int:
    execute(
        """INSERT INTO files (source_root, relative_path, sha256, size, version_group, content_text)
           VALUES ('r', ?, ?, ?, 'g', ?)""",
        (rel, sha256, size, content_text),
    )
    commit()
    return execute("SELECT last_insert_rowid()").fetchone()[0]


def _seed_file(rel: str) -> int:
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, version_group) VALUES ('r', ?, ?, 1, 'g')",
        (rel, "f" * 64),
    )
    commit()
    return execute("SELECT last_insert_rowid()").fetchone()[0]


def test_migration_makes_legacy_inline_restorable(env, backends):
    text = "legacy inline content " * 5
    fid = _seed_legacy_inline(env, "old.txt", text, "c" * 64, len(text))
    out = migration.run_migrations(backends, repair=True)
    assert out["inline_migrated"]["migrated"] == 1
    # Content is now restorable (bytes = stored text re-encoded) and flagged.
    data = restore_bytes(fid, env)
    assert data == text.encode("utf-8")
    row = execute("SELECT migrated, content_text FROM files WHERE id = ?", (fid,)).fetchone()
    assert row["migrated"] == 1 and row["content_text"] is None


def test_migration_flags_lossy_rows(env, backends):
    # UTF-8 replacement chars mean the recomputed hash cannot match the recorded one.
    fid = _seed_legacy_inline(env, "lossy.bin", "bad \ufffd data", "d" * 64, 9)
    out = migration.run_migrations(backends, repair=True)
    assert out["inline_migrated"]["migrated"] == 1
    assert out["inline_migrated"]["lossy"], out


def test_migration_is_idempotent(env, backends):
    _seed_legacy_inline(env, "idem.txt", "idempotent " * 3, "e" * 64, 33)
    migration.run_migrations(backends, repair=True)
    out2 = migration.run_migrations(backends, repair=True)
    assert out2["inline_migrated"]["migrated"] == 0


def test_migration_reports_unrecoverable_chunk_rows(env, backends):
    fid = _seed_file("ghost.txt")
    from backupme.core.database import get_connection
    # Legacy broken rows exist because constraints were bypassed historically.
    conn = get_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    execute(
        "INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, 0, 'missing-blob', 0, 1)",
        (fid,),
    )
    conn.execute("PRAGMA foreign_keys = ON")
    commit()
    out = migration.run_migrations(backends, repair=True)
    assert any(e["blob_id"] == "missing-blob" for e in out["unrecoverable"])


def test_db_check_command_runs_migration(env, backends, capsys):
    from click.testing import CliRunner
    from backupme.cli.base import cli
    text = "cli migration " * 5
    _seed_legacy_inline(env, "cli.txt", text, "c" * 64, len(text))
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(env / "test.db"), "db", "check", "--repair"])
    assert result.exit_code == 0, result.output
    assert "inline migrated: 1" in result.output

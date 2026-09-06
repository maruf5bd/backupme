from backupme.core.verify import get_report
from tests.helpers import backup_bytes


def test_report_includes_migration_sections(env, backends):
    backup_bytes(env, "r.txt", b"report " * 50, backends)
    rep = get_report()
    assert rep["migrated_lossy"] == 0
    assert rep["unrecoverable_files"] == []


def test_report_counts_migrated_lossy(env, backends):
    from backupme.core.database import execute, commit
    from backupme.core import migration
    execute(
        """INSERT INTO files (source_root, relative_path, sha256, size, version_group, content_text)
           VALUES ('r', 'l.txt', ?, 5, 'g', ?)""",
        ("a" * 64, "abc\\def"),
    )
    commit()
    migration.run_migrations(backends, repair=True)
    rep = get_report()
    assert rep["migrated_lossy"] == 1


def test_report_lists_unrecoverable(env, backends):
    from backupme.core.database import execute, commit, get_connection
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, version_group) VALUES ('r', 'ghost.txt', ?, 1, 'g')",
        ("f" * 64,),
    )
    fid = execute("SELECT last_insert_rowid()").fetchone()[0]
    commit()
    conn = get_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    execute(
        "INSERT INTO file_chunks (file_id, seq, blob_id, pt_offset, pt_size) VALUES (?, 0, 'gone-blob', 0, 1)",
        (fid,),
    )
    conn.execute("PRAGMA foreign_keys = ON")
    commit()
    rep = get_report()
    assert [e["blob_id"] for e in rep["unrecoverable_files"]] == ["gone-blob"]
    assert any("re-backup" in r for r in rep["recommendations"])

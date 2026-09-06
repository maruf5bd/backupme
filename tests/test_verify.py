import tempfile
from pathlib import Path

from backupme.core.verify import run_verify_restore, run_integrity_check
from tests.helpers import backup_bytes


def test_verify_restore_uses_configured_count(env, backends):
    for i in range(5):
        backup_bytes(env, f"c{i}.txt", ("content %d " % i).encode() * 30, backends)
    # Rewrite scratch config to set verify_restore.count = 2
    cfg = Path(env / "config.yaml")
    cfg.write_text(cfg.read_text() + "verification:\n  verify_restore:\n    count: 2\n")
    run_verify_restore()
    checks = execute_last_check()
    assert checks["total_files"] == 2


def test_integrity_check_cleans_temp_dirs(env, backends):
    backup_bytes(env, "t.txt", b"temp check " * 50, backends)
    tmp_root = Path(tempfile.gettempdir())
    before = {p.name for p in tmp_root.glob("tmp*")}
    run_integrity_check(count=5)
    after = {p.name for p in tmp_root.glob("tmp*")}
    assert after <= before


def execute_last_check():
    from backupme.core.database import execute
    row = execute("SELECT * FROM integrity_checks ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row)

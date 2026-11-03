from pathlib import Path

import pytest
import yaml

from backupme.config import load_config
from backupme.core import dedup
from backupme.core import index
from backupme.core.database import execute
from tests.helpers import backup_bytes


def test_next_version_seq_unknown_path_is_1(env):
    assert index.next_version_seq(str(env), "docs/a.txt") == 1


def test_next_version_seq_counts_indexed_rows(env, backends):
    backup_bytes(env, "docs/a.txt", b"hello", backends)
    assert index.next_version_seq(str(env), "docs/a.txt") == 2


def test_next_version_seq_counts_soft_deleted_rows(env, backends):
    res = backup_bytes(env, "docs/a.txt", b"hello", backends)
    index.delete_file(res["file_id"])
    # The row is soft-deleted but its version_seq must never be reused.
    assert index.next_version_seq(str(env), "docs/a.txt") == 2


def test_hard_delete_file_removes_rows_and_unrefs_blob(env, backends):
    res = backup_bytes(env, "docs/a.txt", b"hello", backends)
    file_id = res["file_id"]
    blob_id = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ?", (file_id,)
    ).fetchone()[0]
    assert execute("SELECT COUNT(*) FROM file_chunks WHERE file_id = ?", (file_id,)).fetchone()[0] == 1
    assert dedup.get_blob(blob_id) is not None

    index.hard_delete_file(file_id)

    assert execute("SELECT COUNT(*) FROM files WHERE id = ?", (file_id,)).fetchone()[0] == 0
    assert execute("SELECT COUNT(*) FROM file_chunks WHERE file_id = ?", (file_id,)).fetchone()[0] == 0
    # Sole reference dropped -> blob row (and its storage rows) deleted.
    assert dedup.get_blob(blob_id) is None
    assert execute("SELECT COUNT(*) FROM blob_storage WHERE blob_id = ?", (blob_id,)).fetchone()[0] == 0


def test_hard_delete_file_only_drops_refcount(env, backends):
    # Two files share the same content -> same bundle member blob.
    r1 = backup_bytes(env, "docs/a.txt", b"hello", backends)
    r2 = backup_bytes(env, "docs/b.txt", b"hello", backends)
    blob_id = execute(
        "SELECT blob_id FROM file_chunks WHERE file_id = ?", (r1["file_id"],)
    ).fetchone()[0]
    before = dedup.get_blob(blob_id)["ref_count"]

    index.hard_delete_file(r1["file_id"])

    assert execute("SELECT COUNT(*) FROM files WHERE id = ?", (r1["file_id"],)).fetchone()[0] == 0
    row = dedup.get_blob(blob_id)
    assert row is not None
    assert row["ref_count"] == before - 1


def _write_ingest_yaml(path: Path):
    cfg = {
        "cpanel_ingest": {
            "enabled": True,
            "drop_root": "/srv/drop",
            "staging_dir": "/tmp/stage",
            "staging_budget_gb": 25,
            "max_parallel": 4,
            "bandwidth_limit_mbps": 50,
            "keep_raw": 3,
            "raw_backend": "usb_drive",
            "account_pattern": "^cpmove-(?P<user>[a-z0-9]+)",
            "exclude_members": ["*/mail/*/cur/*", "*/.trash/*"],
            "max_member_size_gb": 2,
        }
    }
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def test_ingest_config_parses_all_fields(tmp_path):
    p = tmp_path / "ingest.yaml"
    _write_ingest_yaml(p)
    cfg = load_config(str(p))
    ing = cfg.cpanel_ingest
    assert ing.enabled is True
    assert ing.drop_root == "/srv/drop"
    assert ing.staging_dir == "/tmp/stage"
    assert ing.staging_budget_gb == 25.0
    assert ing.max_parallel == 4
    assert ing.bandwidth_limit_mbps == 50.0
    assert ing.keep_raw == 3
    assert ing.raw_backend == "usb_drive"
    assert ing.account_pattern == "^cpmove-(?P<user>[a-z0-9]+)"
    assert ing.exclude_members == ["*/mail/*/cur/*", "*/.trash/*"]
    assert ing.max_member_size_gb == 2.0


def test_ingest_config_defaults_without_section(tmp_path):
    p = tmp_path / "plain.yaml"
    p.write_text("backends:\n  local:\n  - name: l\n    path: /tmp/x\n", encoding="utf-8")
    cfg = load_config(str(p))
    ing = cfg.cpanel_ingest
    assert ing.enabled is False
    assert ing.drop_root == ""
    assert ing.staging_dir == "staging"
    assert ing.staging_budget_gb == 10.0
    assert ing.max_parallel == 2
    assert ing.bandwidth_limit_mbps == 0.0
    assert ing.keep_raw == 0
    assert ing.raw_backend == ""
    assert ing.account_pattern == ""
    assert ing.exclude_members == []
    assert ing.max_member_size_gb == 5.0

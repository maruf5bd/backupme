"""Scheduler data-health tick (backlog B11): summary in verify tick + regression warning."""
import logging

import pytest

from backupme.core.database import execute, commit
from backupme.scheduler import BackupScheduler

LOG_NAME = "backupme.scheduler"  # get_logger prefixes the package


@pytest.fixture()
def daemon():
    return BackupScheduler({"scheduler": {}, "db_lifecycle": {}})


def test_verify_tick_records_health(daemon, caplog):
    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        daemon._run_scheduled_verify()
    assert daemon._last_health is not None
    for key in ("live_files", "no_chunks_files", "missing_blob_files",
                "missing_blobs", "same_name_groups", "purge_candidates"):
        assert key in daemon._last_health
    assert any("health tick" in r.message for r in caplog.records)


def test_health_tick_warns_on_regression(daemon, caplog):
    daemon._last_health = {"live_files": 0, "no_chunks_files": 0,
                           "missing_blob_files": 0, "missing_blobs": 0,
                           "same_name_groups": 0, "purge_candidates": 0}
    # a live file with no chunk rows = purge candidate + no_chunks regression
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
        "VALUES ('docs', 'broken.txt', 'f' * 64, 10, 'vg-broken')"
    )
    commit()
    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        daemon._log_health()
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("data health regression" in m for m in warnings)


def test_health_tick_first_run_no_regression_warning(daemon, caplog):
    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        daemon._log_health()
    assert not any("data health regression" in r.message for r in caplog.records)

"""Failure notification dispatcher (backlog B16)."""
import logging

import pytest

from backupme.core import notify
from backupme.core.database import execute, commit
from backupme.scheduler import BackupScheduler


@pytest.fixture()
def no_toast(monkeypatch):
    """Replace the desktop toast with a recorder."""
    calls = []
    monkeypatch.setattr(notify, "_desktop_toast",
                        lambda title, message: calls.append((title, message)))
    return calls


def test_always_logs_warning(no_toast, caplog):
    with caplog.at_level(logging.WARNING, logger="backupme.core.notify"):
        result = notify.notify_failure("boom", "detail text")
    assert result == {"logged": True, "desktop": "sent"}
    assert any("boom: detail text" in r.message for r in caplog.records)


def test_defaults_no_config_attempt_toast(no_toast, caplog):
    result = notify.notify_failure("boom", "detail")
    assert result["desktop"] == "sent"
    assert no_toast == [("boom", "detail")]


def test_desktop_disabled_skips_toast(no_toast):
    cfg = {"notifications": {"on_failure": True, "desktop": False}}
    result = notify.notify_failure("boom", "detail", cfg)
    assert result["desktop"] == "skipped-disabled"
    assert no_toast == []


def test_on_failure_disabled_skips_toast(no_toast):
    cfg = {"notifications": {"on_failure": False, "desktop": True}}
    result = notify.notify_failure("boom", "detail", cfg)
    assert result["desktop"] == "skipped-disabled"
    assert no_toast == []


def test_dataclass_config_supported(no_toast):
    from backupme.config import NotificationsConfig
    cfg = type("C", (), {"notifications": NotificationsConfig(on_failure=True, desktop=True)})()
    result = notify.notify_failure("boom", "detail", cfg)
    assert result["desktop"] == "sent"


def test_toast_error_never_propagates(monkeypatch):
    def bad_toast(title, message):
        raise RuntimeError("no gui")
    monkeypatch.setattr(notify, "_desktop_toast", bad_toast)
    result = notify.notify_failure("boom", "detail")
    assert result["desktop"] == "error"


def test_scheduler_regression_notifies(no_toast):
    daemon = BackupScheduler({"notifications": {"on_failure": True, "desktop": True}})
    daemon._last_health = {"live_files": 0, "no_chunks_files": 0,
                           "missing_blob_files": 0, "missing_blobs": 0,
                           "same_name_groups": 0, "purge_candidates": 0}
    execute(
        "INSERT INTO files (source_root, relative_path, sha256, size, version_group) "
        "VALUES ('docs', 'broken2.txt', 'a' * 64, 10, 'vg-broken2')"
    )
    commit()
    daemon._log_health()
    assert len(no_toast) == 1
    assert no_toast[0][0] == "Data health regression"

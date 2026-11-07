"""B07 regression: web-route failures are logged, never silently swallowed.

Backlog B07 removed every silent except-pass in backupme/web/routes/*.py.
These tests prove representative conversions: the HTTP contract is unchanged
(same status codes and default payloads), but the failure now shows up in the
log under the module's own logger name (backupme.web.routes.<module>).

Covered spots:
- dbops.py      /api/db/info survives a failing journal_mode PRAGMA (wal_mode
                falls back to False) and logs a warning;
- misc.py       /api/reports survives a failing integrity_checks query
                (returns []) and logs a warning;
- scheduler.py  /api/scheduler/config survives a corrupt config.yaml (returns
                the defaults) and logs a warning.
"""
import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.core import database as dbmod


@pytest.fixture()
def client(env, backends):
    from backupme.config import load_config
    from backupme.web.app import create_app
    return TestClient(create_app(load_config()))


def _route_records(caplog, module: str):
    name = f"backupme.web.routes.{module}"
    return [r for r in caplog.records if r.name == name]


def test_db_info_wal_pragma_failure_logs(client, monkeypatch, caplog):
    """A failing journal_mode PRAGMA keeps the 200 + wal_mode:false contract
    and logs through backupme.web.routes.dbops (B07: was a silent pass)."""
    real_execute = dbmod.execute

    def failing_execute(sql, params=()):
        if "PRAGMA journal_mode" in sql:
            raise sqlite3.OperationalError("journal_mode probe exploded")
        return real_execute(sql, params)

    monkeypatch.setattr(dbmod, "execute", failing_execute)
    with caplog.at_level(logging.WARNING, logger="backupme.web.routes.dbops"):
        response = client.get("/api/db/info")
    assert response.status_code == 200
    assert response.json()["wal_mode"] is False
    records = _route_records(caplog, "dbops")
    assert records, "expected a warning from web.routes.dbops"
    assert any("journal_mode" in r.getMessage() for r in records)


def test_reports_query_failure_logs(client, monkeypatch, caplog):
    """A failing integrity_checks query keeps the 200 + [] contract and logs
    through backupme.web.routes.misc (B07: was a silent return of [])."""
    real_execute = dbmod.execute

    def failing_execute(sql, params=()):
        if "integrity_checks" in sql:
            raise sqlite3.OperationalError("no such table: integrity_checks")
        return real_execute(sql, params)

    monkeypatch.setattr(dbmod, "execute", failing_execute)
    with caplog.at_level(logging.WARNING, logger="backupme.web.routes.misc"):
        response = client.get("/api/reports")
    assert response.status_code == 200
    assert response.json() == []
    records = _route_records(caplog, "misc")
    assert records, "expected a warning from web.routes.misc"
    assert any("integrity_checks" in r.getMessage() for r in records)


def test_scheduler_config_broken_yaml_logs(client, caplog):
    """A corrupt config.yaml keeps the defaults contract on
    /api/scheduler/config and logs through backupme.web.routes.scheduler
    (B07: was a silent return of defaults)."""
    Path("config.yaml").write_text("scheduler: [unclosed", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="backupme.web.routes.scheduler"):
        response = client.get("/api/scheduler/config")
    assert response.status_code == 200
    assert response.json() == {"interval": 3600, "enabled": False}
    records = _route_records(caplog, "scheduler")
    assert records, "expected a warning from web.routes.scheduler"
    assert any("scheduler config" in r.getMessage() for r in records)

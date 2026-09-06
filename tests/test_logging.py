import logging

import pytest

from backupme.core.uploader import upload_bundle
from tests.helpers import load_cfg


def test_processing_failure_is_logged(env, backends, caplog):
    with caplog.at_level(logging.ERROR, logger="backupme"):
        results = upload_bundle(
            [{"local_path": str(env / "no-such-file.txt"),
              "source_root": str(env), "relative_path": "no-such-file.txt"}],
            backends, load_cfg(), 1,
        )
    assert results[0]["status"] == "error"
    assert any("no-such-file.txt" in r.message and r.levelno == logging.ERROR
               for r in caplog.records)


def test_backend_connect_failure_is_logged(caplog, tmp_path):
    from backupme.backends import get_backend
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    with caplog.at_level(logging.WARNING, logger="backupme"):
        bk = get_backend("gone", {"local": [{"name": "gone",
                                             "path": str(blocker / "sub")}]})
    assert bk is None
    assert any(r.levelno == logging.WARNING and "gone" in r.message
               for r in caplog.records)


def test_setup_logging_levels():
    from backupme.log import setup_logging, get_logger
    setup_logging(False)
    assert get_logger("x").getEffectiveLevel() == logging.INFO
    setup_logging(True)
    assert get_logger("x").getEffectiveLevel() == logging.DEBUG
    setup_logging(False)

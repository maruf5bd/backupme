import pytest

from pathlib import Path

from backupme.backends import get_all_backends, invalidate_cache


def _impossible_path(tmp_path) -> str:
    """A path that can never be created: its parent is a regular file."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    return str(blocker / "sub" / "dir")


def test_unreachable_backend_is_skipped_not_fatal(env, backends, tmp_path):
    """A backend whose base path cannot be created must be skipped, not crash everything."""
    from tests.helpers import add_backend, load_cfg
    add_backend(env, "unreachable", Path(_impossible_path(tmp_path)))
    invalidate_cache()
    bks = get_all_backends(load_cfg())
    names = [b.name for b in bks]
    assert "test_local" in names
    assert "unreachable" not in names


def test_get_backend_unreachable_returns_none(tmp_path):
    from backupme.backends import get_backend
    bk = get_backend("nope", {"local": [{"name": "nope", "path": _impossible_path(tmp_path)}]})
    assert bk is None

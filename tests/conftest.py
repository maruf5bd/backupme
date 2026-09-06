import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backupme.core import database as dbmod  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Safety net: every test runs against a scratch DB, never the real one."""
    monkeypatch.chdir(tmp_path)
    from backupme.backends import invalidate_cache
    invalidate_cache()
    dbmod.close_connection()
    dbmod.DB_PATH = tmp_path / "isolated.db"
    dbmod.initialize()
    yield
    dbmod.close_connection()
    invalidate_cache()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Scratch environment: isolated config, database, storage backend, and CWD."""
    root = tmp_path / "env"
    root.mkdir()
    _write_config(root, encrypted=False)
    monkeypatch.setenv("BACKUPME_CONFIG", str(root / "config.yaml"))
    monkeypatch.chdir(root)
    from backupme.backends import invalidate_cache
    invalidate_cache()
    dbmod.close_connection()
    dbmod.DB_PATH = root / "test.db"
    dbmod.initialize()
    yield root
    dbmod.close_connection()
    invalidate_cache()


@pytest.fixture()
def enc_env(tmp_path, monkeypatch):
    """Like env, but with AES-GCM encryption enabled and a key file on disk."""
    root = tmp_path / "encenv"
    root.mkdir()
    from backupme.core import crypto as crypto_mod
    key_path = root / "test.key"
    crypto_mod.save_key(crypto_mod.generate_key(), str(key_path))
    _write_config(root, encrypted=True, key_file=str(key_path))
    monkeypatch.setenv("BACKUPME_CONFIG", str(root / "config.yaml"))
    monkeypatch.chdir(root)
    from backupme.backends import invalidate_cache
    invalidate_cache()
    dbmod.close_connection()
    dbmod.DB_PATH = root / "test.db"
    dbmod.initialize()
    yield root
    dbmod.close_connection()
    invalidate_cache()


def _write_config(root: Path, encrypted: bool, key_file: str | None = None):
    lines = [
        "backends:",
        "  local:",
        "  - name: test_local",
        f"    path: {(root / 'storage').as_posix()}",
        "    priority: 100",
        "encryption:",
        f"  enabled: {'true' if encrypted else 'false'}",
    ]
    if key_file:
        lines.append(f"  key_file: {Path(key_file).as_posix()}")
    (root / "config.yaml").write_text("\n".join(lines) + "\n")


@pytest.fixture()
def backends(env):
    from backupme.config import load_config
    from backupme.backends import get_all_backends
    return get_all_backends(load_config())

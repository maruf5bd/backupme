from tests.helpers import load_cfg
from backupme.core.database import execute


def test_db_initialized(env):
    gen = execute("SELECT generation FROM db_meta WHERE id = 1").fetchone()[0]
    assert gen == 0


def test_backend_connects(backends):
    assert [b.name for b in backends] == ["test_local"]
    assert backends[0].connected


def test_config_isolated_per_test(env):
    assert load_cfg().encryption.enabled is False

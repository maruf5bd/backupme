import shutil
import sqlite3

import pytest

from backupme.core import database as dbmod
from backupme.core.database import get_connection, close_connection, execute, commit


def test_get_connection_honors_path(tmp_path):
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    conn_a = get_connection(str(a))
    conn_a.execute("CREATE TABLE t (x INTEGER)")
    conn_a.commit()
    close_connection()
    conn_a2 = get_connection(str(a))
    conn_a2.execute("SELECT COUNT(*) FROM t").fetchone()
    with pytest.raises(sqlite3.OperationalError):
        get_connection(str(b)).execute("SELECT COUNT(*) FROM t").fetchone()
    # Same path -> same cached connection object
    assert get_connection(str(a)) is conn_a2


def test_reconstruct_reads_replica_generation(env, backends):
    from backupme.core import snapshot as snapmod
    from backupme.core.recover import _reconstruct_from_backend
    # Generation 3 at snapshot time; primary drifts to 9 afterwards.
    execute("UPDATE db_meta SET generation = 3 WHERE id = 1")
    commit()
    snap = snapmod.create_snapshot()
    bk = backends[0]
    bk.db_upload_chunk(snap["snapshot"], f"__backupme_db__/snapshots/{snap['snapshot']}")
    execute("UPDATE db_meta SET generation = 9 WHERE id = 1")
    commit()
    candidate = _reconstruct_from_backend(bk)
    assert candidate["generation"] == 3


def test_recover_picks_newest_replica(env, backends, tmp_path):
    from backupme.core import snapshot as snapmod
    from backupme.core import recover
    bk = backends[0]
    execute("UPDATE db_meta SET generation = 3 WHERE id = 1")
    commit()
    s3 = snapmod.create_snapshot()
    bk.db_upload_chunk(s3["snapshot"], f"__backupme_db__/snapshots/{s3['snapshot']}")
    execute("UPDATE db_meta SET generation = 7 WHERE id = 1")
    commit()
    s7 = snapmod.create_snapshot()
    bk.db_upload_chunk(s7["snapshot"], f"__backupme_db__/snapshots/{s7['snapshot']}")

    # Corrupt the primary DB file so recovery kicks in.
    close_connection()
    shutil.copy2(dbmod.DB_PATH, tmp_path / "good.db")
    dbmod.DB_PATH.write_bytes(b"this is not a database")
    result = recover.recover_database([bk])
    assert result["status"] == "restored"
    assert result["generation"] == 7

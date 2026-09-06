import sqlite3
import threading
import uuid
from pathlib import Path

from backupme.core.schema import SQL_WAL_PRAGMAS, apply_migrations

DB_PATH = Path("backupme.db")

_local = threading.local()
_write_lock = threading.Lock()


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or str(DB_PATH)
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = _create_connection(path)
    else:
        try:
            _local.conn.execute("SELECT 1")
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            _local.conn = _create_connection(path)
    return _local.conn


def _create_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    for pragma in SQL_WAL_PRAGMAS.strip().split(";"):
        p = pragma.strip()
        if p:
            conn.execute(p + ";")
    conn.row_factory = sqlite3.Row
    return conn


def initialize():
    conn = get_connection()
    apply_migrations(conn)
    conn.execute(
        "INSERT OR IGNORE INTO db_meta (id, generation, instance_id) VALUES (1, 0, ?)",
        (uuid.uuid4().hex,),
    )
    conn.commit()


def close_connection():
    if hasattr(_local, "conn") and _local.conn:
        _local.conn.close()
        _local.conn = None


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    return get_connection().execute(sql, params)


def executemany(sql: str, params_list: list[tuple]) -> sqlite3.Cursor:
    return get_connection().executemany(sql, params_list)


def commit():
    get_connection().commit()


def rollback():
    get_connection().rollback()


def get_generation() -> int:
    row = execute("SELECT generation FROM db_meta WHERE id = 1").fetchone()
    return row[0] if row else 0


def bump_generation():
    execute("UPDATE db_meta SET generation = generation + 1 WHERE id = 1")


def get_instance_id() -> str:
    row = execute("SELECT instance_id FROM db_meta WHERE id = 1").fetchone()
    return row[0] if row else ""


def check_integrity() -> list[str]:
    rows = execute("PRAGMA integrity_check").fetchall()
    return [r[0] for r in rows if r[0] != "ok"]


def wal_checkpoint() -> None:
    execute("PRAGMA wal_checkpoint(TRUNCATE)")

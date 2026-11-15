import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from backupme.core.schema import SQL_WAL_PRAGMAS, apply_migrations

DB_PATH = Path("backupme.db")

_local = threading.local()


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = str(db_path or DB_PATH)
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    conn = conns.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            conn = None
    if conn is None:
        conn = _create_connection(path)
        conns[path] = conn
    return conn


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


def close_connection(db_path: str | None = None) -> None:
    conns = getattr(_local, "conns", None)
    if not conns:
        return
    if db_path is None:
        for conn in conns.values():
            try:
                conn.close()
            except Exception:
                pass
        conns.clear()
    else:
        conn = conns.pop(str(db_path), None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    return get_connection().execute(sql, params)


def executemany(sql: str, params_list: list[tuple]) -> sqlite3.Cursor:
    return get_connection().executemany(sql, params_list)


def commit():
    get_connection().commit()


def rollback():
    get_connection().rollback()


@contextmanager
def write_txn():
    """Explicit write transaction: commit on success, rollback on any error.

    SQLite opens a write transaction implicitly on the first INSERT/UPDATE;
    without explicit control a failure mid-unit leaves the write transaction
    OPEN on the pooled thread-local connection — blocking other connections
    until some later trailing commit makes the partial work durable.
    Units of work in the upload path use this to guarantee all-or-nothing.
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise


def get_generation() -> int:
    row = execute("SELECT generation FROM db_meta WHERE id = 1").fetchone()
    return row[0] if row else 0


def bump_generation():
    execute("UPDATE db_meta SET generation = generation + 1 WHERE id = 1")


def get_instance_id() -> str:
    row = execute("SELECT instance_id FROM db_meta WHERE id = 1").fetchone()
    return row[0] if row else ""


def check_integrity() -> list[str]:
    try:
        rows = execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as e:
        return [f"corrupt: {e}"]
    return [r[0] for r in rows if r[0] != "ok"]


def wal_checkpoint() -> None:
    execute("PRAGMA wal_checkpoint(TRUNCATE)")

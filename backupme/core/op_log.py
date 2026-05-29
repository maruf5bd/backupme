import json
import uuid
from datetime import datetime

from backupme.core.database import execute, commit, get_generation


def append(
    op_type: str,
    target: dict,
    idempotency_key: str | None = None,
    precondition: str | None = None,
    status: str = "pending",
) -> dict:
    ik = idempotency_key or uuid.uuid4().hex
    existing = execute(
        "SELECT * FROM op_log WHERE idempotency_key = ?", (ik,)
    ).fetchone()
    if existing:
        return dict(existing)

    execute(
        """INSERT INTO op_log (idempotency_key, op_type, target_json, precondition, status)
           VALUES (?, ?, ?, ?, ?)""",
        (ik, op_type, json.dumps(target), precondition, status),
    )
    commit()
    row = execute("SELECT * FROM op_log WHERE idempotency_key = ?", (ik,)).fetchone()
    return dict(row)


def mark_committed(op_id: int):
    execute(
        "UPDATE op_log SET status = 'committed', completed_at = datetime('now') WHERE id = ?",
        (op_id,),
    )
    commit()


def mark_failed(op_id: int, error: str):
    execute(
        "UPDATE op_log SET status = 'failed', completed_at = datetime('now'), last_error = ? WHERE id = ?",
        (error, op_id),
    )
    commit()


def mark_rolled_back(op_id: int):
    execute(
        "UPDATE op_log SET status = 'rolled_back', completed_at = datetime('now') WHERE id = ?",
        (op_id,),
    )
    commit()


def get_pending() -> list[dict]:
    rows = execute("SELECT * FROM op_log WHERE status = 'pending' ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def replay_since(op_log_id: int = 0) -> list[dict]:
    rows = execute(
        "SELECT * FROM op_log WHERE id > ? AND status = 'committed' ORDER BY id",
        (op_log_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["target"] = json.loads(d["target_json"])
        result.append(d)
    return result


def verify_precondition(precondition: str | None) -> bool:
    if precondition is None:
        return True
    parts = precondition.split(":")
    if len(parts) != 3:
        return True
    entity_type, entity_id, condition = parts
    if condition == "not_exists":
        row = execute(
            f"SELECT 1 FROM {entity_type} WHERE blob_id = ?",
            (entity_id,),
        ).fetchone()
        return row is None
    if condition.startswith("gen_lt"):
        try:
            max_gen = int(condition.split("_")[2])
            return get_generation() < max_gen
        except (ValueError, IndexError):
            return True
    return True


def get_status_stats() -> dict:
    rows = execute(
        "SELECT status, COUNT(*) AS cnt FROM op_log GROUP BY status"
    ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}


def prune(before_id: int):
    execute("DELETE FROM op_log WHERE id < ? AND status IN ('committed', 'rolled_back', 'failed')", (before_id,))
    commit()
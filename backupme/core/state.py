import json
import os
import time
from pathlib import Path


STATE_FILE = Path("backupme.state.json")


def save_checkpoint(op_type: str, data: dict):
    state = _load_raw()
    state["last_checkpoint"] = {
        "type": op_type,
        "data": data,
        "timestamp": time.time(),
    }
    state.setdefault("checkpoints", [])
    state["checkpoints"].append(state["last_checkpoint"])
    if len(state["checkpoints"]) > 100:
        state["checkpoints"] = state["checkpoints"][-100:]
    _write_atomic(state)


def load_state() -> dict | None:
    state = _load_raw()
    return state.get("last_checkpoint")


def get_last_checkpoint() -> dict | None:
    return load_state()


def clear_state():
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def _load_raw() -> dict:
    if not STATE_FILE.exists():
        return {"checkpoints": [], "last_checkpoint": None}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, Exception):
        return {"checkpoints": [], "last_checkpoint": None}


def _write_atomic(state: dict):
    tmp = STATE_FILE.with_suffix(".state.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)
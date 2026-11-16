"""Checkpoint/state helpers.

This module hosts two related but distinct mechanisms:
- ``save_checkpoint`` / ``load_state``: the global rolling state file
  (``backupme.state.json``) used by higher-level ops to record progress.
- ``read_json_checkpoint`` / ``write_json_checkpoint``: resilient per-operation
  checkpoint files (converter, rebalance, register, re-back) with atomic writes.

Both file-based checkpoint families share the same fault-tolerance rule: a
missing or corrupt file is treated as "start fresh", and writes are atomic.
"""

import json
import time
from pathlib import Path
from typing import Any, Callable


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


def read_json_checkpoint(path: Path | str, default_factory: Callable[[], Any]) -> Any:
    """Load a JSON checkpoint file, returning ``default_factory()`` on failure.

    Resilient to missing, unreadable, or malformed files so per-operation
    progress can resume from a known-good default.
    """
    p = Path(path)
    if not p.exists():
        return default_factory()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default_factory()


def write_json_checkpoint(path: Path | str, data: Any, *, sort_keys: bool = False) -> None:
    """Atomically write a JSON checkpoint file (tmp + replace)."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp") if p.suffix else Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=sort_keys), encoding="utf-8")
    tmp.replace(p)


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

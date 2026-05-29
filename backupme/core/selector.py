import re
from difflib import SequenceMatcher

from .. import config as cfgmod


def auto_select_backend(config: cfgmod.Config, required_capacity: int = 0) -> tuple[str, str] | None:
    candidates = []
    for btype, blist in config.backends.items():
        for be in blist:
            if _has_capacity(be, required_capacity):
                candidates.append((btype, be.name, be.priority, be.weight))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[2], -c[3]))
    return (candidates[0][0], candidates[0][1])


def fuzzy_find_backend(query: str, config: cfgmod.Config) -> list[tuple[str, str, float]]:
    results = []
    for btype, blist in config.backends.items():
        for be in blist:
            score = max(
                SequenceMatcher(None, query.lower(), be.name.lower()).ratio(),
                SequenceMatcher(None, query.lower(), btype.lower()).ratio(),
            )
            if query.lower() in be.name.lower():
                score = max(score, 0.9)
            if score > 0.3:
                results.append((btype, be.name, score))
    results.sort(key=lambda r: -r[2])
    return results


def manual_select_backend(query: str, config: cfgmod.Config) -> tuple[str, str] | None:
    results = fuzzy_find_backend(query, config)
    if results:
        return (results[0][0], results[0][1])
    return None


def list_available_backends(config: cfgmod.Config) -> list[dict]:
    result = []
    for btype, blist in config.backends.items():
        for be in blist:
            result.append({
                "type": btype,
                "name": be.name,
                "priority": be.priority,
                "weight": be.weight,
                "quota": be.quota,
            })
    return result


def _has_capacity(be: cfgmod.BackendDef, required_bytes: int) -> bool:
    if not be.quota:
        return True
    quota_bytes = _parse_size(be.quota)
    if quota_bytes <= 0:
        return True
    used = _estimate_used(be.name)
    return (quota_bytes - used) >= required_bytes


def _parse_size(s: str) -> int:
    s = s.strip().upper()
    m = re.match(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?", s)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2) or "B"
    multipliers = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return int(num * multipliers.get(unit, 1))


def _estimate_used(backend_name: str) -> int:
    from . import database as db
    row = db.execute(
        "SELECT COALESCE(SUM(dh.size), 0) FROM files f "
        "JOIN dedup_hashes dh ON f.sha256 = dh.sha256 "
        "WHERE f.backend_name = ?",
        (backend_name,),
    ).fetchone()
    return row[0] if row else 0
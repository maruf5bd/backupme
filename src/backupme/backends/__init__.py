"""Backend registry: ONE resolver + ONE instance cache for every layer.

The web layer and the CLI used to keep separate instance caches (the web
`_backend_pool` in web/routes/common.py vs the core `_instance_cache`
here): the same backend resolved into two different objects, so one
dashboard poll could open a second SSH session to the same box — the D10
churn pattern — and the two layers disagreed about which backends even
existed (defect D30). Everything now resolves through this module:

- ONE instance cache keyed by (name, type, location fingerprint) — the D37
  lesson: the same name on a different root must never share an instance
  or cached bytes. A config edit (password, quota, ...) is caught by a
  full-entry drift fingerprint, so a cached instance never serves stale
  credentials.
- ONE failed-connect cooldown (`CONNECT_COOLDOWN_SECONDS`): an unreachable
  backend is cached as failed (connected=False) instead of being re-dialed
  on every request. The cooldown is in-memory per process; a cross-process
  cooldown is future work (documented in brain/defects.md D30).

Honest resolution (defect D30): resolve_backend/resolve_all_backends
return instances with an explicit `connected` flag and
available_backends/unreachable_backends split them for callers — nothing
silently drops a backend. get_backend/get_all_backends keep the pipeline
contract (connected instances only, see tests/test_backends_connect.py)
so core callers are unaffected, but an unreachable backend is logged
loudly there instead of quietly vanishing.

Explicit connects on a resolved instance (backend test, rescan, speed
test) are never gated by the cooldown — only the resolver's automatic
re-dial is.
"""
import json
import threading
import time

from backupme.log import get_logger

from .base import StorageBackend
from .local import LocalBackend
from .ftp_sftp import FTPBackend, SFTPBackend
from .smb import SMBBackend
from .ssh import SSHBackend
from .rclone import RcloneBackend
from .ninedrive import NineDriveBackend

BACKEND_MAP: dict[str, type[StorageBackend]] = {
    "local": LocalBackend,
    "ftp": FTPBackend,
    "sftp": SFTPBackend,
    "smb": SMBBackend,
    "ssh": SSHBackend,
    "rclone": RcloneBackend,
    "ninedrive": NineDriveBackend,
}

log = get_logger("backends")

# Failed-connect cooldown (defect D30): an unreachable backend is remembered
# for this many seconds and resolved as a failed instance (connected=False)
# instead of being re-dialed per request. In-memory per process; a
# cross-process cooldown is future work (see brain/defects.md D30).
CONNECT_COOLDOWN_SECONDS = 60.0

# Location-bearing config fields: the backend's identity of WHERE its data
# lives. The cache key must never collide across different roots (D37).
_LOCATION_FIELDS = ("path", "host", "port", "share", "remote", "base_url",
                    "folder")

# Cache key (name, type, location fingerprint) -> (config fingerprint,
# instance). The config fingerprint detects drift (edited credentials)
# within the same location.
_instance_cache: dict[tuple[str, str, str], tuple[str, StorageBackend]] = {}

# Failed connects: key -> (monotonic retry-after deadline, config
# fingerprint at failure time, failed instance). The fingerprint lets a
# config edit during the cooldown rebuild a fresh instance instead of
# retrying stale credentials.
_failed_cache: dict[tuple[str, str, str], tuple[float, str, StorageBackend]] = {}

_cache_lock = threading.Lock()
# One builder at a time: first resolution of a backend (connect included)
# is single-flighted so concurrent web requests cannot stampede a rate-
# limited remote into double sessions (D10).
_build_lock = threading.Lock()


def _normalize_config(config) -> dict:
    # Config dataclass
    if hasattr(config, "backends"):
        return _config_to_dict(config.backends)
    # Dict with "backends" key (full config dict from dataclasses.asdict)
    if isinstance(config, dict) and "backends" in config:
        be = config["backends"]
        if isinstance(be, dict):
            return _config_to_dict(be)
        return be
    # Already a backends dict (keys are backend types)
    return config


def _config_to_dict(be_map) -> dict:
    """The ONE backend-entry serializer (defect D30): every layer — CLI,
    web and this resolver — converts a backend definition to the same full
    dict, so rclone keeps config_path/extra_args/timeout_s everywhere and
    both layers derive identical cache keys and drift fingerprints."""
    result = {}
    for btype, blist in be_map.items():
        result[btype] = []
        for be in blist:
            if hasattr(be, "__dataclass_fields__"):
                result[btype].append({k: getattr(be, k) for k in be.__dataclass_fields__})
            else:
                result[btype].append(be)
    return result


def _location_fingerprint(entry: dict) -> str:
    """Canonical location identity of one backend entry (the D37 lesson)."""
    payload = {k: entry.get(k) for k in _LOCATION_FIELDS}
    return json.dumps(payload, sort_keys=True, default=str)


def config_fingerprint(entry) -> str:
    """Stable fingerprint of one full backend config entry — the drift
    check for the instance cache: a config edit (password, quota, ...)
    produces a new fingerprint so a cached instance is rebuilt instead of
    serving stale credentials. Location-only identity lives in the cache
    key (see _backend_key)."""
    return json.dumps(entry, sort_keys=True, default=str)


def _backend_key(name: str, btype: str, entry: dict) -> tuple[str, str, str]:
    return (name, btype, _location_fingerprint(entry))


def _disconnect(inst) -> None:
    try:
        inst.disconnect()
    except Exception:
        pass


def _reusable_instance(key: tuple[str, str, str], fingerprint: str):
    """Connected instance with a matching config fingerprint, or None.

    A stale-config or disconnected instance is dropped (and disconnected)
    so the caller builds a fresh one — the old transport may be dead."""
    with _cache_lock:
        hit = _instance_cache.get(key)
        if hit is None:
            return None
        cached_fp, inst = hit
        if cached_fp == fingerprint and inst.connected:
            return inst
        del _instance_cache[key]
    _disconnect(inst)
    return None


def _failed_or_expired(key: tuple[str, str, str], fingerprint: str):
    """(failed instance, still cooling?) from the failed-connect cache.

    The failed entry is popped once its cooldown expired so the caller
    retries the connect on the SAME instance (one object, one session per
    key — never a second parallel session). An entry whose config
    fingerprint drifted (edited while cooling down) is dropped so the
    caller rebuilds a fresh instance with the new credentials."""
    with _cache_lock:
        failed = _failed_cache.get(key)
        if failed is None:
            return None, False
        retry_after, stored_fp, inst = failed
        if stored_fp != fingerprint:
            del _failed_cache[key]
            return None, False
        if time.monotonic() < retry_after:
            return inst, True
        del _failed_cache[key]
    return inst, False


def _store_resolution(key: tuple[str, str, str], fingerprint: str, inst,
                      connected: bool, err: str) -> None:
    if connected:
        with _cache_lock:
            _instance_cache[key] = (fingerprint, inst)
            _failed_cache.pop(key, None)
    else:
        retry_after = time.monotonic() + CONNECT_COOLDOWN_SECONDS
        with _cache_lock:
            _failed_cache[key] = (retry_after, fingerprint, inst)
            _instance_cache.pop(key, None)
    # Resolver contract markers (read via the base.py properties): an
    # explicit connect() is never gated by these, only auto re-resolution.
    inst._connect_retry_after = 0.0 if connected else retry_after
    inst._connect_error = err
    if not connected:
        log.warning(
            "backend %s is unreachable (%s); backing off %.0fs — "
            "resolve_all_backends()/unreachable_backends() still report it",
            inst.name, err or "connect failed", CONNECT_COOLDOWN_SECONDS)


def _resolve_entry(name: str, btype: str, cls: type[StorageBackend],
                   entry: dict) -> StorageBackend:
    """Resolve ONE configured entry to its instance (connected or failed).

    Cache fast path first; construction + connect single-flights under
    _build_lock so concurrent first resolves share one session."""
    key = _backend_key(name, btype, entry)
    fingerprint = config_fingerprint(entry)
    inst = _reusable_instance(key, fingerprint)
    if inst is not None:
        return inst
    with _build_lock:
        # Double-check under the build lock: another thread may have built
        # (or failed) this backend while we waited.
        inst = _reusable_instance(key, fingerprint)
        if inst is not None:
            return inst
        failed, cooling = _failed_or_expired(key, fingerprint)
        if failed is not None and cooling:
            return failed  # still in cooldown — honest failure, no re-dial
        if failed is not None:
            inst = failed  # cooldown over: retry on the SAME instance
        else:
            inst = cls(name, entry)
        err = ""
        try:
            ok = bool(inst.connect())
        except Exception as e:
            err = str(e)
            log.warning("backend %s failed to connect: %s", name, e,
                        exc_info=True)
            ok = False
        if ok:
            err = ""
        elif not err:
            err = "connect failed"
        _store_resolution(key, fingerprint, inst, ok, err)
        return inst


def resolve_backend(name: str, config=None) -> StorageBackend | None:
    """The ONE honest single-backend resolution (defect D30): returns the
    configured instance whether its last connect succeeded or failed —
    callers read `connected` to react. None only when the name is not
    configured. With config=None, only the cache is consulted."""
    if config is None:
        with _cache_lock:
            for key, hit in _instance_cache.items():
                if key[0] == name:
                    return hit[1]
            for key, failed in _failed_cache.items():
                if key[0] == name:
                    return failed[1]
        return None
    cfg = _normalize_config(config)
    for btype, cls in BACKEND_MAP.items():
        for entry in cfg.get(btype, []):
            if entry.get("name") == name:
                return _resolve_entry(name, btype, cls, entry)
    return None


def get_backend(name: str, config=None) -> StorageBackend | None:
    """Connected instance or None — the pipeline contract (unchanged, see
    tests/test_backends_connect.py). Unreachable backends surface via
    resolve_backend / unreachable_backends instead of vanishing here."""
    inst = resolve_backend(name, config)
    if inst is not None and inst.connected:
        return inst
    return None


def resolve_all_backends(config) -> list[StorageBackend]:
    """Every configured instance, connected or not (defect D30 honest
    resolution) — nothing silently dropped. Read `connected` per instance,
    or use available_backends/unreachable_backends to split the list."""
    cfg = _normalize_config(config)
    result = []
    for btype, cls in BACKEND_MAP.items():
        for entry in cfg.get(btype, []):
            name = entry.get("name", f"{btype}_{len(result)}")
            result.append(_resolve_entry(name, btype, cls, entry))
    return result


def available_backends(config) -> list[StorageBackend]:
    """The reachable instances (defect D30 helper) — for pipelines that
    must touch the network."""
    return [b for b in resolve_all_backends(config) if b.connected]


def unreachable_backends(config) -> list[StorageBackend]:
    """Configured instances whose last connect failed — the loud companion
    to available_backends (defect D30): the gap between configured and
    reachable backends is reportable, never silent."""
    return [b for b in resolve_all_backends(config) if not b.connected]


def get_all_backends(config) -> list[StorageBackend]:
    """Connected instances only — the pipeline contract (unchanged, see
    tests/test_backends_connect.py). An unreachable backend is logged
    loudly here so guard/gc-style callers see the gap instead of missing a
    backend silently; resolve_all_backends()/unreachable_backends() report
    it programmatically."""
    result = []
    for inst in resolve_all_backends(config):
        if inst.connected:
            result.append(inst)
        else:
            log.warning(
                "backend %s is unreachable (connect failed); excluded from "
                "this run — see unreachable_backends() for the honest list",
                inst.name)
    return result


def invalidate_cache(name: str | None = None):
    """Drop cached instance(s) — all, or every location variant of one
    name. Callers: web app registration (per-app test isolation), config
    edits, and test isolation (tests/conftest.py)."""
    with _cache_lock:
        if name:
            stale = [v[1] for k, v in _instance_cache.items() if k[0] == name]
            stale += [v[2] for k, v in _failed_cache.items() if k[0] == name]
            for k in [k for k in _instance_cache if k[0] == name]:
                del _instance_cache[k]
            for k in [k for k in _failed_cache if k[0] == name]:
                del _failed_cache[k]
        else:
            stale = [v[1] for v in _instance_cache.values()]
            stale += [v[2] for v in _failed_cache.values()]
            _instance_cache.clear()
            _failed_cache.clear()
    for inst in stale:
        _disconnect(inst)

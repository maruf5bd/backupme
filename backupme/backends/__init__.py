from .base import StorageBackend
from .local import LocalBackend
from .ftp_sftp import FTPBackend, SFTPBackend
from .smb import SMBBackend
from .ssh import SSHBackend
from .rclone import RcloneBackend

BACKEND_MAP: dict[str, type[StorageBackend]] = {
    "local": LocalBackend,
    "ftp": FTPBackend,
    "sftp": SFTPBackend,
    "smb": SMBBackend,
    "ssh": SSHBackend,
    "rclone": RcloneBackend,
}

_instance_cache: dict[str, StorageBackend] = {}


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
    result = {}
    for btype, blist in be_map.items():
        result[btype] = []
        for be in blist:
            if hasattr(be, "__dataclass_fields__"):
                result[btype].append({k: getattr(be, k) for k in be.__dataclass_fields__})
            else:
                result[btype].append(be)
    return result


def get_backend(name: str, config=None) -> StorageBackend | None:
    if name in _instance_cache:
        inst = _instance_cache[name]
        if inst.connected:
            return inst
        # Stale instance — remove and re-create
        _instance_cache.pop(name, None)
    if config is None:
        return None
    cfg = _normalize_config(config)
    for btype, cls in BACKEND_MAP.items():
        be = cfg.get(btype, [])
        for entry in be:
            if entry.get("name") == name:
                inst = cls(name, entry)
                try:
                    connected = inst.connect()
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "backend %s failed to connect", name, exc_info=True)
                    connected = False
                if connected:
                    _instance_cache[name] = inst
                    return inst
    return None


def get_all_backends(config) -> list[StorageBackend]:
    cfg = _normalize_config(config)
    result = []
    for btype, cls in BACKEND_MAP.items():
        for entry in cfg.get(btype, []):
            name = entry.get("name", f"{btype}_{len(result)}")
            if name in _instance_cache:
                result.append(_instance_cache[name])
                continue
            inst = cls(name, entry)
            try:
                connected = inst.connect()
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "backend %s failed to connect; skipping", name, exc_info=True)
                connected = False
            if connected:
                _instance_cache[name] = inst
                result.append(inst)
    return result


def invalidate_cache(name: str | None = None):
    if name:
        _instance_cache.pop(name, None)
    else:
        _instance_cache.clear()
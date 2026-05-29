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


def get_backend(name: str, config: dict | None = None) -> StorageBackend | None:
    if name in _instance_cache:
        return _instance_cache[name]
    if config is None:
        return None
    for btype, cls in BACKEND_MAP.items():
        be = config.get(btype, [])
        for entry in be:
            if entry.get("name") == name:
                inst = cls(name, entry)
                if inst.connect():
                    _instance_cache[name] = inst
                    return inst
    return None


def get_all_backends(config: dict) -> list[StorageBackend]:
    result = []
    for btype, cls in BACKEND_MAP.items():
        for entry in config.get(btype, []):
            inst = cls(entry.get("name", f"{btype}_{len(result)}"), entry)
            if inst.connect():
                result.append(inst)
    return result


def invalidate_cache(name: str | None = None):
    if name:
        _instance_cache.pop(name, None)
    else:
        _instance_cache.clear()
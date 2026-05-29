import os, re, yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

ENV_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

@dataclass
class ChunkingConfig:
    enabled: bool = True
    min_size: int = 1_048_576
    chunk_size: int = 67_108_864
    parallel_uploads: int = 4

@dataclass
class CompressOverride:
    ext: str = ""
    algorithm: str = "zstd"
    level: int = 3
    skip: bool = False

@dataclass
class CompressionConfig:
    enabled: bool = True
    default_algorithm: str = "zstd"
    default_level: int = 3
    min_size: int = 1024
    intelligent_skip: bool = True
    overrides: list[CompressOverride] = field(default_factory=list)

@dataclass
class EncryptionConfig:
    enabled: bool = False
    method: str = "aes-256-gcm"
    key_file: str = ""

@dataclass
class VerificationConfig:
    enabled: bool = True
    retries: int = 3
    fallback_on_fail: bool = True
    verify_restore_count: int = 5
    verify_restore_schedule: str = "weekly"

@dataclass
class RetentionConfig:
    enabled: bool = True
    versions: int = 5
    daily: int = 7
    weekly: int = 4
    monthly: int = 12

@dataclass
class DatabaseSnapshotConfig:
    interval: int = 3600
    retention: int = 168

@dataclass
class DatabaseAutoBackupConfig:
    enabled: bool = True
    backends: str = "all"

@dataclass
class DatabaseConfig:
    snapshots: DatabaseSnapshotConfig = field(default_factory=DatabaseSnapshotConfig)
    auto_backup: DatabaseAutoBackupConfig = field(default_factory=DatabaseAutoBackupConfig)

@dataclass
class NotificationsConfig:
    on_failure: bool = True
    desktop: bool = True

@dataclass
class BackendDef:
    type: str = ""
    name: str = ""
    path: str = ""
    host: str = ""
    port: int = 21
    username: str = ""
    password: str = ""
    key_file: str = ""
    share: str = ""
    remote: str = ""
    bwlimit: str = ""
    quota: str = ""
    priority: int = 50
    weight: int = 10

@dataclass
class BackupDef:
    source: str = ""
    exclude: list[str] = field(default_factory=list)
    mirror: bool = False
    backend: Optional[str] = None

@dataclass
class Config:
    backups: list[BackupDef] = field(default_factory=list)
    backends: dict[str, list[BackendDef]] = field(default_factory=dict)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    compression: CompressionConfig = field(default_factory=CompressionConfig)
    encryption: EncryptionConfig = field(default_factory=EncryptionConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)


def _resolve_env(value: str) -> str:
    def _repl(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")
    return ENV_RE.sub(_repl, value)

def _resolve_dict(d, seen: set | None = None):
    if seen is None:
        seen = set()
    key = id(d)
    if key in seen:
        return
    seen.add(key)
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, str) and "{{" in v:
                d[k] = _resolve_env(v)
            else:
                _resolve_dict(v, seen)
    elif isinstance(d, list):
        for item in d:
            _resolve_dict(item, seen)

def _build_backend(name: str, btype: str, data: dict) -> BackendDef:
    b = BackendDef(type=btype, name=name)
    for k, v in data.items():
        if hasattr(b, k):
            setattr(b, k, v)
    return b

def load_config(path: str = "config.yaml") -> Config:
    if not os.path.exists(path):
        return Config()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not raw:
        return Config()
    _resolve_dict(raw)

    cfg = Config()
    for bdef in raw.get("backups", []):
        cfg.backups.append(BackupDef(**bdef))

    for btype, blist in raw.get("backends", {}).items():
        for item in blist:
            name = item.pop("name", f"{btype}_{len(cfg.backends.get(btype, []))}")
            cfg.backends.setdefault(btype, []).append(_build_backend(name, btype, item))

    if "chunking" in raw:
        cfg.chunking = ChunkingConfig(**raw["chunking"])
    if "compression" in raw:
        comp = raw["compression"]
        overrides = []
        for o in comp.pop("overrides", []):
            overrides.append(CompressOverride(**o))
        cfg.compression = CompressionConfig(**comp)
        cfg.compression.overrides = overrides
    if "encryption" in raw:
        cfg.encryption = EncryptionConfig(**raw["encryption"])
    if "verification" in raw:
        v = raw["verification"]
        cfg.verification = VerificationConfig(
            enabled=v.get("enabled", True),
            retries=v.get("retries", 3),
            fallback_on_fail=v.get("fallback_on_fail", True),
            verify_restore_count=v.get("verify_restore", {}).get("count", 5),
            verify_restore_schedule=v.get("verify_restore", {}).get("schedule", "weekly"),
        )
    if "retention" in raw:
        cfg.retention = RetentionConfig(**raw["retention"])
    if "database" in raw:
        db = raw["database"]
        snap = DatabaseSnapshotConfig(**db.get("snapshots", {}))
        auto = DatabaseAutoBackupConfig(**db.get("auto_backup", {}))
        cfg.database = DatabaseConfig(snapshots=snap, auto_backup=auto)
    if "notifications" in raw:
        cfg.notifications = NotificationsConfig(**raw["notifications"])

    return cfg
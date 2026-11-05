import os, re, yaml
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

ENV_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

_log = logging.getLogger(__name__)

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

def _parse_size_val(v):
    """Parse a value that might be a human-readable size string (e.g. '64MB') into int."""
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        m = _SIZE_RE.match(v)
        if m:
            num = float(m.group(1))
            unit = (m.group(2) or "B").upper()
            return int(num * _SIZE_UNITS.get(unit, 1))
    return v

@dataclass
class ChunkingConfig:
    enabled: bool = True
    min_size: int = 10_485_760
    chunk_size: int = 67_108_864

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
class SchedulerConfig:
    interval: int = 3600
    ingest_interval: int = 300
    gc_interval_days: int = 7
    verify_interval_days: int = 1

@dataclass
class DBLifecycleConfig:
    purge_grace_days: int = 14
    cold_export_after_days: int = 30
    parquet_dir: str = "parquet"
    parquet_compression: str = "zstd"
    parquet_compression_level: int = 3

@dataclass
class DatabaseConfig:
    snapshots: DatabaseSnapshotConfig = field(default_factory=DatabaseSnapshotConfig)
    auto_backup: DatabaseAutoBackupConfig = field(default_factory=DatabaseAutoBackupConfig)
    lifecycle: DBLifecycleConfig = field(default_factory=DBLifecycleConfig)

@dataclass
class NotificationsConfig:
    on_failure: bool = True
    desktop: bool = True

@dataclass
class IngestConfig:
    enabled: bool = False
    drop_root: str = ""                  # directory holding cpmove-*.tar.gz
    staging_dir: str = "staging"         # plaintext extraction area
    staging_budget_gb: float = 10.0
    max_parallel: int = 2
    bandwidth_limit_mbps: float = 0.0    # 0 = unlimited (Phase 3 pacing hook)
    keep_raw: int = 0                    # generations of raw archive to keep (0 = none)
    raw_backend: str = ""                # backend name for raw archives ("" = default set)
    account_pattern: str = ""            # optional regex override for account detection
    exclude_members: list[str] = field(default_factory=list)  # fnmatch globs
    max_member_size_gb: float = 5.0      # skip members larger than this

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
    base_url: str = ""
    api_key: str = ""
    email: str = ""
    refresh_token: str = ""
    folder: str = ""

@dataclass
class BackupDef:
    source: str = ""
    exclude: list[str] = field(default_factory=list)
    mirror: bool = False
    backend: Optional[str] = None
    move: bool = False

@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    token: str = ""          # empty = open dashboard (loopback-only today)


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
    db_lifecycle: DBLifecycleConfig = field(default_factory=DBLifecycleConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    cpanel_ingest: IngestConfig = field(default_factory=IngestConfig)
    web: WebConfig = field(default_factory=WebConfig)


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

def _filter_fields(cls, data: dict) -> dict:
    """Keep known dataclass fields only; warn about ignored keys so existing
    YAML files never crash when options are removed."""
    known = cls.__dataclass_fields__
    dropped = sorted(k for k in data if k not in known)
    if dropped:
        _log.warning("config: ignoring unknown %s option(s): %s",
                     cls.__name__, ", ".join(dropped))
    return {k: v for k, v in data.items() if k in known}


def _build_backend(name: str, btype: str, data: dict) -> BackendDef:
    b = BackendDef(type=btype, name=name)
    _int_fields = {"port", "priority", "weight"}
    for k, v in data.items():
        if hasattr(b, k):
            if k in _int_fields:
                try:
                    v = int(v)
                except (ValueError, TypeError):
                    continue
            setattr(b, k, v)
    return b

def load_config(path: str | None = None) -> Config:
    if path is None:
        path = os.environ.get("BACKUPME_CONFIG", "config.yaml")
    if not os.path.exists(path):
        return Config()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not raw:
        return Config()
    _resolve_dict(raw)

    cfg = Config()
    for bdef in raw.get("backups", []):
        cfg.backups.append(BackupDef(**_filter_fields(BackupDef, bdef)))

    for btype, blist in raw.get("backends", {}).items():
        for item in blist:
            name = item.get("name", f"{btype}_{len(cfg.backends.get(btype, []))}")
            cfg.backends.setdefault(btype, []).append(_build_backend(name, btype, dict(item)))

    if "chunking" in raw:
        ck = {k: _parse_size_val(v) for k, v in raw["chunking"].items()}
        cfg.chunking = ChunkingConfig(**_filter_fields(ChunkingConfig, ck))
    if "compression" in raw:
        comp = dict(raw["compression"])
        overrides = []
        for o in comp.pop("overrides", []) or []:
            overrides.append(CompressOverride(**_filter_fields(CompressOverride, o)))
        cfg.compression = CompressionConfig(**_filter_fields(CompressionConfig, comp))
        cfg.compression.overrides = overrides
    if "encryption" in raw:
        cfg.encryption = EncryptionConfig(**_filter_fields(EncryptionConfig, raw["encryption"]))
    if "verification" in raw:
        v = raw["verification"]
        cfg.verification = VerificationConfig(
            enabled=v.get("enabled", True),
            retries=v.get("retries", 3),
            verify_restore_count=v.get("verify_restore", {}).get("count", 5),
            verify_restore_schedule=v.get("verify_restore", {}).get("schedule", "weekly"),
        )
        _filter_fields(VerificationConfig, v)  # warn about removed/unknown keys
    if "retention" in raw:
        cfg.retention = RetentionConfig(**_filter_fields(RetentionConfig, raw["retention"]))
    if "database" in raw:
        db = raw["database"]
        snap = DatabaseSnapshotConfig(**_filter_fields(DatabaseSnapshotConfig, db.get("snapshots", {})))
        auto = DatabaseAutoBackupConfig(**_filter_fields(DatabaseAutoBackupConfig, db.get("auto_backup", {})))
        cfg.database = DatabaseConfig(snapshots=snap, auto_backup=auto)
    if "db_lifecycle" in raw:
        cfg.db_lifecycle = DBLifecycleConfig(**_filter_fields(DBLifecycleConfig, raw["db_lifecycle"]))
    if "database" in raw and "lifecycle" in raw["database"]:
        cfg.database.lifecycle = DBLifecycleConfig(**_filter_fields(DBLifecycleConfig, raw["database"]["lifecycle"]))
    if "scheduler" in raw:
        cfg.scheduler = SchedulerConfig(**_filter_fields(SchedulerConfig, raw["scheduler"]))
    if "notifications" in raw:
        cfg.notifications = NotificationsConfig(**_filter_fields(NotificationsConfig, raw["notifications"]))
    if "cpanel_ingest" in raw:
        cfg.cpanel_ingest = IngestConfig(**_filter_fields(IngestConfig, raw["cpanel_ingest"]))
    if "web" in raw:
        cfg.web = WebConfig(**_filter_fields(WebConfig, raw["web"]))

    return cfg
"""Pin config.example.yaml to the config dataclasses.

Guards against the wiring-drift class-bug where a YAML key exists but no
dataclass field reads it (the key is then silently warned-and-ignored forever),
or a dataclass field exists but the example never documents it.
"""
import dataclasses
from pathlib import Path

import yaml

from backupme.config import (
    BackupDef,
    BackendDef,
    ChunkingConfig,
    CompressionConfig,
    CompressOverride,
    EncryptionConfig,
    IngestConfig,
    NotificationsConfig,
    RetentionConfig,
    SchedulerConfig,
    VerificationConfig,
    WebConfig,
)

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "config.example.yaml"

# sections in the example mapped to their dataclass
SCALAR_SECTIONS = {
    "chunking": ChunkingConfig,
    "compression": CompressionConfig,
    "encryption": EncryptionConfig,
    "verification": VerificationConfig,
    "retention": RetentionConfig,
    "scheduler": SchedulerConfig,
    "notifications": NotificationsConfig,
    "cpanel_ingest": IngestConfig,
    "web": WebConfig,
}

# fields that legitimately differ between the example and the dataclass default
OPTIONAL_IN_EXAMPLE = {
    "compression": {"overrides"},
    "verification": {"verify_restore"},  # documented in nested shape (see below)
    "db_lifecycle": set(),
}


def _documented(section: str, cls) -> set:
    """Keys of the example section, counting nested shapes that load_config maps
    onto dataclass fields as documented (verification.verify_restore -> count/schedule)."""
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    documented = set(data[section])
    if section == "verification" and isinstance(data[section].get("verify_restore"), dict):
        documented |= {"verify_restore_count", "verify_restore_schedule"}
    return documented


def _fields(cls):
    return {f.name for f in dataclasses.fields(cls)}


def test_example_has_only_known_scalar_keys():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for section, cls in SCALAR_SECTIONS.items():
        assert section in data, f"config.example.yaml is missing the '{section}' section"
        unknown = set(data[section]) - _fields(cls) - OPTIONAL_IN_EXAMPLE.get(section, set())
        assert not unknown, f"{section}: keys with no dataclass field: {sorted(unknown)}"


def test_example_documents_every_scalar_field():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for section, cls in SCALAR_SECTIONS.items():
        missing = _fields(cls) - _documented(section, cls) - OPTIONAL_IN_EXAMPLE.get(section, set())
        assert not missing, f"{section}: dataclass fields missing from example: {sorted(missing)}"


def test_verification_nested_verify_restore_documented():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    vr = data["verification"].get("verify_restore")
    assert isinstance(vr, dict) and {"count", "schedule"} <= set(vr), (
        "verification.verify_restore {count, schedule} must stay documented: "
        "load_config reads verify_restore_count/schedule from this nested shape"
    )


def test_web_section_documents_token():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert "token" in data["web"], (
        "web.token must stay documented: it arms /api/* Bearer auth"
    )


def test_top_level_sections_all_present():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for section in ("backends", "backups", "database", "db_lifecycle"):
        assert section in data, f"config.example.yaml is missing the '{section}' section"


def test_backup_and_backend_defs_documented():
    """Every BackupDef/BackendDef field must appear somewhere in the example's
    backend/backup entries (the reference users copy from)."""
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    backend_keys = {k for entries in data["backends"].values() for e in entries for k in e}
    backup_keys = {k for b in data["backups"] for k in b}
    missing_backup = _fields(BackupDef) - backup_keys
    assert not missing_backup, f"BackupDef fields missing from example backups: {sorted(missing_backup)}"
    missing_backend = _fields(BackendDef) - backend_keys - {"name"}
    # 9drive-only fields may live in a commented example; enforce the common core strictly
    core = {"type", "path", "host", "port", "username", "password", "key_file",
            "share", "remote", "quota", "priority"}
    assert not (missing_backend & core), (
        f"BackendDef core fields missing from example backends: {sorted(missing_backend & core)}"
    )

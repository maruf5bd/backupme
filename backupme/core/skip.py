import os
import fnmatch
import tempfile
from pathlib import Path

_WINDOWS_SYSTEM_DIRS = {
    "Windows", "Windows.old", "WinSxS", "System32", "SysWOW64",
    "Program Files", "Program Files (x86)", "ProgramData",
    "Recovery", "System Volume Information", "$Recycle.Bin",
    "Config.Msi", "MSOCache", "PerfLogs",
}

_WINDOWS_TEMP_DIRS = {
    "Temporary Internet Files",
    "AppData\\Local\\Temp", "AppData\\Local\\Microsoft\\Windows\\INetCache",
    "AppData\\Local\\Microsoft\\Windows\\WER",
    "AppData\\Roaming\\Microsoft\\Windows\\Recent",
}

_SKIP_DIR_PATTERNS = [
    ".git", ".svn", ".hg", "__pycache__", "node_modules",
    ".venv", "venv", "env", ".tox", ".eggs", "eggs",
    ".cache", ".npm", ".yarn", ".bundle",
    ".idea", ".vscode", ".vs",
    "build", "dist", "target", "out",
]

_SKIP_FILE_PATTERNS = [
    "*.tmp", "*.temp", "*.log", "*.cache",
    "*.swp", "*.swo", "*.bak", "*.old",
    "*.dmp", "*.hprof", "*.pdb",
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "Thumbs.db", ".DS_Store", "desktop.ini",
    "dump.rdb", "*.sqlite-wal", "*.sqlite-shm",
    "backupme.db", "backupme.db-wal", "backupme.db-shm",
]


def should_skip(path: str | Path, user_excludes: list[str] | None = None) -> tuple[bool, str]:
    """Returns (skip: bool, reason: str) for whether this path should be excluded from backup.

    Checks:
    - Windows system directories
    - Temp directories
    - Common dev/build artifacts
    - User-provided fnmatch excludes
    - Known skip file patterns (unless extension is in always-keep list)
    """
    p = Path(path)
    name = p.name

    # os.walk and the CLI walkers already produce absolute paths — resolving
    # them is a syscall per file on the hot path.
    rp = p if p.is_absolute() else p.resolve()
    parts = rp.parts
    full_path = str(rp)
    for part in parts:
        if part in _WINDOWS_SYSTEM_DIRS:
            return True, f"Windows system dir: {part}"
        for pattern in _SKIP_DIR_PATTERNS:
            if fnmatch.fnmatch(part, pattern):
                return True, f"Excluded pattern dir: {pattern}"
    for temp_pat in _WINDOWS_TEMP_DIRS:
        if temp_pat in full_path:
            return True, f"Temp dir pattern: {temp_pat}"

    for pattern in _SKIP_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True, f"Excluded pattern file: {pattern}"

    if user_excludes:
        posix_full = full_path.replace("\\", "/")
        for pattern in user_excludes:
            norm = pattern.replace("\\", "/")
            if (
                fnmatch.fnmatch(name, norm)
                or fnmatch.fnmatch(posix_full, norm)
                or fnmatch.fnmatch(posix_full, "**/" + norm)
            ):
                return True, f"User exclude: {pattern}"

    return False, ""


def should_skip_dir(path: str | Path) -> bool:
    """Quick check for directory-level skip (no user excludes)."""
    p = Path(path)
    name = p.name
    rp = p if p.is_absolute() else p.resolve()
    full = str(rp)
    if name in _WINDOWS_SYSTEM_DIRS:
        return True
    for pattern in _SKIP_DIR_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    for temp_pat in _WINDOWS_TEMP_DIRS:
        if temp_pat in full:
            return True
    return False
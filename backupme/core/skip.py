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
    "Temp", "tmp", "Temporary Internet Files",
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
]

_ALWAYS_KEEP = {".txt", ".md", ".py", ".js", ".ts", ".rs", ".go", ".java",
                ".c", ".h", ".cpp", ".hpp", ".yaml", ".yml", ".json", ".xml",
                ".csv", ".xlsx", ".docx", ".pdf", ".jpg", ".png"}


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
    ext = p.suffix.lower()

    parts = p.resolve().parts
    full_path = str(p.resolve())
    for part in parts:
        if part in _WINDOWS_SYSTEM_DIRS:
            return True, f"Windows system dir: {part}"
        if part.lower() in ("temp", "tmp"):
            return True, f"Temp dir: {part}"
        for pattern in _SKIP_DIR_PATTERNS:
            if fnmatch.fnmatch(part, pattern):
                return True, f"Excluded pattern dir: {pattern}"
    for temp_pat in _WINDOWS_TEMP_DIRS:
        if temp_pat in full_path:
            return True, f"Temp dir pattern: {temp_pat}"

    for pattern in _SKIP_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            if ext in _ALWAYS_KEEP:
                return False, ""
            return True, f"Excluded pattern file: {pattern}"

    if user_excludes:
        for pattern in user_excludes:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(p), pattern):
                return True, f"User exclude: {pattern}"

    return False, ""


def should_skip_dir(path: str | Path) -> bool:
    """Quick check for directory-level skip (no user excludes)."""
    p = Path(path)
    name = p.name
    full = str(p.resolve())
    if name in _WINDOWS_SYSTEM_DIRS:
        return True
    if name.lower() in ("temp", "tmp"):
        return True
    for pattern in _SKIP_DIR_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    for temp_pat in _WINDOWS_TEMP_DIRS:
        if temp_pat in full:
            return True
    return False
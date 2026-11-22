"""Shared path-name safety guards for upload indexing and restore writing (D36 / SEC T7).

Some names index fine but can never be restored safely on Windows:
- a colon in a file name is an NTFS alternate data stream ("evil.txt:stream"),
- reserved device names (CON, NUL, COM1..9, ...) are refused by the filesystem
  as any path component,
- a literal ".." segment escapes the restore root.
Both sides reject them up front with a clear error instead of storing a row
that can only fail later.
"""

# Windows reserved device names, matched case-insensitively against the part
# of a name before the first dot (so "CON.txt" is reserved too).
_RESERVED_BASE_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM0", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT0", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    "CONIN$", "CONOUT$",
})


def reserved_device_reason(name: str) -> str | None:
    """Reason `name` is a Windows reserved device name, or None."""
    base = name.split(".", 1)[0].strip().upper()
    if base in _RESERVED_BASE_NAMES:
        return f"reserved Windows device name {name!r}"
    return None


def unsafe_relative_path_reason(relative_path: str) -> str | None:
    """Reason a relative path can never be indexed/restored safely, or None.

    Applied to uploaded relative paths (and archive member names): ".." and
    reserved device names are unsafe in ANY segment; a colon is rejected in
    the final segment (the file name an NTFS stream would attach to).
    """
    parts = str(relative_path).replace("\\", "/").split("/")
    final = parts[-1] if parts else ""
    for part in parts:
        if part == "..":
            return f"path has a literal '..' segment: {relative_path!r}"
        device_reason = reserved_device_reason(part)
        if device_reason:
            return f"{device_reason} (in path {relative_path!r})"
    if ":" in final:
        return f"NTFS alternate-stream name (colon in file name): {relative_path!r}"
    return None


def unsafe_dest_filename_reason(name: str) -> str | None:
    """Reason a restore destination file name must not be written, or None."""
    if name in ("..", "."):
        return f"unsafe restore destination name {name!r}"
    if ":" in name:
        return f"NTFS alternate-stream restore destination {name!r}"
    return reserved_device_reason(name)

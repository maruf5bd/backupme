"""Scan all local drives for archive files (zip/tar/tar.gz/7z/rar/etc.).
Reports found archives with path, size, and drive."""
import os
import re
import sys
from pathlib import Path

ARCHIVE_PAT = re.compile(r"\.(zip|tar|tar\.gz|tgz|tar\.bz2|tbz2|taz|7z|rar)$", re.I)

# Common drive letters to check on Windows
DRIVES = [f"{d}:\\" for d in "DEFGHIJKLMNOPQRSTUVWXYZ"]

def find_archives(drive):
    """Walk a drive and return list of (path, size) for archive files."""
    results = []
    drive_path = Path(drive)
    try:
        if not drive_path.exists():
            return results
    except (PermissionError, OSError):
        print(f"  [cannot access {drive}]")
        return results
    try:
        for root, dirs, files in os.walk(drive_path):
            # Skip common junk/system dirs to speed up
            dirs[:] = [d for d in dirs if d not in (
                "$RECYCLE.BIN", "System Volume Information", "Windows",
                "Program Files", "Program Files (x86)", "AppData",
                "ProgramData", "Users"
            )]
            for f in files:
                if ARCHIVE_PAT.search(f):
                    fp = Path(root) / f
                    try:
                        results.append((str(fp), fp.stat().st_size))
                    except OSError:
                        pass
    except (PermissionError, OSError) as e:
        print(f"  [skip {drive}: {e}]")
    return results

def main():
    all_found = []
    total_bytes = 0
    for drive in DRIVES:
        found = find_archives(drive)
        if found:
            print(f"\n=== {drive} ({len(found)} archives) ===")
            for path, size in found:
                total_bytes += size
                print(f"  {size:>12,}  {path}")
            all_found.extend(found)

    print("\n" + "=" * 60)
    print(f"TOTAL: {len(all_found)} archives, {total_bytes:,} bytes ({total_bytes/(1024**3):.2f} GB)")
    
    out = Path("all_drive_archives.txt")
    with out.open("w") as f:
        f.write(f"Total: {len(all_found)} archives, {total_bytes} bytes\n")
        for path, size in all_found:
            f.write(f"{size}\t{path}\n")
    print(f"Full list written to {out}")

if __name__ == "__main__":
    main()

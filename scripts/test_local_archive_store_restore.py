"""Test local archive store → restore → dedup space saving → same-name handling.

Workflow:
1. SCAN local drives for archive files (zip/tar/tar.gz/7z/rar)
2. STORE: back each archive into backupme vault (dedup by SHA-256)
3. RESTORE: download each archive back, verify SHA-256 matches
4. DEDUP REPORT: show space saved (unique blobs vs total bytes)
5. SAME-NAME: detect archives with identical filenames but different paths

Usage:
    python test_local_archive_store_restore.py --scan         # scan drives only
    python test_local_archive_store_restore.py --store        # scan + store
    python test_local_archive_store_restore.py --restore      # restore stored archives
    python test_local_archive_store_restore.py --all          # scan + store + restore
    python test_local_archive_store_restore.py --limit 10     # limit to N archives
"""
import hashlib
import os
import re
import sys
from pathlib import Path

from backupme.cli.base import cli
from click.testing import CliRunner

ARCHIVE_PAT = re.compile(r"\.(zip|tar|tar\.gz|tgz|tar\.bz2|tbz2|taz|7z|rar)$", re.I)

ROOT = Path(__file__).resolve().parent.parent  # repo root (script lives in scripts/)
DRIVES = [f"{d}:\\" for d in "DEFGHIJKLMNOPQRSTUVWXYZ"]

SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information", "Windows",
    "Program Files", "Program Files (x86)", "AppData",
    "ProgramData", "Users",
}


def scan_drives(limit=None):
    """Scan all local drives for archive files. Returns list of (path, size)."""
    found = []
    for drive in DRIVES:
        drive_path = Path(drive)
        try:
            if not drive_path.exists():
                continue
        except (PermissionError, OSError):
            continue
        try:
            for root, dirs, files in os.walk(drive_path):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in files:
                    if ARCHIVE_PAT.search(f):
                        fp = Path(root) / f
                        try:
                            found.append((str(fp), fp.stat().st_size))
                        except OSError:
                            pass
        except (PermissionError, OSError):
            continue
    found.sort(key=lambda x: -x[1])  # largest first
    if limit:
        found = found[:limit]
    return found


def store_archive(filepath):
    """Back up a local archive file into backupme via the CLI backup command."""
    runner = CliRunner()
    result = runner.invoke(cli, ["backup", str(filepath), "--no-skip"])
    return result


def find_in_db(sha256):
    """Look up a file by SHA-256 in the database."""
    from backupme.core.database import execute
    rows = execute(
        "SELECT id, source_root, relative_path, size, sha256 FROM files WHERE sha256 = ? AND deleted_at IS NULL",
        (sha256,),
    ).fetchall()
    return rows


def restore_by_sha256(sha256, out_dir):
    """Restore a file from backupme by SHA-256."""
    rows = find_in_db(sha256)
    if not rows:
        return None, "not in database"
    row = rows[0]
    from backupme.core.downloader import download_file
    dest = Path(out_dir) / f"restored_{row['id']}.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = download_file(row["id"], str(dest))
    return result, None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dedup_report():
    """Show dedup space savings from the database."""
    from backupme.core.database import execute
    total_rows = execute("SELECT COUNT(*) FROM files WHERE deleted_at IS NULL").fetchone()[0]
    total_bytes = execute("SELECT COALESCE(SUM(size),0) FROM files WHERE deleted_at IS NULL").fetchone()[0]
    unique_blobs = execute("SELECT COUNT(DISTINCT blob_id) FROM file_chunks").fetchone()[0]
    unique_bytes = execute(
        "SELECT COALESCE(SUM(DISTINCT size),0) FROM files WHERE deleted_at IS NULL"
    ).fetchone()[0]
    print("\n=== DEDUP REPORT ===")
    print(f"Total file rows:    {total_rows:,}")
    print(f"Total bytes:        {total_bytes:,} ({total_bytes/(1024**3):.2f} GB)")
    print(f"Unique blobs:       {unique_blobs:,}")
    print(f"Space saved:        {total_bytes - unique_bytes:,} bytes ({(1-unique_bytes/total_bytes)*100:.1f}%)")


def same_name_report():
    """Find archives with same filename but different paths/content."""
    from backupme.core.database import execute
    rows = execute(
        "SELECT source_root, relative_path, size, sha256 FROM files WHERE deleted_at IS NULL "
        "AND relative_path LIKE '%.zip' OR relative_path LIKE '%.tar.gz' OR relative_path LIKE '%.7z' OR relative_path LIKE '%.rar'"
    ).fetchall()
    by_name = {}
    for r in rows:
        name = Path(r["relative_path"]).name
        by_name.setdefault(name, []).append(r)
    dupes = {n: rs for n, rs in by_name.items() if len(rs) > 1}
    print("\n=== SAME-NAME ARCHIVES ===")
    print(f"Groups with same filename: {len(dupes)}")
    for name, rs in list(dupes.items())[:10]:
        print(f"\n  {name} ({len(rs)} copies):")
        for r in rs:
            print(f"    {r['source_root']}\\{r['relative_path']}  {r['size']:,} bytes  sha={r['sha256'][:12]}...")
    return dupes


def cmd_scan(limit):
    found = scan_drives(limit)
    print(f"\n=== LOCAL DRIVE SCAN ({len(found)} archives) ===")
    total = 0
    for path, size in found:
        total += size
        print(f"  {size:>12,}  {path}")
    print(f"\nTOTAL: {len(found)} archives, {total:,} bytes ({total/(1024**3):.2f} GB)")
    return found


def cmd_store(limit):
    found = scan_drives(limit)
    print(f"\n=== STORING {len(found)} ARCHIVES ===")
    stored = 0
    skipped = 0
    errors = []
    for idx, (filepath, size) in enumerate(found, 1):
        print(f"[{idx}/{len(found)}] {filepath} ({size:,} bytes)")
        sha = sha256_file(filepath)
        existing = find_in_db(sha)
        if existing:
            print(f"    SKIP (already in DB, id={existing[0]['id']})")
            skipped += 1
            continue
        try:
            result = store_archive(filepath)
            if result.exit_code == 0:
                print(f"    STORED OK")
                stored += 1
            else:
                print(f"    STORE FAILED: {result.output}")
                errors.append((filepath, result.output))
        except Exception as e:
            print(f"    STORE ERROR: {e}")
            errors.append((filepath, str(e)))
    print(f"\nSTORE complete: {stored} stored, {skipped} skipped (dup), {len(errors)} errors")
    return found, errors


def cmd_restore(limit):
    from backupme.core.database import execute
    rows = execute(
        "SELECT id, source_root, relative_path, size, sha256 FROM files WHERE deleted_at IS NULL "
        "ORDER BY id DESC LIMIT ?",
        (limit or 1000,),
    ).fetchall()
    print(f"\n=== RESTORING {len(rows)} ARCHIVES ===")
    out_dir = ROOT / "restored_local_archives"
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    failed = 0
    hash_mismatch = 0
    for idx, row in enumerate(rows, 1):
        print(f"[{idx}/{len(rows)}] {row['relative_path']} ({row['size']:,} bytes)")
        result, err = restore_by_sha256(row["sha256"], str(out_dir))
        if err:
            print(f"    FAILED: {err}")
            failed += 1
            continue
        if result and result["status"] == "ok":
            actual = sha256_file(result["path"])
            if actual == row["sha256"]:
                print(f"    RESTORED OK (hash verified)")
                ok += 1
            else:
                print(f"    HASH MISMATCH!")
                hash_mismatch += 1
        else:
            print(f"    RESTORE FAILED")
            failed += 1
    print(f"\nRESTORE complete: {ok} OK, {failed} failed, {hash_mismatch} hash mismatches")
    return ok, failed, hash_mismatch


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Local archive store → restore → dedup test")
    parser.add_argument("--scan", action="store_true", help="Scan drives only")
    parser.add_argument("--store", action="store_true", help="Scan + store archives")
    parser.add_argument("--restore", action="store_true", help="Restore stored archives")
    parser.add_argument("--all", action="store_true", help="Scan + store + restore + reports")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N archives")
    args = parser.parse_args()

    if args.all:
        args.scan = args.store = args.restore = True

    if args.scan:
        cmd_scan(args.limit)

    if args.store:
        cmd_store(args.limit)

    if args.restore:
        cmd_restore(args.limit)

    if args.all or (args.store or args.restore):
        dedup_report()
        same_name_report()


if __name__ == "__main__":
    main()

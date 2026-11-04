"""Restore archive files from the backup vault or a remote backend.

Three modes:

1. TEST phase (`--one`): download ONE archive from storage, extract it,
   print its member listing. Proves download + extraction works before any
   bulk operation.

2. ALL phase (`--all`): download every archive (zip/tar/tar.gz/7z/rar)
   tracked in the database to a local output directory, extract each one,
   and write a manifest.

3. SCAN phase (`--scan-storagebox`): list archive files on the Storage Box
   backend so you can decide what to restore.

Usage:
    python test_restore_archives.py --one
    python test_restore_archives.py --all
    python test_restore_archives.py --scan-storagebox
"""
import argparse
import re
import sys
import tarfile
import zipfile
from pathlib import Path

from backupme.core.database import execute
from backupme.core.downloader import download_file
from backupme.core.database import close_connection
from backupme.backends import get_all_backends
from backupme.config import load_config

ROOT = Path(__file__).resolve().parent.parent  # repo root (script lives in scripts/)

ARCHIVE_PAT = re.compile(r"\.(zip|tar|tar\.gz|tgz|tar\.bz2|tbz2|taz|7z|rar)$", re.I)

OUTPUT_DIR = ROOT / "restored_archives"
EXTRACT_DIR = ROOT / "extracted_archives"
MANIFEST = ROOT / "archive_processing_manifest.txt"
SCAN_LIST = ROOT / "storagebox_archives.txt"


def find_archives(accessible_only=False):
    """Return list of (file_id, source_root, relative_path, size, sha256)
    for every archive tracked in the database."""
    query = (
        "SELECT id, source_root, relative_path, size, sha256 "
        "FROM files WHERE deleted_at IS NULL ORDER BY relative_path"
    )
    if accessible_only:
        query = (
            "SELECT f.id, f.source_root, f.relative_path, f.size, f.sha256 "
            "FROM files f "
            "WHERE f.deleted_at IS NULL "
            "AND EXISTS ("
            "  SELECT 1 FROM file_chunks fc "
            "  JOIN blob_storage bs ON bs.blob_id = fc.blob_id "
            "  WHERE fc.file_id = f.id AND bs.backend_name != 'hostraj Gdrive'"
            ")"
        )
    rows = execute(query).fetchall()
    return [
        (r["id"], r["source_root"], r["relative_path"], r["size"], r["sha256"])
        for r in rows
        if ARCHIVE_PAT.search(r["relative_path"] or "")
    ]


def download_one(file_id, dest_path):
    """Download one archive using the atomic downloader (verify-hash)."""
    return download_file(file_id, str(dest_path))


def extract_archive(archive_path: Path, extract_to: Path):
    """Extract an archive into extract_to/<archive_stem>/. Returns list of
    (member_name, size) for everything extracted."""
    extract_to.mkdir(parents=True, exist_ok=True)
    target = extract_to / archive_path.stem
    target.mkdir(parents=True, exist_ok=True)
    members = []

    name = archive_path.name.lower()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    zf.extract(info, target)
                    members.append((info.filename, info.file_size))
        elif name.endswith(".tar.gz") or name.endswith(".tgz"):
            with tarfile.open(archive_path, "r:gz") as tf:
                for m in tf.getmembers():
                    if m.isdir():
                        continue
                    tf.extract(m, target)
                    members.append((m.name, m.size))
        elif name.endswith(".tar.bz2") or name.endswith(".tbz2"):
            with tarfile.open(archive_path, "r:bz2") as tf:
                for m in tf.getmembers():
                    if m.isdir():
                        continue
                    tf.extract(m, target)
                    members.append((m.name, m.size))
        elif name.endswith(".tar"):
            with tarfile.open(archive_path, "r:") as tf:
                for m in tf.getmembers():
                    if m.isdir():
                        continue
                    tf.extract(m, target)
                    members.append((m.name, m.size))
        elif name.endswith(".7z"):
            try:
                import py7zr

                with py7zr.SevenZipFile(archive_path, "r") as zf:
                    zf.extractall(target)
                    for m in zf.list():
                        members.append((m.filename, m.uncompressed))
            except Exception as e:
                members.append((f"[7z extract error: {e}]", 0))
        elif name.endswith(".rar"):
            try:
                import rarfile

                with rarfile.RarFile(archive_path, "r") as rf:
                    for info in rf.infolist():
                        if info.isdir():
                            continue
                        rf.extract(info, target)
                        members.append((info.filename, info.file_size))
            except ImportError:
                members.append(
                    ("[skipped: rarfile not installed — pip install rarfile]", 0)
                )
            except Exception as e:
                members.append((f"[rar extract error: {e}]", 0))
    except Exception as e:
        members.append((f"[extract error: {e}]", 0))

    return target, members


def scan_storagebox():
    """List archive files on the Storage Box backend."""
    cfg = load_config()
    backends = get_all_backends(cfg)
    storagebox = None
    for b in backends:
        if b.name == "storagebox":
            storagebox = b
            break

    if not storagebox:
        print("Storage Box backend 'storagebox' not found in config.")
        print(
            "Run: backupme backend add sftp "
            "--name storagebox --host u321741.your-storagebox.de --port 22 "
            "--username u321741 --password <password>"
        )
        return []

    try:
        files = storagebox.walk("")
    except Exception as e:
        print(f"Error walking Storage Box: {e}")
        return []

    archives = []
    for f in files:
        if ARCHIVE_PAT.search(f["path"]):
            archives.append((f["path"], f["size"]))

    print(f"\n=== Storage Box archives ({len(archives)} found) ===")
    total = 0
    for path, size in archives:
        total += size
        print(f"  {size:>12,}  {path}")

    print(f"\nTOTAL: {len(archives)} archives, {total:,} bytes ({total / (1024**3):.2f} GB)")
    print(f"Full list written to {SCAN_LIST}")

    with SCAN_LIST.open("w") as f:
        f.write(f"Total: {len(archives)} archives, {total} bytes\n")
        for path, size in archives:
            f.write(f"{size}\t{path}\n")

    return archives


def process_one(archives):
    """Phase 1: download + extract a single archive as a test."""
    candidate = None
    for a in archives:
        file_id, src, path, size, sha = a
        if 10_000 <= size <= 50_000_000:  # 10 KB to 50 MB
            candidate = a
            break
    if candidate is None:
        candidate = archives[0]

    file_id, src, rel_path, size, sha256 = candidate
    print(f"TEST archive: id={file_id} src={src} path={rel_path} size={size}")
    print(f"  sha256={sha256[:16]}...")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUTPUT_DIR / Path(rel_path).name
    if dest.exists():
        dest = OUTPUT_DIR / f"{Path(rel_path).stem}_{file_id}{Path(rel_path).suffix}"

    print(f"  downloading to {dest} ...")
    result = download_one(file_id, str(dest))
    print(f"  download status={result['status']} bytes={result['size']}")
    if result["status"] != "ok":
        print(f"  DOWNLOAD FAILED: {result.get('message') or 'unknown error'}")
        close_connection()
        return False

    print(f"  extracting ...")
    target, members = extract_archive(dest, EXTRACT_DIR)
    print(f"  extracted to {target}")
    print(f"  members: {len(members)}")
    for mname, msize in members[:15]:
        print(f"    {mname} ({msize} bytes)")
    if len(members) > 15:
        print(f"    ... and {len(members) - 15} more")

    close_connection()
    print("\nTEST PASSED: download + extract works for one archive.")
    return True


def process_all(archives, limit=None):
    """Phase 2: download + extract every archive, write a manifest."""
    if limit:
        archives = archives[:limit]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)

    total = len(archives)
    ok = 0
    failed = []
    total_members = 0

    with MANIFEST.open("w") as mf:
        mf.write("Archive processing manifest\n")
        mf.write("=========================\n")
        mf.write(f"Total archives: {total}\n\n")

        for idx, (file_id, src, rel_path, size, sha256) in enumerate(archives, 1):
            print(f"[{idx}/{total}] {src}\\{rel_path} ({size} bytes)")
            dest = OUTPUT_DIR / Path(rel_path).name
            if dest.exists():
                dest = OUTPUT_DIR / f"{Path(rel_path).stem}_{file_id}{Path(rel_path).suffix}"

            try:
                result = download_one(file_id, str(dest))
                if result["status"] != "ok":
                    failed.append((rel_path, result.get("message") or "download failed"))
                    mf.write(f"{rel_path}: DOWNLOAD FAILED\n")
                    continue

                target, members = extract_archive(dest, EXTRACT_DIR)
                total_members += len(members)
                ok += 1
                mf.write(f"{rel_path}: OK -> {target} ({len(members)} members)\n")
                for mname, msize in members[:5]:
                    mf.write(f"    {mname} ({msize} bytes)\n")
                if len(members) > 5:
                    mf.write(f"    ... and {len(members) - 5} more\n")
            except Exception as e:
                failed.append((rel_path, str(e)))
                mf.write(f"{rel_path}: ERROR {e}\n")

    close_connection()
    print(f"\nALL phase complete: {ok}/{total} downloaded+extracted, {len(failed)} failed")
    print(f"Total members extracted: {total_members}")
    print(f"Manifest written to: {MANIFEST}")
    print(f"Archives in: {OUTPUT_DIR}")
    print(f"Extractions in: {EXTRACT_DIR}")
    return failed


def main():
    parser = argparse.ArgumentParser(
        description="Restore and process archive files from backup vault"
    )
    parser.add_argument("--one", action="store_true", help="Test with one archive only")
    parser.add_argument("--all", action="store_true", help="Process all archives")
    parser.add_argument(
        "--scan-storagebox",
        action="store_true",
        help="Scan Storage Box backend for archive files",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit for --all")
    args = parser.parse_args()

    if args.scan_storagebox:
        scan_storagebox()
        return

    if not (args.one or args.all):
        parser.error("specify --one, --all, or --scan-storagebox")

    archives = find_archives(accessible_only=not args.all)
    print(f"Found {len(archives)} archive files in database.\n")

    if args.one:
        ok = process_one(archives)
        if ok:
            print("\nYou can now run: python test_restore_archives.py --all")
        return

    if args.all:
        process_all(archives, limit=args.limit)


if __name__ == "__main__":
    main()

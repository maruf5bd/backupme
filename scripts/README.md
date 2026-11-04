# scripts/

Standalone, user-facing maintenance and test tools. These are not part of the
pytest suite (that lives in `tests/`). Run them from the repository root so
that files they write land in the repo-root scratch dirs:

    cd /j/personal@hostraj.com/backupme
    python scripts/scan_all_drives.py

## Tools

- `test_restore_archives.py` — Download, extract and verify archives tracked
  in the backupme database: one archive, all of them, or just scan the Storage
  Box backend. Writes to `restored_archives/` and `extracted_archives/`.
- `test_local_archive_store_restore.py` — End-to-end check of the local
  archive flow: scan drives for archives, store them into the vault, restore
  them back and verify SHA-256, then report dedup savings and same-name
  conflicts. Writes to `restored_local_archives/`.
- `scan_all_drives.py` — Walk all mounted Windows drives and list archive
  files (zip/tar/tar.gz/7z/rar) with sizes; writes `all_drive_archives.txt`
  to the current directory.
- `test_storagebox_sftp.py` — Connectivity smoke test for the Hetzner Storage
  Box SFTP backend; reads the password from the `STORAGEBOX_PASSWORD`
  environment variable.

## Example usage

    python scripts/test_restore_archives.py --one
    python scripts/test_restore_archives.py --all
    python scripts/test_restore_archives.py --scan-storagebox

    python scripts/test_local_archive_store_restore.py --scan
    python scripts/test_local_archive_store_restore.py --store
    python scripts/test_local_archive_store_restore.py --restore
    python scripts/test_local_archive_store_restore.py --all --limit 10

    python scripts/scan_all_drives.py

    STORAGEBOX_PASSWORD=... python scripts/test_storagebox_sftp.py

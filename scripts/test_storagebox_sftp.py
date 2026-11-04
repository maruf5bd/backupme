"""Test Storage Box SFTP connection - reads password from env var."""
import os
import paramiko
from pathlib import Path

host = "u321741.your-storagebox.de"
port = 22
username = "u321741"
password = os.environ.get("STORAGEBOX_PASSWORD", "")

if not password:
    print("Set STORAGEBOX_PASSWORD environment variable and re-run.")
    print("Example: $env:STORAGEBOX_PASSWORD='your_password'")
    print("Then: python test_storagebox_sftp.py")
    exit(1)

try:
    transport = paramiko.Transport((host, port))
    transport.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)

    print("Connected successfully!")
    print(f"Server version: {transport.remote_version}")

    # List root directory
    print("\nRoot directory contents:")
    try:
        for attr in sftp.listdir_attr("."):
            print(f"  {attr.filename}  ({attr.st_size} bytes)")
    except Exception as e:
        print(f"  Error listing root: {e}")

    # Check disk space
    try:
        stat = sftp.statvfs(".")
        free = stat.f_bavail * stat.f_frsize
        total = stat.f_blocks * stat.f_frsize
        print(f"\nDisk space: {free / (1024**3):.2f} GB free of {total / (1024**3):.2f} GB")
    except Exception as e:
        print(f"  Error getting disk stats: {e}")

    sftp.close()
    transport.close()
    print("\nConnection test PASSED")

except Exception as e:
    print(f"\nConnection FAILED: {e}")
